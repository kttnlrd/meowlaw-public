//! Text-level rules shared by every format: encoding, invisible characters,
//! whitespace hygiene, page furniture, hyphenation, flattened tables and
//! duplicated paragraphs. One pass over the lines; duplicate detection hashes
//! paragraphs, so everything is linear in the size of the file.

use crate::{Cx, Finding, Loc, Severity, list_nums, plural};
use regex::Regex;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::LazyLock;

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

pub(crate) struct Decoded {
    pub text: String,
    pub utf16: bool,
    /// Byte offset of the first invalid UTF-8 sequence (Latin-1 fallback used).
    pub invalid_at: Option<usize>,
    /// Bytes 0x80-0x9F present: almost certainly Windows-1252.
    pub cp1252: bool,
}

/// Decode like the ingester (UTF-8, else Latin-1), but recognise UTF-16 so
/// the rest of the lint still says something useful.
pub(crate) fn decode(bytes: &[u8]) -> Decoded {
    let bom_le = bytes.starts_with(&[0xFF, 0xFE]);
    let bom_be = bytes.starts_with(&[0xFE, 0xFF]);
    let sniff = &bytes[..bytes.len().min(1024)];
    let half = sniff.len() / 2;
    let nul_even = sniff.iter().step_by(2).filter(|b| **b == 0).count();
    let nul_odd = sniff.iter().skip(1).step_by(2).filter(|b| **b == 0).count();
    let guess_le = half >= 8 && nul_odd * 10 >= half * 4 && nul_even * 10 < half;
    let guess_be = half >= 8 && nul_even * 10 >= half * 4 && nul_odd * 10 < half;
    if bom_le || bom_be || guess_le || guess_be {
        let be = bom_be || (guess_be && !bom_le);
        let body = if bom_le || bom_be { &bytes[2..] } else { bytes };
        let units = body.chunks_exact(2).map(|c| {
            if be {
                u16::from_be_bytes([c[0], c[1]])
            } else {
                u16::from_le_bytes([c[0], c[1]])
            }
        });
        let text = char::decode_utf16(units)
            .map(|r| r.unwrap_or('\u{fffd}'))
            .collect();
        return Decoded {
            text,
            utf16: true,
            invalid_at: None,
            cp1252: false,
        };
    }
    match std::str::from_utf8(bytes) {
        Ok(s) => Decoded {
            text: s.to_string(),
            utf16: false,
            invalid_at: None,
            cp1252: false,
        },
        Err(e) => Decoded {
            text: bytes.iter().map(|b| *b as char).collect(),
            utf16: false,
            invalid_at: Some(e.valid_up_to()),
            cp1252: bytes.iter().any(|b| (0x80..=0x9F).contains(b)),
        },
    }
}

pub(crate) fn report_decoding(cx: &mut Cx, d: &Decoded, bytes: &[u8]) {
    if d.utf16 {
        cx.add(
            Finding::new(
                "encoding.utf16",
                Severity::Error,
                "file is UTF-16; the ingester reads UTF-8 (or Latin-1), so every other byte becomes a NUL and the text is unusable",
            )
            .fix("Convert to UTF-8, e.g. `iconv -f UTF-16 -t UTF-8 in.txt > out.txt`."),
        );
    }
    if let Some(pos) = d.invalid_at {
        let line = bytes[..pos].iter().filter(|b| **b == b'\n').count() + 1;
        let excerpt_line = d.text.lines().nth(line - 1).unwrap_or("");
        let mut msg = format!(
            "not valid UTF-8 (first bad byte 0x{:02X}); the ingester falls back to Latin-1, so every non-ASCII character may come out wrong",
            bytes[pos]
        );
        if d.cp1252 {
            msg.push_str(
                ". Bytes 0x80–0x9F suggest Windows-1252: smart quotes and dashes will become invisible control characters",
            );
        }
        let enc = if d.cp1252 {
            "WINDOWS-1252"
        } else {
            "ISO-8859-1"
        };
        cx.add(
            Finding::new("encoding.invalid-utf8", Severity::Warning, msg)
                .at(Loc::line(line))
                .excerpt(excerpt_line)
                .fix(format!(
                    "Convert to UTF-8, e.g. `iconv -f {enc} -t UTF-8 in.txt > out.txt`."
                )),
        );
    }
}

/// `file.empty` / `file.tiny`. Returns true when there is nothing to lint.
pub(crate) fn check_empty(cx: &mut Cx, text: &str) -> bool {
    let visible = text
        .chars()
        .filter(|c| !c.is_whitespace() && *c != '\u{feff}')
        .count();
    cx.stat("chars", text.chars().count());
    cx.stat("lines", text.lines().count());
    if visible == 0 {
        cx.add(
            Finding::new("file.empty", Severity::Error, "file has no text")
                .fix("Check the export: the source may be scanned (needs OCR) or the save failed."),
        );
        return true;
    }
    if visible < 200 {
        cx.add(Finding::new(
            "file.tiny",
            Severity::Warning,
            format!("file has only {visible} visible characters; is this the whole document?"),
        ));
    }
    false
}

// ---------------------------------------------------------------------------
// Lines
// ---------------------------------------------------------------------------

/// The text split into lines, with the page of each line (form feeds).
pub(crate) struct Lines<'a> {
    pub lines: Vec<&'a str>,
    /// 1-based page of each line.
    pub page: Vec<usize>,
    /// The text contains form feeds.
    pub has_pages: bool,
    /// Report pages only (PDF): line numbers of extracted text mean nothing to a user.
    pub pdf: bool,
    /// Text rebuilt from a container format (DOCX, HTML, XML): no positions.
    pub no_loc: bool,
}

impl<'a> Lines<'a> {
    pub fn from_text(src: &'a str, pdf: bool) -> Self {
        let mut lines = Vec::new();
        let mut page = Vec::new();
        let mut current = 1usize;
        let mut has_pages = false;
        for raw in src.split('\n') {
            let raw = raw.strip_suffix('\r').unwrap_or(raw);
            let lead = raw.len() - raw.trim_start_matches('\u{c}').len();
            current += lead;
            let total = raw.bytes().filter(|b| *b == 0x0c).count();
            has_pages |= total > 0;
            lines.push(raw.trim_matches('\u{c}'));
            page.push(current);
            current += total - lead;
        }
        // A trailing newline (or form feed) leaves an empty last line.
        if lines.len() > 1 && lines.last().is_some_and(|l| l.is_empty()) {
            lines.pop();
            page.pop();
        }
        Self {
            lines,
            page,
            has_pages,
            pdf,
            no_loc: false,
        }
    }

    /// Text extracted from a container format: rules run, positions are not reported.
    pub fn extracted(src: &'a str) -> Self {
        let mut l = Self::from_text(src, false);
        l.no_loc = true;
        l
    }

    pub fn len(&self) -> usize {
        self.lines.len()
    }

    pub fn pages(&self) -> usize {
        self.page.last().copied().unwrap_or(1)
    }

    pub fn loc(&self, i: usize) -> Loc {
        if self.no_loc {
            Loc::default()
        } else if self.pdf {
            Loc {
                line: None,
                page: self.page.get(i).copied(),
            }
        } else {
            Loc {
                line: Some(i + 1),
                page: if self.has_pages {
                    self.page.get(i).copied()
                } else {
                    None
                },
            }
        }
    }

    /// "on lines 3, 7, 12", "on pages 2, 5" or "in the document".
    pub fn where_(&self, at: &[usize]) -> String {
        if self.no_loc {
            "in the document".to_string()
        } else if self.pdf {
            let mut pages: Vec<usize> = at.iter().map(|i| self.page[*i]).collect();
            pages.dedup();
            let word = if pages.len() == 1 { "page" } else { "pages" };
            format!("on {word} {}", list_nums(&pages, 6))
        } else {
            let nums: Vec<usize> = at.iter().map(|i| i + 1).collect();
            let word = if nums.len() == 1 { "line" } else { "lines" };
            format!("on {word} {}", list_nums(&nums, 6))
        }
    }

    fn blank(&self, i: Option<usize>) -> bool {
        match i {
            None => true,
            Some(i) => self.lines.get(i).is_none_or(|l| l.trim().is_empty()),
        }
    }

    /// Line `i` stands alone: blank (or page/file edge) above and below.
    fn isolated(&self, i: usize) -> bool {
        let prev = i.checked_sub(1);
        let page_edge_before = prev.is_some_and(|p| self.page[p] != self.page[i]);
        let page_edge_after = self.page.get(i + 1).is_some_and(|p| *p != self.page[i]);
        (page_edge_before || self.blank(prev)) && (page_edge_after || self.blank(Some(i + 1)))
    }
}

// ---------------------------------------------------------------------------
// The line pass
// ---------------------------------------------------------------------------

pub(crate) struct TextRules {
    /// Line endings, trailing whitespace, tabs, long lines (source text formats).
    pub hygiene: bool,
    pub furniture: bool,
    pub hyphenation: bool,
    /// Column-layout table detection (plain text and PDF).
    pub layout_tables: bool,
    pub markdown: bool,
    /// A BOM at the start breaks the first line (plain text reader keeps it).
    pub bom_matters: bool,
    /// Lines before this index (front matter) are skipped by line rules.
    pub skip_lines: usize,
}

#[derive(Default)]
struct Tally {
    n: usize,
    at: Vec<usize>,
}

impl Tally {
    fn hit(&mut self, i: usize) {
        self.n += 1;
        if self.at.last() != Some(&i) && self.at.len() < 8 {
            self.at.push(i);
        }
    }
}

static MOJIBAKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"â€[™œ\x{9d}”“˜¦¢ž\x{80}-\x{9f}]|â€|Ã[\x{80}-\x{bf}]|Â[\x{a0}-\x{bf}]|ï»¿").unwrap()
});
pub(crate) static PAGE_NUM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:[-–—]\s*)?(?:[0-9]{1,4}|[ivxlc]{1,7})(?:\s*[-–—])?$").unwrap()
});
pub(crate) static PAGE_X_OF_Y: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)^(?:-\s*)?(?:page|p\.|pg\.?)\s*[0-9]{1,4}(?:\s*(?:of|/)\s*[0-9]{1,4})?(?:\s*-)?$|^[0-9]{1,4}\s*(?:of|/)\s*[0-9]{1,4}$",
    )
    .unwrap()
});
static NUMERIC_CELL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[(\-–−]?[$€£¥]?\s?[0-9][0-9,.\s]*%?\)?$|^(?i:nil|n/?a|-|–|—)$").unwrap()
});
static TOC_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\S.{2,}?(?:\s*\.{4,}\s*|\s*…{2,}\s*|\t+|\s{3,})(?:[0-9]{1,4}|[ivxlc]{1,6})$")
        .unwrap()
});
/// "Short title. 12": a TOC entry whose leader collapsed to one space. Only
/// counted inside a run whose page numbers never decrease.
static TOC_LOOSE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\S.{2,}\.\s(?P<page>[0-9]{1,4})$").unwrap());
static MD_TABLE_SEP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$").unwrap());

pub(crate) fn check_text(cx: &mut Cx, lines: &Lines, raw: &str, rules: &TextRules) {
    if rules.hygiene {
        line_endings(cx, raw);
    }
    if raw.starts_with('\u{feff}') {
        let (sev, msg) = if rules.bom_matters {
            (
                Severity::Warning,
                "file starts with a byte-order mark; the plain-text reader keeps it, so the first line will not match a heading pattern",
            )
        } else {
            (
                Severity::Info,
                "file starts with a byte-order mark (harmless here, but unnecessary in UTF-8)",
            )
        };
        cx.add(
            Finding::new("encoding.bom", sev, msg)
                .at(lines.loc(0))
                .fix("Auto-fix: strip the BOM."),
        );
    }

    let mut nul = Tally::default();
    let mut control = Tally::default();
    let mut bom_mid = Tally::default();
    let mut nbsp = Tally::default();
    let mut shy = Tally::default();
    let mut zw = Tally::default();
    let mut lig = Tally::default();
    let mut pua = Tally::default();
    let mut repl = Tally::default();
    let mut tabs = Tally::default();
    let mut mojibake = Tally::default();
    let mut hyphen = Tally::default();
    let mut page_nums = Tally::default();
    let mut pdf_inline_nums = Tally::default();
    let mut page_x = Tally::default();
    let mut long = Tally::default();
    let mut longest = 0usize;
    let mut trailing = 0usize;
    let mut non_empty = 0usize;
    let (mut dq_straight, mut dq_curly, mut sq_straight, mut sq_curly) = (0, 0, 0, 0);
    let mut furniture: HashMap<String, Vec<usize>> = HashMap::new();
    let mut in_fence = false;
    let mut col_run: Vec<usize> = Vec::new();
    let mut col_tables: Vec<(usize, usize)> = Vec::new();
    let mut short_run: Vec<usize> = Vec::new();
    let mut numeric_in_run = 0usize;
    let mut flat_tables: Vec<(usize, usize, usize)> = Vec::new();
    let mut md_table: Vec<usize> = Vec::new();
    // (line, page number) of each entry in the current run.
    let mut toc_run: Vec<(usize, Option<u32>)> = Vec::new();
    let mut toc_regions: Vec<(usize, usize, usize)> = Vec::new();
    let mut tables_suspected = 0usize;

    let n = lines.len();
    for i in rules.skip_lines..n {
        let line = lines.lines[i];
        let t = line.trim();
        if !t.is_empty() {
            non_empty += 1;
        }
        // Characters.
        let mut seen_text = false;
        for (col, c) in line.chars().enumerate() {
            match c {
                '\0' => nul.hit(i),
                '\t' if seen_text => tabs.hit(i),
                '\t' => {}
                '\n' | '\r' | '\u{c}' => {}
                '\u{feff}' if !(i == 0 && col == 0) => bom_mid.hit(i),
                '\u{feff}' => {}
                c if (c as u32) < 0x20 || ('\u{7f}'..='\u{9f}').contains(&c) => control.hit(i),
                '\u{a0}' | '\u{2000}'..='\u{200a}' | '\u{202f}' | '\u{205f}' | '\u{3000}' => {
                    nbsp.hit(i)
                }
                '\u{ad}' => shy.hit(i),
                '\u{200b}' | '\u{200c}' | '\u{200d}' | '\u{2060}' => zw.hit(i),
                '\u{fb00}'..='\u{fb06}' => lig.hit(i),
                '\u{e000}'..='\u{f8ff}' => pua.hit(i),
                '\u{fffd}' => repl.hit(i),
                '"' => dq_straight += 1,
                '\u{201c}' | '\u{201d}' => dq_curly += 1,
                '\'' => sq_straight += 1,
                '\u{2018}' | '\u{2019}' => sq_curly += 1,
                _ => {}
            }
            if !c.is_whitespace() {
                seen_text = true;
            }
        }
        if !line.is_ascii() && MOJIBAKE.is_match(line) {
            for _ in MOJIBAKE.find_iter(line) {
                mojibake.hit(i);
            }
        }

        if rules.markdown && t.starts_with("```") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence {
            continue;
        }

        if rules.hygiene {
            let len = line.chars().count();
            longest = longest.max(len);
            if len > 1000 {
                long.hit(i);
            }
            let te = line.trim_end_matches([' ', '\t']);
            // Two trailing spaces are a Markdown hard break.
            if te.len() != line.len()
                && !t.is_empty()
                && !(rules.markdown && line.len() - te.len() == 2)
            {
                trailing += 1;
            }
        }

        if rules.hyphenation
            && let Some(next) = lines.lines.get(i + 1)
            && is_hyphen_split(line, next)
        {
            hyphen.hit(i);
        }

        if rules.furniture && !t.is_empty() {
            if PAGE_X_OF_Y.is_match(t) {
                page_x.hit(i);
            } else if PAGE_NUM.is_match(t) {
                if lines.isolated(i) {
                    page_nums.hit(i);
                } else if lines.pdf && t.len() <= 4 && t.bytes().all(|b| b.is_ascii_digit()) {
                    pdf_inline_nums.hit(i);
                }
            } else if let Some(k) = furniture_key(t) {
                furniture.entry(k).or_default().push(i);
            }
        }

        // Table of contents: entries ending in a page number after dot leaders,
        // a tab or a wide gap. Blank lines do not break a run.
        if !t.is_empty() {
            let last_page = toc_run.iter().rev().find_map(|(_, p)| *p);
            let entry = if !t.chars().any(|c| c.is_alphabetic()) {
                None
            } else if TOC_LINE.is_match(t) {
                Some(None)
            } else {
                TOC_LOOSE
                    .captures(t)
                    .and_then(|c| c["page"].parse::<u32>().ok())
                    .filter(|p| last_page.is_none_or(|l| *p >= l))
                    .map(Some)
            };
            if let Some(page) = entry {
                toc_run.push((i, page));
            } else if toc_run.last().is_none_or(|(l, _)| i - l > 2) {
                flush_toc(&mut toc_run, &mut toc_regions);
            }
        }

        // Column layout: 3+ internal gaps of 2+ spaces (or tabs).
        if rules.layout_tables {
            if column_gaps(t) >= 3 {
                col_run.push(i);
            } else if !t.is_empty() || col_run.len() >= 3 {
                if col_run.len() >= 3 {
                    col_tables.push((col_run[0], col_run.len()));
                }
                col_run.clear();
            }
        }

        // Flattened tables: one short cell per line.
        let listish = t.starts_with("- ") || t.starts_with("* ") || t.starts_with('#');
        if !t.is_empty() {
            if t.chars().count() <= 30 && !listish && !t.starts_with('|') {
                short_run.push(i);
                if NUMERIC_CELL.is_match(t) {
                    numeric_in_run += 1;
                }
            } else {
                flush_short(&mut short_run, &mut numeric_in_run, &mut flat_tables);
            }
        }

        // Markdown tables.
        if rules.markdown {
            if t.starts_with('|') {
                md_table.push(i);
            } else if !md_table.is_empty() {
                check_md_table(cx, lines, &md_table);
                tables_suspected += 1;
                md_table.clear();
            }
        }
    }
    if col_run.len() >= 3 {
        col_tables.push((col_run[0], col_run.len()));
    }
    flush_toc(&mut toc_run, &mut toc_regions);
    flush_short(&mut short_run, &mut numeric_in_run, &mut flat_tables);
    if !md_table.is_empty() {
        check_md_table(cx, lines, &md_table);
        tables_suspected += 1;
    }

    // --- report ---------------------------------------------------------
    let tally = |cx: &mut Cx, t: &Tally, rule: &str, sev: Severity, what: &str, fix: &str| {
        if t.n == 0 {
            return;
        }
        let first = t.at[0];
        let mut f = Finding::new(
            rule,
            sev,
            format!("{} {}", plural(t.n, what), lines.where_(&t.at)),
        )
        .at(lines.loc(first))
        .excerpt(lines.lines[first]);
        if !fix.is_empty() {
            f = f.fix(fix);
        }
        cx.add(f);
    };
    tally(
        cx,
        &nul,
        "chars.nul",
        Severity::Error,
        "NUL character",
        "Auto-fix: remove NULs. Many NULs usually mean a UTF-16 or binary file.",
    );
    tally(
        cx,
        &control,
        "chars.control",
        Severity::Warning,
        "control character",
        "Auto-fix: remove control characters. C1 controls (U+0080–U+009F) usually mean Windows-1252 text decoded as Latin-1: re-convert the source instead.",
    );
    tally(
        cx,
        &bom_mid,
        "chars.bom",
        Severity::Warning,
        "byte-order mark inside the text (files concatenated?)",
        "Auto-fix: remove.",
    );
    tally(
        cx,
        &nbsp,
        "chars.nbsp",
        Severity::Info,
        "non-breaking or unusual space",
        "Auto-fix: replace with ordinary spaces.",
    );
    tally(
        cx,
        &shy,
        "chars.soft-hyphen",
        Severity::Warning,
        "soft hyphen (invisible; splits words for search)",
        "Auto-fix: remove.",
    );
    tally(
        cx,
        &zw,
        "chars.zero-width",
        Severity::Warning,
        "zero-width character (invisible; splits words for search)",
        "Auto-fix: remove.",
    );
    tally(
        cx,
        &lig,
        "chars.ligature",
        Severity::Warning,
        "ligature such as ﬁ/ﬂ (\"ﬁnancial\" will not match \"financial\")",
        "Auto-fix: expand to letters.",
    );
    tally(
        cx,
        &pua,
        "chars.private-use",
        Severity::Warning,
        "private-use character (symbol-font glyph with no meaning)",
        "Re-export with embedded Unicode fonts, or OCR the page; the real characters cannot be recovered from the text.",
    );
    tally(
        cx,
        &repl,
        "chars.replacement",
        Severity::Warning,
        "U+FFFD replacement character (text already lost in an earlier conversion)",
        "Re-convert from the original source.",
    );
    tally(
        cx,
        &mojibake,
        "encoding.mojibake",
        Severity::Warning,
        "double-encoded sequence such as \"â€™\" or \"Ã©\"",
        "The text was UTF-8 decoded as Windows-1252 and saved again. Re-export from the source, or repair with a tool such as `ftfy`.",
    );
    if rules.hygiene {
        tally(
            cx,
            &tabs,
            "whitespace.tabs",
            Severity::Info,
            "line with tabs inside prose",
            "Replace tabs with spaces; tabs inside text usually mark a flattened table or tab-aligned numbering.",
        );
        if !long.at.is_empty() {
            let sev = if longest > 20_000 {
                Severity::Warning
            } else {
                Severity::Info
            };
            tally(
                cx,
                &long,
                "lines.very-long",
                sev,
                "line over 1,000 characters",
                "Very long lines usually mean paragraphs were joined or the file has no line breaks; check that paragraphs are separated by blank lines.",
            );
        }
        if trailing >= 10 && trailing * 5 >= non_empty {
            cx.add(Finding::new(
                "whitespace.trailing",
                Severity::Info,
                format!("{trailing} of {non_empty} non-empty lines end in spaces or tabs"),
            ));
        }
    }
    for (what, s, c) in [
        ("double", dq_straight, dq_curly),
        ("single", sq_straight, sq_curly),
    ] {
        if s.min(c) >= 2 {
            cx.add(Finding::new(
                "quotes.mixed",
                Severity::Info,
                format!("both straight ({s}) and curly ({c}) {what} quotes are used; definitions match either, but a search for an exact quoted phrase may not"),
            ));
            break;
        }
    }
    if hyphen.n > 0 {
        let sev = if hyphen.n >= 3 {
            Severity::Warning
        } else {
            Severity::Info
        };
        let first = hyphen.at[0];
        let next = lines.lines.get(first + 1).copied().unwrap_or("");
        cx.add(
            Finding::new(
                "text.hyphenated-line-break",
                sev,
                format!(
                    "{} split across lines by a hyphen {} (\"assess-⏎able\" will not match \"assessable\")",
                    plural(hyphen.n, "word"),
                    lines.where_(&hyphen.at)
                ),
            )
            .at(lines.loc(first))
            .excerpt(&format!("{} ⏎ {}", lines.lines[first].trim(), next.trim()))
            .fix("Auto-fix: join lowercase hyphen splits (keeps the hyphen where the hyphenated form appears elsewhere in the text)."),
        );
    }
    if page_nums.n > 0 {
        let (sev, msg, fix) = if lines.pdf {
            (
                Severity::Info,
                "bare page-number line",
                "The PDF reader drops these automatically.",
            )
        } else {
            (
                Severity::Warning,
                "bare page-number line (page furniture; will become a stray heading or paragraph)",
                "Auto-fix: remove page-number lines that stand alone between blank lines.",
            )
        };
        tally(cx, &page_nums, "furniture.page-number", sev, msg, fix);
    }
    if pdf_inline_nums.n >= 3 {
        tally(
            cx,
            &pdf_inline_nums,
            "pdf.paragraph-numbers-dropped",
            Severity::Warning,
            "line holding only a number next to text; the PDF reader deletes every 1–4 digit line, so if these are paragraph or section numbers they will be lost",
            "Convert the PDF to text yourself (e.g. `pdftotext -layout`), join each number to its paragraph, and ingest the .txt.",
        );
    }
    tally(
        cx,
        &page_x,
        "furniture.page-x-of-y",
        Severity::Warning,
        "\"Page X of Y\" line",
        "Auto-fix: remove.",
    );

    if rules.furniture {
        let found = if lines.has_pages && lines.pages() >= 3 {
            furniture_by_page(lines, rules.skip_lines)
        } else {
            furniture_periodic(&furniture, lines)
        };
        let mut total = 0;
        for (key_line, count, desc) in found {
            total += count;
            cx.add(
                Finding::new(
                    "furniture.repeated-line",
                    Severity::Warning,
                    format!("this line repeats {desc}; it looks like a running header or footer and will be ingested into the text (or as a bogus heading)"),
                )
                .at(lines.loc(key_line))
                .excerpt(lines.lines[key_line].trim())
                .fix("Remove running headers/footers before ingesting (not auto-fixed because it deletes wording)."),
            );
        }
        cx.stat("furniture_lines", total);
    }

    for (start, end, len) in &toc_regions {
        if let (Some(a), Some(b)) = (lines.loc(*start).line, lines.loc(*end).line) {
            cx.toc_lines.push((a, b));
        }
        cx.add(
            Finding::new(
                "text.table-of-contents",
                Severity::Warning,
                format!("table of contents ({len} entries): each entry looks like a heading, so it becomes an empty provision or duplicates the real heading"),
            )
            .at(lines.loc(*start))
            .excerpt(lines.lines[*start].trim())
            .fix("Delete the table of contents before ingesting."),
        );
    }
    cx.stat(
        "toc_entries",
        toc_regions.iter().map(|(_, _, n)| n).sum::<usize>(),
    );
    for (start, len) in &col_tables {
        tables_suspected += 1;
        cx.add(
            Finding::new(
                "table.column-layout",
                Severity::Warning,
                format!("{len} consecutive lines laid out in columns: a table flattened to text; cells will be run together into one paragraph"),
            )
            .at(lines.loc(*start))
            .excerpt(lines.lines[*start].trim())
            .fix("Convert the table to a Markdown table (| a | b |) or keep it as an image/attachment; check the numbers survive."),
        );
    }
    for (start, len, numeric) in &flat_tables {
        tables_suspected += 1;
        cx.add(
            Finding::new(
                "table.flattened",
                Severity::Info,
                format!("{len} consecutive short lines ({numeric} numeric) look like table cells, one per line; rows and columns are lost"),
            )
            .at(lines.loc(*start))
            .excerpt(lines.lines[*start].trim())
            .fix("Rebuild the table as a Markdown table if its content matters."),
        );
    }
    cx.stat("tables_suspected", tables_suspected);
}

fn flush_toc(run: &mut Vec<(usize, Option<u32>)>, out: &mut Vec<(usize, usize, usize)>) {
    if run.len() >= 5 {
        out.push((run[0].0, run[run.len() - 1].0, run.len()));
    }
    run.clear();
}

fn flush_short(run: &mut Vec<usize>, numeric: &mut usize, out: &mut Vec<(usize, usize, usize)>) {
    if run.len() >= 8 && *numeric * 2 >= run.len() {
        out.push((run[0], run.len(), *numeric));
    }
    run.clear();
    *numeric = 0;
}

fn line_endings(cx: &mut Cx, raw: &str) {
    let b = raw.as_bytes();
    let (mut crlf, mut lf, mut cr) = (0usize, 0usize, 0usize);
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'\r' if b.get(i + 1) == Some(&b'\n') => {
                crlf += 1;
                i += 1;
            }
            b'\r' => cr += 1,
            b'\n' => lf += 1,
            _ => {}
        }
        i += 1;
    }
    cx.stat("crlf_line_endings", crlf);
    if cr > 0 && lf == 0 && crlf == 0 {
        cx.add(
            Finding::new(
                "whitespace.cr-line-endings",
                Severity::Error,
                format!("{cr} old-Mac CR-only line endings: the ingester sees the whole file as one line"),
            )
            .fix("Auto-fix: normalise line endings to LF."),
        );
    } else if (crlf > 0) as u8 + (lf > 0) as u8 + (cr > 0) as u8 > 1 {
        cx.add(
            Finding::new(
                "whitespace.mixed-line-endings",
                Severity::Warning,
                format!("mixed line endings: {crlf} CRLF, {lf} LF, {cr} CR (lone CRs do not break lines for the ingester)"),
            )
            .fix("Auto-fix: normalise line endings to LF."),
        );
    }
}

/// `assess-` at the end of `line` and a lowercase word starting `next`.
pub(crate) fn is_hyphen_split(line: &str, next: &str) -> bool {
    let l = line.trim_end();
    let Some(stem) = l.strip_suffix('-') else {
        return false;
    };
    let word: String = stem
        .chars()
        .rev()
        .take_while(|c| c.is_alphabetic())
        .collect();
    if word.is_empty() || !word.chars().all(|c| c.is_lowercase()) {
        return false;
    }
    // "--" or a word glued to digits is not a split word.
    let before = stem[..stem.len() - word.len()].chars().last();
    if before.is_some_and(|c| c == '-' || c.is_alphanumeric()) {
        return false;
    }
    next.trim_start()
        .chars()
        .next()
        .is_some_and(|c| c.is_lowercase())
}

/// Runs of 2+ spaces (or any tab) between text on a trimmed line.
fn column_gaps(t: &str) -> usize {
    let mut gaps = 0;
    let mut spaces = 0;
    let mut tab = false;
    for c in t.chars() {
        match c {
            ' ' => spaces += 1,
            '\t' => tab = true,
            _ => {
                if spaces >= 2 || tab {
                    gaps += 1;
                }
                spaces = 0;
                tab = false;
            }
        }
    }
    gaps
}

/// Normalised key for a candidate running header/footer line.
fn furniture_key(t: &str) -> Option<String> {
    let len = t.chars().count();
    if !(4..=90).contains(&len) {
        return None;
    }
    if t.starts_with(['#', '|', '>', '`', '-', '*', '+', '(', '[', '=']) {
        return None;
    }
    if t.chars().filter(|c| c.is_alphabetic()).count() < 3 {
        return None;
    }
    let mut k = String::with_capacity(t.len());
    let mut ws = false;
    for c in t.chars() {
        if c.is_whitespace() {
            if !ws {
                k.push(' ');
            }
            ws = true;
            continue;
        }
        ws = false;
        if c.is_ascii_digit() {
            if !k.ends_with('#') {
                k.push('#');
            }
        } else {
            k.extend(c.to_lowercase());
        }
    }
    Some(k)
}

/// Lines among the first/last three of most pages. Returns (line, count, description).
fn furniture_by_page(lines: &Lines, skip: usize) -> Vec<(usize, usize, String)> {
    let npages = lines.pages();
    let mut per_page: Vec<Vec<usize>> = vec![Vec::new(); npages + 1];
    for i in skip..lines.len() {
        if !lines.lines[i].trim().is_empty() {
            per_page[lines.page[i]].push(i);
        }
    }
    let mut seen: HashMap<String, (usize, usize)> = HashMap::new();
    let mut nonempty_pages = 0;
    for idxs in &per_page {
        if idxs.is_empty() {
            continue;
        }
        nonempty_pages += 1;
        let mut keys: Vec<(String, usize)> = Vec::new();
        let edge = idxs
            .iter()
            .take(3)
            .chain(idxs.iter().rev().take(3.min(idxs.len().saturating_sub(3))));
        for &i in edge {
            let t = lines.lines[i].trim();
            if PAGE_NUM.is_match(t) || PAGE_X_OF_Y.is_match(t) {
                continue;
            }
            if let Some(k) = furniture_key(t)
                && !keys.iter().any(|(x, _)| *x == k)
            {
                keys.push((k, i));
            }
        }
        for (k, i) in keys {
            let e = seen.entry(k).or_insert((i, 0));
            e.1 += 1;
        }
    }
    let threshold = 3.max(nonempty_pages * 3 / 10);
    let mut out: Vec<(usize, usize, String)> = seen
        .into_values()
        .filter(|(_, n)| *n >= threshold)
        .map(|(i, n)| {
            (
                i,
                n,
                format!("at the top or bottom of {n} of {nonempty_pages} pages"),
            )
        })
        .collect();
    out.sort();
    out
}

/// Short lines recurring at a regular interval next to a blank line (text
/// with no form feeds, e.g. PDF text saved without page breaks).
fn furniture_periodic(
    keys: &HashMap<String, Vec<usize>>,
    lines: &Lines,
) -> Vec<(usize, usize, String)> {
    let total = lines.len();
    let mut out = Vec::new();
    for occ in keys.values() {
        if occ.len() < 3 {
            continue;
        }
        let mut gaps: Vec<usize> = occ.windows(2).map(|w| w[1] - w[0]).collect();
        if gaps.iter().any(|g| *g < 15) {
            continue;
        }
        let span = occ[occ.len() - 1] - occ[0];
        if span * 10 < total * 3 {
            continue;
        }
        // Page furniture sits beside a blank line; a fragment of prose does not.
        let beside_blank = occ
            .iter()
            .filter(|i| lines.blank(i.checked_sub(1)) || lines.blank(Some(**i + 1)))
            .count();
        if beside_blank * 2 < occ.len() {
            continue;
        }
        gaps.sort_unstable();
        let m = gaps[gaps.len() / 2];
        let regular = gaps
            .iter()
            .filter(|g| g.abs_diff(m) * 100 <= m * 35)
            .count();
        if regular * 10 < gaps.len() * 8 {
            continue;
        }
        out.push((
            occ[0],
            occ.len(),
            format!("{} times, about every {m} lines", occ.len()),
        ));
    }
    out.sort();
    out
}

fn md_cells(row: &str) -> usize {
    let t = row.trim();
    let t = t.strip_prefix('|').unwrap_or(t);
    let t = t.strip_suffix('|').unwrap_or(t);
    let mut n = 1;
    let mut prev = '\0';
    for c in t.chars() {
        if c == '|' && prev != '\\' {
            n += 1;
        }
        prev = c;
    }
    n
}

fn check_md_table(cx: &mut Cx, lines: &Lines, rows: &[usize]) {
    let header = rows[0];
    let cols = md_cells(lines.lines[header]);
    let has_sep = rows
        .get(1)
        .is_some_and(|i| MD_TABLE_SEP.is_match(lines.lines[*i].trim()));
    if !has_sep {
        cx.add(
            Finding::new(
                "md.table-no-separator",
                Severity::Warning,
                "Markdown table has no header separator row (| --- | --- |) under its first row",
            )
            .at(lines.loc(header))
            .excerpt(lines.lines[header].trim())
            .fix("Add a separator row after the header row."),
        );
    }
    let body = if has_sep { &rows[2..] } else { &rows[1..] };
    let bad: Vec<usize> = body
        .iter()
        .copied()
        .filter(|i| md_cells(lines.lines[*i]) != cols)
        .collect();
    if let Some(&first) = bad.first() {
        cx.add(
            Finding::new(
                "md.table-columns",
                Severity::Warning,
                format!(
                    "table header has {cols} columns but {} have a different count (the row {} has {}); cells may have shifted or contain an unescaped |",
                    plural(bad.len(), "row"),
                    lines.where_(&[first]),
                    md_cells(lines.lines[first])
                ),
            )
            .at(lines.loc(first))
            .excerpt(lines.lines[first].trim())
            .fix("Make every row have the same number of cells; escape literal pipes as \\|."),
        );
    }
}

// ---------------------------------------------------------------------------
// Paragraphs and duplicates
// ---------------------------------------------------------------------------

/// Blank-line separated paragraphs with the location of their first line.
pub(crate) fn paragraphs_from_lines(lines: &Lines, skip: usize) -> Vec<(String, Loc)> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut start = 0;
    for i in skip..lines.len() {
        let t = lines.lines[i].trim();
        if t.is_empty() {
            if !cur.is_empty() {
                out.push((std::mem::take(&mut cur), lines.loc(start)));
            }
            continue;
        }
        if cur.is_empty() {
            start = i;
        } else {
            cur.push(' ');
        }
        cur.push_str(t);
    }
    if !cur.is_empty() {
        out.push((cur, lines.loc(start)));
    }
    out
}

/// Repeated paragraphs, by hash (linear). Statutes legitimately repeat short
/// subparagraphs, so only a run of 3+ consecutive repeated paragraphs (a pasted
/// or overlapping block) or a single repeated paragraph of 400+ characters is
/// reported.
pub(crate) fn check_duplicates(cx: &mut Cx, paras: &[(String, Loc)]) {
    let mut seen: HashMap<u64, usize> = HashMap::new();
    // For each paragraph, the index of its earlier twin.
    let mut twin: Vec<Option<usize>> = Vec::with_capacity(paras.len());
    for (i, (p, _)) in paras.iter().enumerate() {
        let norm: String = p.split_whitespace().collect::<Vec<_>>().join(" ");
        if norm.chars().count() < 40 {
            twin.push(None);
            continue;
        }
        let mut h = std::collections::hash_map::DefaultHasher::new();
        norm.hash(&mut h);
        let key = h.finish();
        match seen.get(&key) {
            Some(j) => twin.push(Some(*j)),
            None => {
                seen.insert(key, i);
                twin.push(None);
            }
        }
    }
    let mut reported = 0;
    let mut i = 0;
    while i < paras.len() {
        let Some(j) = twin[i] else {
            i += 1;
            continue;
        };
        // Extend the run while the twins are consecutive too.
        let mut len = 1;
        while i + len < paras.len() && twin[i + len] == Some(j + len) {
            len += 1;
        }
        let chars: usize = paras[i..i + len].iter().map(|(p, _)| p.len()).sum();
        if len >= 3 || chars >= 400 {
            reported += len;
            // Statutes repeat parallel provisions; long runs are more likely accidents.
            let sev = if len >= 5 || chars >= 1500 {
                Severity::Warning
            } else {
                Severity::Info
            };
            let (text, loc) = &paras[i];
            let first = paras[j].1;
            let where_first = match (first.line, first.page) {
                (Some(l), _) => format!("starting at line {l}"),
                (None, Some(p)) => format!("starting on page {p}"),
                _ => "earlier in the document".to_string(),
            };
            let what = if len == 1 {
                "this paragraph repeats one".to_string()
            } else {
                format!("{len} consecutive paragraphs repeat ones")
            };
            cx.add(
                Finding::new(
                    "text.duplicate-paragraph",
                    sev,
                    format!(
                        "{what} {where_first} word for word (double paste or overlapping export?)"
                    ),
                )
                .at(*loc)
                .excerpt(text)
                .fix("Delete the copy if it is accidental."),
            );
        }
        i += len;
    }
    cx.stat("duplicate_paragraphs", reported);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hyphen_split_detection() {
        assert!(is_hyphen_split("income is assess-", "able income"));
        assert!(!is_hyphen_split("income is Assess-", "able income"));
        assert!(!is_hyphen_split("see 8-", "1 above"));
        assert!(!is_hyphen_split("a dash --", "then"));
        assert!(!is_hyphen_split("income is assess-", "Able"));
    }

    #[test]
    fn decode_variants() {
        assert!(decode("héllo".as_bytes()).invalid_at.is_none());
        let d = decode(&[b'a', 0x92, b'b']);
        assert_eq!(d.invalid_at, Some(1));
        assert!(d.cp1252);
        let utf16: Vec<u8> = "\u{feff}hello world, this is text"
            .encode_utf16()
            .flat_map(|u| u.to_le_bytes())
            .collect();
        let d = decode(&utf16);
        assert!(d.utf16);
        assert!(d.text.contains("hello world"));
    }

    #[test]
    fn lines_pages() {
        let l = Lines::from_text("a\nb\n\u{c}c\nd\u{c}\ne\n", false);
        assert_eq!(l.lines, vec!["a", "b", "c", "d", "e"]);
        assert_eq!(l.page, vec![1, 1, 2, 2, 3]);
        assert!(l.has_pages);
    }

    #[test]
    fn md_cell_count() {
        assert_eq!(md_cells("| a | b | c |"), 3);
        assert_eq!(md_cells("a | b"), 2);
        assert_eq!(md_cells("| a \\| x | b |"), 2);
    }

    #[test]
    fn column_gap_count() {
        assert_eq!(column_gaps("Item  2019  2020  2021"), 3);
        assert_eq!(column_gaps("plain prose with single spaces"), 0);
    }
}
