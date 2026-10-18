//! PDF rules. Text comes from Poppler's `pdftotext -layout`, exactly as the
//! ingester gets it; `pdfinfo` adds page count, encryption and rotation when
//! it is installed.

use crate::text::{Lines, TextRules};
use crate::{
    Cx, Finding, LintOptions, Loc, Severity, list_nums, meta, plural, resolve_target, structure,
    text,
};
use anyhow::Result;
use meowlaw_ingest::Format;
use regex::Regex;
use std::collections::HashMap;
use std::io::Read;
use std::process::Command;
use std::sync::LazyLock;

const INSTALL_HINT: &str = "Install Poppler: `apt install poppler-utils` (Debian/Ubuntu), `dnf install poppler-utils` (Fedora), `brew install poppler` (macOS).";

pub(crate) fn lint(cx: &mut Cx, opts: &LintOptions) -> Result<()> {
    let mut head = [0u8; 1024];
    let n = std::fs::File::open(cx.path)?.read(&mut head)?;
    if !head[..n].windows(5).any(|w| w == b"%PDF-") {
        cx.add(
            Finding::new(
                "pdf.not-pdf",
                Severity::Error,
                "file has a .pdf extension but no PDF header",
            )
            .fix("Check the download: it may be an HTML error page or a different format."),
        );
        return Ok(());
    }
    let manifest = meta::collect(cx, None, false);
    let (profile, kind) = resolve_target(cx, opts, &manifest)?;

    let info = pdfinfo(cx);
    let out = match Command::new("pdftotext")
        .args(["-layout", "-enc", "UTF-8"])
        .arg(cx.path)
        .arg("-")
        .output()
    {
        Ok(o) => o,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            cx.add(
                Finding::new(
                    "pdf.pdftotext-missing",
                    Severity::Error,
                    "`pdftotext` is not on PATH: PDFs cannot be linted or ingested without it",
                )
                .fix(INSTALL_HINT),
            );
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let lower = err.to_lowercase();
        if lower.contains("password") || lower.contains("permission") || lower.contains("encrypt") {
            cx.add(
                Finding::new(
                    "pdf.encrypted",
                    Severity::Error,
                    format!("PDF is encrypted and text cannot be extracted: {err}"),
                )
                .fix("Remove the protection first, e.g. `qpdf --decrypt in.pdf out.pdf` (with the password if it has one)."),
            );
        } else {
            cx.add(
                Finding::new(
                    "pdf.extract-failed",
                    Severity::Error,
                    format!("pdftotext failed: {err}"),
                )
                .fix("The file may be damaged; try `qpdf in.pdf repaired.pdf` or re-download it."),
            );
        }
        return Ok(());
    }
    let raw = String::from_utf8_lossy(&out.stdout).into_owned();

    if let Some(i) = &info {
        if i.encrypted {
            cx.add(
                Finding::new(
                    "pdf.encrypted",
                    Severity::Info,
                    "PDF is encrypted (copy/print restrictions); text was still extracted, but other tools may refuse it",
                )
                .fix("`qpdf --decrypt in.pdf out.pdf` removes the restrictions."),
            );
        }
        if i.rotation % 360 != 0 {
            cx.add(Finding::new(
                "pdf.rotated",
                Severity::Info,
                format!(
                    "pages are rotated {}°; check the extracted text reads in order",
                    i.rotation
                ),
            ));
        }
        if OCR_PRODUCER.is_match(&i.producer) {
            cx.add(Finding::new(
                "pdf.ocr",
                Severity::Info,
                format!("text layer was produced by OCR ({}): expect misread characters (l/1, O/0, rn/m) in numbers and citations", i.producer.trim()),
            ));
        }
    }

    // Pages: pdftotext ends every page with a form feed.
    let mut pages: Vec<&str> = raw.split('\u{c}').collect();
    let expected = info.as_ref().map(|i| i.pages).filter(|p| *p > 0);
    if pages.len() > 1
        && pages.last().is_some_and(|p| p.trim().is_empty())
        && expected.is_none_or(|e| pages.len() > e)
    {
        pages.pop();
    }
    let npages = expected.unwrap_or(pages.len()).max(pages.len());
    cx.stat("pages", npages);
    let dens: Vec<usize> = pages
        .iter()
        .map(|p| p.chars().filter(|c| !c.is_whitespace()).count())
        .collect();
    let total: usize = dens.iter().sum();
    cx.stat("chars", raw.chars().count());
    cx.stat("text_chars", total);
    cx.stat("chars_per_page", total / npages.max(1));
    cx.stat("lines", raw.lines().count());
    if total == 0 {
        cx.add(
            Finding::new(
                "pdf.no-text-layer",
                Severity::Error,
                format!("none of the {} has any text: this is a scanned (image-only) PDF", plural(npages, "page")),
            )
            .fix("Run OCR first, e.g. `ocrmypdf --language eng in.pdf out.pdf`, then lint the output."),
        );
        return Ok(());
    }

    let empty: Vec<usize> = (0..npages)
        .filter(|i| dens.get(*i).copied().unwrap_or(0) == 0)
        .map(|i| i + 1)
        .collect();
    if !empty.is_empty() {
        let sev = if empty.len() <= 2 && empty.len() * 10 <= npages {
            Severity::Info
        } else {
            Severity::Warning
        };
        cx.add(
            Finding::new(
                "pdf.empty-pages",
                sev,
                format!(
                    "{} of {} {} no text (pages {}): blank, or scanned images whose text will be missing",
                    empty.len(),
                    npages,
                    if empty.len() == 1 { "page has" } else { "pages have" },
                    list_nums(&empty, 12)
                ),
            )
            .at(Loc {
                line: None,
                page: Some(empty[0]),
            })
            .fix("If they hold text, OCR them: `ocrmypdf --skip-text in.pdf out.pdf` only touches pages without text."),
        );
    }
    let low: Vec<usize> = dens
        .iter()
        .enumerate()
        .filter(|(_, d)| (1..150).contains(*d))
        .map(|(i, _)| i + 1)
        .collect();
    if !low.is_empty() && npages >= 3 {
        let sev = if low.len() * 10 >= npages * 3 {
            Severity::Warning
        } else {
            Severity::Info
        };
        cx.add(
            Finding::new(
                "pdf.low-text-density",
                sev,
                format!(
                    "{} {} under 150 characters of text (pages {}; average {} per page): cover pages, or partly scanned content",
                    plural(low.len(), "page"),
                    if low.len() == 1 { "has" } else { "have" },
                    list_nums(&low, 12),
                    total / npages.max(1)
                ),
            )
            .at(Loc {
                line: None,
                page: Some(low[0]),
            }),
        );
    }

    let mut garbled = Vec::new();
    let mut two_col = Vec::new();
    for (i, p) in pages.iter().enumerate() {
        if is_garbled(p) {
            garbled.push(i + 1);
        }
        if is_two_column(p) {
            two_col.push(i + 1);
        }
    }
    if !garbled.is_empty() {
        cx.add(
            Finding::new(
                "pdf.garbled",
                Severity::Warning,
                format!(
                    "text on {} looks letter-spaced, rotated or garbled (many single-letter tokens or few letters): pages {}",
                    plural(garbled.len(), "page"),
                    list_nums(&garbled, 12)
                ),
            )
            .at(Loc {
                line: None,
                page: Some(garbled[0]),
            })
            .fix("Check those pages by eye; re-OCR them (`ocrmypdf --force-ocr`) if the text is unusable."),
        );
    }
    if !two_col.is_empty() {
        cx.add(
            Finding::new(
                "pdf.two-column",
                Severity::Warning,
                format!(
                    "two-column layout on {} (pages {}): the reader extracts line by line across both columns, interleaving their sentences",
                    plural(two_col.len(), "page"),
                    list_nums(&two_col, 12)
                ),
            )
            .at(Loc {
                line: None,
                page: Some(two_col[0]),
            })
            .fix("Extract each column separately (`pdftotext -x/-y/-W/-H` crop boxes) or convert with a layout-aware tool, then ingest the text."),
        );
    }
    cx.stat("two_column_pages", two_col.len());

    let lines = Lines::from_text(&raw, true);
    let rules = TextRules {
        hygiene: false,
        furniture: true,
        hyphenation: true,
        layout_tables: true,
        markdown: false,
        bom_matters: false,
        skip_lines: 0,
    };
    text::check_text(cx, &lines, &raw, &rules);
    text::check_duplicates(cx, &text::paragraphs_from_lines(&lines, 0));

    // What the ingester structures: page breaks become blank lines and bare
    // 1-4 digit lines are dropped.
    let mut kept: Vec<&str> = Vec::with_capacity(lines.len());
    let mut kept_page: Vec<usize> = Vec::with_capacity(lines.len());
    for (i, l) in lines.lines.iter().enumerate() {
        if i > 0 && lines.page[i] != lines.page[i - 1] {
            kept.push("");
            kept_page.push(lines.page[i]);
        }
        let t = l.trim();
        if !t.is_empty() && t.len() <= 4 && t.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        kept.push(l);
        kept_page.push(lines.page[i]);
    }
    let cleaned = kept.join("\n");
    let cleaned_lines = Lines {
        lines: kept,
        page: kept_page,
        has_pages: true,
        pdf: true,
        no_loc: false,
    };
    let blocks = meowlaw_ingest::blocks::plain_text(&cleaned);
    let sblocks = structure::locate(blocks, &cleaned_lines, 0);
    structure::check(
        cx,
        &sblocks,
        &profile,
        kind,
        Format::Pdf,
        manifest.content.as_deref() == Some("summary"),
    );
    Ok(())
}

static OCR_PRODUCER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)ocrmypdf|tesseract|abbyy|finereader|omnipage|readiris|paper ?capture|clearscan|scan",
    )
    .unwrap()
});

struct Info {
    pages: usize,
    encrypted: bool,
    rotation: i64,
    producer: String,
}

fn pdfinfo(cx: &mut Cx) -> Option<Info> {
    let out = match Command::new("pdfinfo").arg(cx.path).output() {
        Ok(o) => o,
        Err(_) => {
            cx.add(
                Finding::new(
                    "pdf.pdfinfo-missing",
                    Severity::Info,
                    "`pdfinfo` is not on PATH: page count and encryption are inferred from the extracted text",
                )
                .fix(INSTALL_HINT),
            );
            return None;
        }
    };
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout);
    let mut kv: HashMap<&str, &str> = HashMap::new();
    for l in s.lines() {
        if let Some((k, v)) = l.split_once(':') {
            kv.insert(k.trim(), v.trim());
        }
    }
    let producer = [kv.get("Producer"), kv.get("Creator")]
        .into_iter()
        .flatten()
        .copied()
        .collect::<Vec<_>>()
        .join(" / ");
    let info = Info {
        pages: kv.get("Pages").and_then(|v| v.parse().ok()).unwrap_or(0),
        encrypted: kv.get("Encrypted").is_some_and(|v| v.starts_with("yes")),
        rotation: kv.get("Page rot").and_then(|v| v.parse().ok()).unwrap_or(0),
        producer,
    };
    if !info.producer.is_empty() {
        cx.stat("producer", info.producer.clone());
    }
    cx.stat("encrypted", info.encrypted);
    Some(info)
}

/// Letter-spaced ("S e c t i o n"), rotated or symbol-font text.
fn is_garbled(page: &str) -> bool {
    let tokens: Vec<&str> = page.split_whitespace().collect();
    if tokens.len() >= 40 {
        let singles = tokens
            .iter()
            .filter(|t| {
                let mut c = t.chars();
                c.next().is_some_and(|c| c.is_alphabetic()) && c.next().is_none()
            })
            .count();
        if singles * 100 > tokens.len() * 35 {
            return true;
        }
    }
    let visible = page.chars().filter(|c| !c.is_whitespace()).count();
    let letters = page.chars().filter(|c| c.is_alphabetic()).count();
    visible >= 200 && letters * 10 < visible * 4
}

/// Most lines split by one wide gap at a consistent column.
fn is_two_column(page: &str) -> bool {
    let mut nonempty = 0usize;
    let mut cols: Vec<usize> = Vec::new();
    for line in page.lines() {
        let chars: Vec<char> = line.trim_end().chars().collect();
        let lead = chars.iter().take_while(|c| c.is_whitespace()).count();
        if lead == chars.len() {
            continue;
        }
        nonempty += 1;
        let mut gaps: Vec<(usize, usize)> = Vec::new();
        let mut run_start = None;
        for (i, c) in chars.iter().enumerate().skip(lead) {
            if *c == ' ' {
                run_start.get_or_insert(i);
            } else if let Some(s) = run_start.take()
                && i - s >= 3
            {
                gaps.push((s, i));
            }
        }
        if let [(s, e)] = gaps[..] {
            let left = chars[lead..s].iter().filter(|c| !c.is_whitespace()).count();
            let right = chars[e..].iter().filter(|c| !c.is_whitespace()).count();
            if left >= 15 && right >= 15 {
                cols.push(e);
            }
        }
    }
    if cols.len() < 8 || cols.len() * 100 < nonempty * 35 {
        return false;
    }
    let mut sorted = cols.clone();
    sorted.sort_unstable();
    let m = sorted[sorted.len() / 2];
    let near = cols.iter().filter(|c| c.abs_diff(m) <= 6).count();
    near * 10 >= cols.len() * 6
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn garbled_detection() {
        let spaced = "S e c t i o n o n e a p p l i e s t o t h e t a x p a y e r w h o i s r e s i d e n t i n A u s t r a l i a d u r i n g t h e y e a r";
        assert!(is_garbled(spaced));
        assert!(!is_garbled(
            "Section 8-1 applies to a taxpayer who is resident in Australia during the year of income and who incurs a loss or outgoing in gaining or producing assessable income, and the loss is not capital in nature."
        ));
    }

    #[test]
    fn two_column_detection() {
        let left = "The Commissioner may make a determination";
        let right = "under this section if the taxpayer applies";
        let page: String = (0..12)
            .map(|_| format!("{left}        {right}\n"))
            .collect();
        assert!(is_two_column(&page));
        assert!(!is_two_column(&format!("{left} {right}\n").repeat(12)));
    }
}
