//! DOCX rules: what Word keeps outside the text the ingester reads.
//!
//! The ingester reads `w:t` runs from `word/document.xml` and treats only
//! paragraphs styled `Heading N` / `Title` as headings. Automatic numbering,
//! footnotes, comments and tracked deletions all live elsewhere.

use crate::text::{Lines, TextRules};
use crate::{Cx, Finding, LintOptions, Severity, meta, plural, resolve_target, structure, text};
use anyhow::Result;
use meowlaw_core::CorpusKind;
use meowlaw_ingest::Format;
use quick_xml::events::{BytesStart, Event};
use std::collections::HashMap;
use std::io::Read;

type Zip = zip::ZipArchive<std::fs::File>;

fn read_part(zip: &mut Zip, name: &str) -> Option<String> {
    let mut f = zip.by_name(name).ok()?;
    let mut s = String::new();
    f.read_to_string(&mut s).ok()?;
    Some(s)
}

fn attr(e: &BytesStart, local: &str) -> Option<String> {
    e.attributes()
        .flatten()
        .find(|a| a.key.local_name().as_ref() == local)
        .map(|a| a.value.to_string())
}

/// The ingester's heading-style rule.
fn heading_style_depth(style: &str) -> Option<u8> {
    let s = style.to_ascii_lowercase().replace([' ', '-', '_'], "");
    if s == "title" {
        return Some(1);
    }
    s.strip_prefix("heading")
        .and_then(|n| n.parse::<u8>().ok())
        .filter(|d| (1..=9).contains(d))
}

#[derive(Default, Clone)]
struct Style {
    name: String,
    based_on: Option<String>,
    num: Option<(String, String)>,
}

#[derive(Default)]
struct Para {
    style: Option<String>,
    num: Option<(String, String)>,
    text_len: usize,
}

#[derive(Default)]
struct Scan {
    paras: Vec<Para>,
    ins: usize,
    del: usize,
    moves: usize,
    format_changes: usize,
    tables: usize,
    images: usize,
    text_boxes: usize,
    fields: usize,
    toc: bool,
    hidden_runs: usize,
}

fn scan_document(xml: &str) -> Result<Scan, String> {
    let mut r = quick_xml::Reader::from_str(xml);
    let mut s = Scan::default();
    let mut stack: Vec<String> = Vec::new();
    let mut cur: Option<Para> = None;
    let mut num_id: Option<String> = None;
    let mut ilvl: Option<String> = None;
    loop {
        let ev = r
            .read_event()
            .map_err(|e| format!("{e} (at byte {})", r.error_position()))?;
        let (e, empty) = match &ev {
            Event::Start(e) => (Some(e.clone()), false),
            Event::Empty(e) => (Some(e.clone()), true),
            _ => (None, false),
        };
        if let Some(e) = e {
            let name = e.local_name().as_ref().to_string();
            let in_ppr = stack.iter().any(|n| n == "pPr");
            match name.as_str() {
                "p" => {
                    cur = Some(Para::default());
                    num_id = None;
                    ilvl = None;
                }
                "pStyle" if in_ppr => {
                    if let Some(p) = cur.as_mut() {
                        p.style = attr(&e, "val");
                    }
                }
                "numId" if in_ppr => num_id = attr(&e, "val"),
                "ilvl" if in_ppr => ilvl = attr(&e, "val"),
                "ins" => s.ins += 1,
                "del" => s.del += 1,
                "moveFrom" | "moveTo" => s.moves += 1,
                "rPrChange" | "pPrChange" | "sectPrChange" | "tblPrChange" => s.format_changes += 1,
                "tbl" => s.tables += 1,
                "drawing" | "pict" | "object" => s.images += 1,
                "txbxContent" => s.text_boxes += 1,
                "fldSimple" => {
                    s.fields += 1;
                    if attr(&e, "instr").is_some_and(|i| i.trim_start().starts_with("TOC")) {
                        s.toc = true;
                    }
                }
                "fldChar" if attr(&e, "fldCharType").as_deref() == Some("begin") => s.fields += 1,
                "fldChar" => {}
                "vanish" => {
                    let n = stack.len();
                    if n >= 2 && stack[n - 1] == "rPr" && stack[n - 2] == "r" {
                        s.hidden_runs += 1;
                    }
                }
                _ => {}
            }
            if !empty {
                stack.push(name);
            }
            continue;
        }
        match ev {
            Event::Text(t) => {
                let top = stack.last().map(String::as_str);
                if top == Some("t") {
                    if let Some(p) = cur.as_mut() {
                        p.text_len += t.xml10_content().trim().len();
                    }
                } else if top == Some("instrText")
                    && t.xml10_content().trim_start().starts_with("TOC")
                {
                    s.toc = true;
                }
            }
            Event::End(e) => {
                let name = e.local_name();
                if name.as_ref() == "numPr"
                    && let (Some(p), Some(id)) = (cur.as_mut(), num_id.clone())
                {
                    p.num = Some((id, ilvl.clone().unwrap_or_else(|| "0".into())));
                }
                if name.as_ref() == "p"
                    && let Some(p) = cur.take()
                {
                    s.paras.push(p);
                }
                stack.pop();
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(s)
}

fn scan_styles(xml: &str) -> HashMap<String, Style> {
    let mut out = HashMap::new();
    let mut r = quick_xml::Reader::from_str(xml);
    let mut cur: Option<(String, Style)> = None;
    let mut num_id: Option<String> = None;
    let mut ilvl: Option<String> = None;
    while let Ok(ev) = r.read_event() {
        match ev {
            Event::Start(e) | Event::Empty(e) => match e.local_name().as_ref() {
                "style" => {
                    if let Some((id, st)) = cur.take() {
                        out.insert(id, st);
                    }
                    cur = attr(&e, "styleId").map(|id| (id, Style::default()));
                    num_id = None;
                    ilvl = None;
                }
                "name" => {
                    if let Some((_, st)) = cur.as_mut() {
                        st.name = attr(&e, "val").unwrap_or_default();
                    }
                }
                "basedOn" => {
                    if let Some((_, st)) = cur.as_mut() {
                        st.based_on = attr(&e, "val");
                    }
                }
                "numId" => num_id = attr(&e, "val"),
                "ilvl" => ilvl = attr(&e, "val"),
                _ => {}
            },
            Event::End(e) => match e.local_name().as_ref() {
                "numPr" => {
                    if let (Some((_, st)), Some(id)) = (cur.as_mut(), num_id.clone()) {
                        st.num = Some((id, ilvl.clone().unwrap_or_else(|| "0".into())));
                    }
                }
                "style" => {
                    if let Some((id, st)) = cur.take() {
                        out.insert(id, st);
                    }
                }
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }
    out
}

/// numId → abstractNumId, and (abstractNumId, ilvl) → numFmt.
fn scan_numbering(xml: &str) -> impl Fn(&str, &str) -> Option<String> {
    let mut num_to_abs: HashMap<String, String> = HashMap::new();
    let mut fmt: HashMap<(String, String), String> = HashMap::new();
    let mut r = quick_xml::Reader::from_str(xml);
    let mut abs: Option<String> = None;
    let mut num: Option<String> = None;
    let mut lvl: Option<String> = None;
    while let Ok(ev) = r.read_event() {
        match ev {
            Event::Start(e) | Event::Empty(e) => match e.local_name().as_ref() {
                "abstractNum" => abs = attr(&e, "abstractNumId"),
                "lvl" => lvl = attr(&e, "ilvl"),
                "numFmt" => {
                    if let (Some(a), Some(l), Some(v)) = (abs.clone(), lvl.clone(), attr(&e, "val"))
                    {
                        fmt.insert((a, l), v);
                    }
                }
                "num" => num = attr(&e, "numId"),
                "abstractNumId" => {
                    if let (Some(n), Some(v)) = (num.clone(), attr(&e, "val")) {
                        num_to_abs.insert(n, v);
                    }
                }
                _ => {}
            },
            Event::End(e) => match e.local_name().as_ref() {
                "abstractNum" => abs = None,
                "num" => num = None,
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }
    move |num_id: &str, ilvl: &str| {
        let a = num_to_abs.get(num_id)?;
        fmt.get(&(a.clone(), ilvl.to_string())).cloned()
    }
}

/// Count real notes (ids above the separator notes) in footnotes/endnotes.
fn count_notes(xml: &str, element: &str) -> usize {
    let mut r = quick_xml::Reader::from_str(xml);
    let mut n = 0;
    while let Ok(ev) = r.read_event() {
        match ev {
            Event::Start(e) | Event::Empty(e) if e.local_name().as_ref() == element => {
                let ty = attr(&e, "type");
                if ty.is_none() || ty.as_deref() == Some("normal") {
                    n += 1;
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    n
}

pub(crate) fn lint(cx: &mut Cx, opts: &LintOptions) -> Result<()> {
    let file = std::fs::File::open(cx.path)?;
    let mut zip = match zip::ZipArchive::new(file) {
        Ok(z) => z,
        Err(e) => {
            let mut head = [0u8; 8];
            let n = std::fs::File::open(cx.path)?.read(&mut head)?;
            let legacy = head[..n].starts_with(&[0xD0, 0xCF, 0x11, 0xE0]);
            let msg = if legacy {
                "this is a legacy Word 97-2003 .doc file renamed to .docx; it cannot be read"
                    .to_string()
            } else {
                format!("not a valid DOCX (ZIP) container: {e}")
            };
            cx.add(
                Finding::new("docx.not-zip", Severity::Error, msg)
                    .fix("Open it in Word or LibreOffice and save as .docx (`soffice --headless --convert-to docx in.doc`)."),
            );
            return Ok(());
        }
    };
    let Some(doc_xml) = read_part(&mut zip, "word/document.xml") else {
        cx.add(
            Finding::new(
                "docx.no-document-xml",
                Severity::Error,
                "ZIP has no word/document.xml: not a Word document (an .xlsx, .pptx or .odt renamed?)",
            )
            .fix("Save it from Word/LibreOffice as a Word document (.docx)."),
        );
        return Ok(());
    };
    let scan = match scan_document(&doc_xml) {
        Ok(s) => s,
        Err(e) => {
            cx.add(
                Finding::new(
                    "docx.xml-parse",
                    Severity::Error,
                    format!("word/document.xml is not well-formed XML; ingest will fail: {e}"),
                )
                .fix("Open and re-save the file in Word; it may be truncated or corrupted."),
            );
            return Ok(());
        }
    };
    let styles = read_part(&mut zip, "word/styles.xml")
        .map(|s| scan_styles(&s))
        .unwrap_or_default();
    let numbering = read_part(&mut zip, "word/numbering.xml").unwrap_or_default();
    let num_fmt = scan_numbering(&numbering);
    let comments = read_part(&mut zip, "word/comments.xml")
        .map(|s| count_notes(&s, "comment"))
        .unwrap_or(0);
    let footnotes = read_part(&mut zip, "word/footnotes.xml")
        .map(|s| count_notes(&s, "footnote"))
        .unwrap_or(0);
    let endnotes = read_part(&mut zip, "word/endnotes.xml")
        .map(|s| count_notes(&s, "endnote"))
        .unwrap_or(0);

    let manifest = meta::collect(cx, None, false);
    let (profile, kind) = resolve_target(cx, opts, &manifest)?;

    cx.stat("paragraphs", scan.paras.len());
    cx.stat("tables", scan.tables);
    cx.stat("images", scan.images);
    cx.stat("footnotes", footnotes + endnotes);
    cx.stat("comments", comments);

    let text_len: usize = scan.paras.iter().map(|p| p.text_len).sum();
    if text_len == 0 {
        if scan.images > 0 {
            cx.add(
                Finding::new(
                    "docx.images-only",
                    Severity::Error,
                    format!(
                        "the document has {} but no text: a scan pasted into Word",
                        plural(scan.images, "image")
                    ),
                )
                .fix("OCR the original scan (e.g. export to PDF and run `ocrmypdf`)."),
            );
        } else {
            cx.add(Finding::new(
                "file.empty",
                Severity::Error,
                "document has no text",
            ));
        }
        return Ok(());
    }

    // Tracked changes.
    let tracked = scan.ins + scan.del + scan.moves;
    if tracked > 0 {
        cx.add(
            Finding::new(
                "docx.tracked-changes",
                Severity::Error,
                format!(
                    "{} unresolved tracked changes ({} insertions, {} deletions, {} moves): the ingester reads the text as if every change were accepted, so proposed wording becomes the law",
                    tracked, scan.ins, scan.del, scan.moves
                ),
            )
            .fix("In Word: Review → Accept/Reject all changes (decide which version is authoritative), then save."),
        );
    } else if scan.format_changes > 0 {
        cx.add(Finding::new(
            "docx.tracked-changes",
            Severity::Info,
            format!(
                "{} tracked formatting changes (text is unaffected)",
                scan.format_changes
            ),
        ));
    }
    if comments > 0 {
        cx.add(
            Finding::new(
                "docx.comments",
                Severity::Warning,
                format!("{} present; they are not ingested (if they matter, they are lost; if they are drafting notes, check none were typed into the text itself)", plural(comments, "comment")),
            )
            .fix("Delete resolved comments, or move important ones into the text or metadata."),
        );
    }

    // Styles: which paragraphs the ingester will see as headings.
    let resolve_style = |id: &str| -> (String, Option<(String, String)>) {
        let mut name = String::new();
        let mut num = None;
        let mut cur = Some(id.to_string());
        for _ in 0..10 {
            let Some(c) = cur else { break };
            let Some(st) = styles.get(&c) else { break };
            if name.is_empty() {
                name = st.name.clone();
            }
            if num.is_none() {
                num = st.num.clone();
            }
            cur = st.based_on.clone();
        }
        (name, num)
    };
    let mut heading_paras = 0usize;
    let mut custom: HashMap<String, usize> = HashMap::new();
    let mut numbered = 0usize;
    let mut numbered_headings = 0usize;
    for p in &scan.paras {
        if p.text_len == 0 {
            continue;
        }
        let (is_heading, style_num) = match &p.style {
            Some(id) => {
                let (name, num) = resolve_style(id);
                let h = heading_style_depth(id).is_some();
                if !h {
                    let lname = name.to_lowercase();
                    let lid = id.to_lowercase();
                    if heading_style_depth(&name).is_some()
                        || lname.contains("heading")
                        || lid.contains("head")
                        || lname.contains("head")
                    {
                        *custom.entry(id.clone()).or_default() += 1;
                    }
                }
                (h, num)
            }
            None => (false, None),
        };
        if is_heading {
            heading_paras += 1;
        }
        let num = p.num.clone().or(style_num);
        if let Some((id, lvl)) = num
            && id != "0"
        {
            let f = num_fmt(&id, &lvl);
            // Bullets carry no number; unknown formats are assumed numbered.
            if !matches!(f.as_deref(), Some("bullet") | Some("none")) {
                numbered += 1;
                if is_heading {
                    numbered_headings += 1;
                }
            }
        }
    }
    cx.stat("heading_style_paragraphs", heading_paras);
    cx.stat("auto_numbered_paragraphs", numbered);
    if heading_paras == 0 {
        cx.add(
            Finding::new(
                "docx.no-heading-styles",
                Severity::Warning,
                "no paragraph uses a Heading 1–9 or Title style: the DOCX reader only treats styled paragraphs as headings, so no headings or provisions will be detected however the text looks",
            )
            .fix("Apply Heading styles to headings in Word, or save as plain text (.txt) so the profile's heading patterns are applied to each line."),
        );
    }
    if !custom.is_empty() {
        let mut v: Vec<_> = custom.into_iter().collect();
        v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        let list = v
            .iter()
            .take(8)
            .map(|(s, n)| format!("{s} ({n})"))
            .collect::<Vec<_>>()
            .join(", ");
        cx.add(
            Finding::new(
                "docx.custom-heading-styles",
                Severity::Warning,
                format!("paragraphs use heading-like styles the ingester does not recognise (it only knows style ids Heading1–9 and Title): {list}"),
            )
            .fix("Map these styles to built-in Heading styles in Word (or base them on them and rename), or convert to text."),
        );
    }
    if numbered > 0 {
        let sev = if numbered_headings > 0 || kind == Some(CorpusKind::Case) {
            Severity::Error
        } else {
            Severity::Warning
        };
        cx.add(
            Finding::new(
                "docx.auto-numbering",
                sev,
                format!(
                    "{} use Word automatic numbering{}. Automatic numbers are generated by Word when displayed and are NOT in document.xml, so the ingester never sees them: section and paragraph numbers will be missing{}",
                    plural(numbered, "paragraph"),
                    if numbered_headings > 0 {
                        format!(" ({numbered_headings} of them headings)")
                    } else {
                        String::new()
                    },
                    if kind == Some(CorpusKind::Case) {
                        " and judgment paragraphs cannot be split"
                    } else {
                        ""
                    }
                ),
            )
            .fix("Convert numbering to plain text before ingesting: in Word run the macro `ActiveDocument.ConvertNumbersToText`, or save as .txt/.pdf (both keep the numbers)."),
        );
    }
    if scan.fields > 0 {
        let msg = if scan.toc {
            format!(
                "{} field codes including a table of contents: field results are read as last updated, and TOC entries become paragraphs that duplicate the headings",
                scan.fields
            )
        } else {
            format!(
                "{} field codes (cross-references, dates, page numbers): their last-updated results are ingested as text",
                scan.fields
            )
        };
        cx.add(
            Finding::new("docx.fields", Severity::Info, msg)
                .fix("Update all fields (Ctrl+A, F9) before export; delete the table of contents."),
        );
    }
    if footnotes + endnotes > 0 {
        cx.add(
            Finding::new(
                "docx.footnotes",
                Severity::Warning,
                format!(
                    "{} and {} present: notes are not ingested, only their reference marks",
                    plural(footnotes, "footnote"),
                    plural(endnotes, "endnote")
                ),
            )
            .fix("If the notes matter (e.g. legislative history), convert them to inline text or a Notes section."),
        );
    }
    if scan.tables > 0 {
        cx.add(
            Finding::new(
                "docx.tables",
                Severity::Info,
                format!("{}: each cell becomes its own paragraph, so rows and columns are lost", plural(scan.tables, "table")),
            )
            .fix("Check tables (rates, thresholds) survive; consider converting them to Markdown tables."),
        );
    }
    if scan.text_boxes > 0 {
        cx.add(
            Finding::new(
                "docx.text-boxes",
                Severity::Warning,
                format!("{}: their text is spliced into whichever paragraph anchors the box, out of reading order", plural(scan.text_boxes, "text box")),
            )
            .fix("Move text out of text boxes into the body."),
        );
    }
    if scan.hidden_runs > 0 {
        cx.add(
            Finding::new(
                "docx.hidden-text",
                Severity::Warning,
                format!(
                    "{} hidden text runs: hidden text is ingested as if it were visible",
                    scan.hidden_runs
                ),
            )
            .fix("Show hidden text (File → Options → Display) and delete or unhide it."),
        );
    }

    // The text exactly as the ingester reads it.
    let blocks = match meowlaw_ingest::blocks::docx(cx.path) {
        Ok(b) => b,
        Err(e) => {
            cx.add(Finding::new(
                "docx.xml-parse",
                Severity::Error,
                format!("the DOCX reader failed: {e:#}"),
            ));
            return Ok(());
        }
    };
    let joined: String = blocks
        .iter()
        .map(|b| match b {
            meowlaw_ingest::Block::Heading { text, .. } | meowlaw_ingest::Block::Para(text) => {
                text.as_str()
            }
            _ => "",
        })
        .collect::<Vec<_>>()
        .join("\n\n");
    if text::check_empty(cx, &joined) {
        return Ok(());
    }
    let lines = Lines::extracted(&joined);
    let rules = TextRules {
        hygiene: false,
        furniture: false,
        hyphenation: false,
        layout_tables: false,
        markdown: false,
        bom_matters: false,
        skip_lines: 0,
    };
    text::check_text(cx, &lines, &joined, &rules);
    let sblocks = structure::unlocated(blocks);
    let paras: Vec<_> = sblocks
        .iter()
        .filter(|b| b.kind == structure::SKind::Para)
        .map(|b| (b.text.clone(), b.loc))
        .collect();
    text::check_duplicates(cx, &paras);
    structure::check(
        cx,
        &sblocks,
        &profile,
        kind,
        Format::Docx,
        manifest.content.as_deref() == Some("summary"),
    );
    Ok(())
}
