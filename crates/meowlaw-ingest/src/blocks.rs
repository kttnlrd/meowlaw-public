//! Format readers. Every input format is reduced to a flat list of [`Block`]s;
//! the structurer then builds the tree from those blocks using the profile.

use anyhow::{Context, Result, bail};
use quick_xml::events::Event;
use std::io::Read;
use std::path::Path;

#[derive(Debug, Clone, PartialEq)]
pub enum Block {
    /// A heading. `depth` is the Markdown/HTML level when known.
    Heading { depth: Option<u8>, text: String },
    /// A paragraph or list item of body text.
    Para(String),
    /// A structural element from XML with its label, number and heading already split.
    Element {
        depth: u8,
        label: String,
        number: Option<String>,
        title: Option<String>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Markdown,
    Text,
    Html,
    Xml,
    Docx,
    Pdf,
    Json,
}

impl Format {
    pub fn from_path(p: &Path) -> Option<Self> {
        let ext = p.extension()?.to_str()?.to_ascii_lowercase();
        Some(match ext.as_str() {
            "md" | "markdown" => Self::Markdown,
            "txt" | "text" => Self::Text,
            "html" | "htm" | "xhtml" => Self::Html,
            "xml" | "akn" => Self::Xml,
            "docx" => Self::Docx,
            "pdf" => Self::Pdf,
            "json" => Self::Json,
            _ => return None,
        })
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s.to_ascii_lowercase().as_str() {
            "md" | "markdown" => Self::Markdown,
            "txt" | "text" => Self::Text,
            "html" => Self::Html,
            "xml" | "akn" | "akoma-ntoso" | "uslm" => Self::Xml,
            "docx" => Self::Docx,
            "pdf" => Self::Pdf,
            "json" => Self::Json,
            _ => return None,
        })
    }
}

/// Read a file into blocks. `Json` is handled by the caller (it is already a tree).
pub fn read_blocks(path: &Path, format: Format) -> Result<Vec<Block>> {
    match format {
        Format::Markdown => Ok(markdown(&read_text(path)?)),
        Format::Text => Ok(text_with_tables(&read_text(path)?)),
        Format::Html => Ok(html(&read_text(path)?)),
        Format::Xml => xml(&read_text(path)?),
        Format::Docx => docx(path),
        Format::Pdf => pdf(path),
        Format::Json => bail!("JSON input is a tree, not blocks"),
    }
}

pub fn read_text(path: &Path) -> Result<String> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    Ok(match String::from_utf8(bytes) {
        Ok(s) => s,
        // Latin-1 fallback: every byte is a code point.
        Err(e) => e.into_bytes().iter().map(|b| *b as char).collect(),
    })
}

/// Split off TOML front matter delimited by `+++` lines.
pub fn front_matter(src: &str) -> (Option<&str>, &str) {
    let s = src.trim_start_matches('\u{feff}');
    if let Some(rest) = s
        .strip_prefix("+++\n")
        .or_else(|| s.strip_prefix("+++\r\n"))
        && let Some(end) = rest.find("\n+++")
    {
        let fm = &rest[..end];
        let body = rest[end + 4..].trim_start_matches(['\r', '\n']);
        return (Some(fm), body);
    }
    (None, s)
}

/// Markdown: ATX headings become headings; blank-line separated text becomes
/// paragraphs. List items and table rows are kept one per paragraph so
/// definition patterns can anchor on them.
pub fn markdown(src: &str) -> Vec<Block> {
    let (_, body) = front_matter(src);
    let mut out = Vec::new();
    let mut para: Vec<&str> = Vec::new();
    let mut in_fence = false;
    let flush = |para: &mut Vec<&str>, out: &mut Vec<Block>| {
        if !para.is_empty() {
            let t = para.join("\n").trim().to_string();
            if !t.is_empty() {
                out.push(Block::Para(t));
            }
            para.clear();
        }
    };
    for line in body.lines() {
        let t = line.trim_end();
        if t.trim_start().starts_with("```") {
            in_fence = !in_fence;
            para.push(t);
            continue;
        }
        if in_fence {
            para.push(t);
            continue;
        }
        if let Some((depth, text)) = atx_heading(t) {
            flush(&mut para, &mut out);
            out.push(Block::Heading {
                depth: Some(depth),
                text: text.to_string(),
            });
        } else if t.trim().is_empty() {
            flush(&mut para, &mut out);
        } else if t.trim_start().starts_with('|') {
            // Consecutive pipe-table lines stay one block.
            if !para
                .first()
                .is_some_and(|l| l.trim_start().starts_with('|'))
            {
                flush(&mut para, &mut out);
            }
            para.push(t);
        } else if is_item_start(t) {
            flush(&mut para, &mut out);
            para.push(t);
        } else {
            if para
                .first()
                .is_some_and(|l| l.trim_start().starts_with('|'))
            {
                flush(&mut para, &mut out);
            }
            para.push(t);
        }
    }
    flush(&mut para, &mut out);
    out
}

fn atx_heading(line: &str) -> Option<(u8, &str)> {
    let t = line.trim_start();
    let hashes = t.bytes().take_while(|b| *b == b'#').count();
    if (1..=6).contains(&hashes) && t[hashes..].starts_with(' ') {
        let text = t[hashes..].trim().trim_end_matches('#').trim();
        let text = text.trim_matches('*').trim();
        return Some((hashes as u8, text));
    }
    None
}

fn is_item_start(line: &str) -> bool {
    let t = line.trim_start();
    t.starts_with("- ")
        || t.starts_with("* ")
        || t.starts_with("• ")
        || t.starts_with('|')
        || (t.starts_with('(') && t.find(')').is_some_and(|i| i <= 6 && i > 1))
}

/// Plain text: each non-empty line that looks like a heading (short, no final
/// full stop) is offered to the structurer as a heading candidate; the
/// structurer decides via the profile. Other lines are grouped into paragraphs.
pub fn plain_text(src: &str) -> Vec<Block> {
    let mut out = Vec::new();
    let mut para: Vec<String> = Vec::new();
    let flush = |para: &mut Vec<String>, out: &mut Vec<Block>| {
        if !para.is_empty() {
            out.push(Block::Para(para.join(" ").trim().to_string()));
            para.clear();
        }
    };
    let mut after_heading = false;
    for line in src.lines() {
        let t = line.trim();
        if t.is_empty() {
            flush(&mut para, &mut out);
            after_heading = false;
            continue;
        }
        // Only the first line of a block can be a heading, and only if it is
        // short and unpunctuated. The structurer decides via the profile; lines
        // after it belong to the paragraph that follows.
        let heading_like = t.chars().count() <= 120 && !t.ends_with(['.', ',', ';', ':']);
        if heading_like && para.is_empty() && !after_heading {
            out.push(Block::Heading {
                depth: None,
                text: t.to_string(),
            });
            after_heading = true;
            continue;
        }
        after_heading = false;
        if is_item_start(t) {
            flush(&mut para, &mut out);
            para.push(t.to_string());
        } else {
            para.push(t.to_string());
        }
    }
    flush(&mut para, &mut out);
    out
}

/// HTML: h1-h6 are headings; p, li, td/th rows, dt/dd and blockquote are paragraphs.
pub fn html(src: &str) -> Vec<Block> {
    use scraper::{Html, Selector};
    let doc = Html::parse_document(src);
    let sel = Selector::parse("h1,h2,h3,h4,h5,h6,p,li,table,dt,dd,blockquote,pre").unwrap();
    let row_sel = Selector::parse("tr").unwrap();
    let mut out = Vec::new();
    for el in doc.select(&sel) {
        let name = el.value().name();
        // Skip elements nested in another selected block (li > p etc.).
        if el
            .ancestors()
            .filter_map(scraper::ElementRef::wrap)
            .any(|a| {
                matches!(
                    a.value().name(),
                    "p" | "li" | "table" | "dd" | "blockquote" | "pre"
                )
            })
        {
            continue;
        }
        let text = if name == "table" {
            // Rows of this table only (not of tables nested in a cell).
            let mut header = false;
            let rows: Vec<crate::tables::Row> = el
                .select(&row_sel)
                .filter(|tr| {
                    tr.ancestors()
                        .filter_map(scraper::ElementRef::wrap)
                        .find(|a| a.value().name() == "table")
                        .is_some_and(|t| t.id() == el.id())
                })
                .enumerate()
                .map(|(i, tr)| {
                    let cells: Vec<scraper::ElementRef> = tr
                        .children()
                        .filter_map(scraper::ElementRef::wrap)
                        .filter(|c| matches!(c.value().name(), "td" | "th"))
                        .collect();
                    if i == 0 && cells.iter().all(|c| c.value().name() == "th") {
                        header = true;
                    }
                    cells
                        .iter()
                        .map(|c| normalise_ws(&c.text().collect::<String>()))
                        .collect()
                })
                .collect();
            crate::tables::to_markdown(&rows, header && rows.len() > 1)
        } else if name == "pre" {
            el.text().collect::<String>()
        } else {
            normalise_ws(&el.text().collect::<String>())
        };
        if text.trim().is_empty() {
            continue;
        }
        if let Some(d) = name.strip_prefix('h').and_then(|d| d.parse::<u8>().ok()) {
            out.push(Block::Heading {
                depth: Some(d),
                text,
            });
        } else if name == "li" {
            out.push(Block::Para(format!("- {text}")));
        } else {
            out.push(Block::Para(text));
        }
    }
    out
}

fn normalise_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Hierarchical elements recognised in Akoma Ntoso and USLM.
const XML_LEVELS: &[(&str, &str)] = &[
    ("book", "Book"),
    ("title", "Title"),
    ("subtitle", "Subtitle"),
    ("chapter", "Chapter"),
    ("subchapter", "Subchapter"),
    ("part", "Part"),
    ("subpart", "Subpart"),
    ("division", "Division"),
    ("subdivision", "Subdivision"),
    ("section", "Section"),
    ("article", "Article"),
    ("rule", "Rule"),
    ("regulation", "Regulation"),
    ("clause", "Clause"),
    ("schedule", "Schedule"),
    ("annex", "Annex"),
    ("appendix", "Appendix"),
    ("attachment", "Annex"),
];

/// Block-level elements that end a paragraph.
const XML_BLOCKS: &[&str] = &[
    "p",
    "subsection",
    "paragraph",
    "subparagraph",
    "clause_",
    "point",
    "item",
    "content",
    "intro",
    "wrapUp",
    "chapeau",
    "continuation",
    "list",
    "blockList",
    "listIntroduction",
    "tr",
    "li",
    "def",
    "definition",
    "alinea",
    "indent",
    "level",
    "proviso",
    "subclause",
];

const XML_SKIP: &[&str] = &[
    "meta",
    "toc",
    "notes",
    "note",
    "authorialNote",
    "sourceCredit",
    "references",
    "lifecycle",
    "analysis",
    "proprietary",
    "preface",
    "coverPage",
];

/// Akoma Ntoso (OASIS LegalDocML) and USLM (US Code). Both use hierarchical
/// elements with `num` and `heading` children, so one reader handles both.
pub fn xml(src: &str) -> Result<Vec<Block>> {
    let mut reader = quick_xml::Reader::from_str(src);
    reader.config_mut().trim_text(false);
    let mut out = Vec::new();
    // Stack of open element local names.
    let mut stack: Vec<String> = Vec::new();
    let mut depth: u8 = 0;
    // Pending structural element awaiting its num/heading.
    let mut pending: Option<(u8, String, Option<String>, Option<String>)> = None;
    let mut capture: Option<(&'static str, String)> = None;
    let mut para = String::new();
    let mut skip_depth: Option<usize> = None;
    // Open table: nesting depth, rows, the cell being read, and whether each
    // row is all `th` (a header row).
    struct XmlTable {
        depth: usize,
        rows: Vec<Vec<String>>,
        th: Vec<bool>,
        cell: Option<String>,
    }
    let mut tbl: Option<XmlTable> = None;

    fn flush_para(para: &mut String, out: &mut Vec<Block>) {
        let t = normalise_ws(para);
        // A bare paragraph number ("1.", "(a)") belongs to the text that follows.
        if is_bare_number(&t) {
            *para = format!("{t} ");
            return;
        }
        if !t.is_empty() {
            out.push(Block::Para(t));
        }
        para.clear();
    }
    fn flush_pending(
        pending: &mut Option<(u8, String, Option<String>, Option<String>)>,
        out: &mut Vec<Block>,
    ) {
        if let Some((depth, label, number, title)) = pending.take() {
            out.push(Block::Element {
                depth,
                label,
                number,
                title,
            });
        }
    }

    loop {
        let ev = reader.read_event().context("parsing XML")?;
        match ev {
            Event::Start(e) => {
                let name = e.local_name().as_ref().to_string();
                stack.push(name.clone());
                if skip_depth.is_some() {
                    continue;
                }
                if XML_SKIP.contains(&name.as_str()) {
                    skip_depth = Some(stack.len());
                    continue;
                }
                if let Some(t) = tbl.as_mut() {
                    match name.as_str() {
                        "table" => t.depth += 1,
                        "tr" if t.depth == 1 => {
                            t.rows.push(Vec::new());
                            t.th.push(true);
                        }
                        "td" | "th" if t.depth == 1 => {
                            if name == "td"
                                && let Some(h) = t.th.last_mut()
                            {
                                *h = false;
                            }
                            t.cell = Some(String::new());
                        }
                        _ => {
                            if let Some(c) = t.cell.as_mut()
                                && XML_BLOCKS.contains(&name.as_str())
                                && !c.is_empty()
                            {
                                c.push(' ');
                            }
                        }
                    }
                    continue;
                }
                if name == "table" {
                    flush_pending(&mut pending, &mut out);
                    flush_para(&mut para, &mut out);
                    tbl = Some(XmlTable {
                        depth: 1,
                        rows: Vec::new(),
                        th: Vec::new(),
                        cell: None,
                    });
                    continue;
                }
                if let Some((_, label)) = XML_LEVELS.iter().find(|(n, _)| *n == name) {
                    // `title` is also an inline element in some schemas: only treat it
                    // as a level when it is not inside a heading.
                    if name == "title" && stack.iter().any(|s| s == "heading") {
                        continue;
                    }
                    flush_para(&mut para, &mut out);
                    flush_pending(&mut pending, &mut out);
                    depth = depth.saturating_add(1);
                    pending = Some((depth, label.to_string(), None, None));
                } else if name == "num" && pending.is_some() && capture.is_none() {
                    capture = Some(("num", String::new()));
                } else if (name == "heading" || name == "subheading")
                    && pending.is_some()
                    && capture.is_none()
                {
                    capture = Some(("heading", String::new()));
                } else if XML_BLOCKS.contains(&name.as_str()) {
                    flush_pending(&mut pending, &mut out);
                    flush_para(&mut para, &mut out);
                }
            }
            Event::End(e) => {
                let name = e.local_name().as_ref().to_string();
                let level = stack.len();
                stack.pop();
                if let Some(sd) = skip_depth {
                    if level == sd {
                        skip_depth = None;
                    }
                    continue;
                }
                if let Some(t) = tbl.as_mut() {
                    match name.as_str() {
                        "table" => t.depth -= 1,
                        "td" | "th" if t.depth == 1 => {
                            if let Some(c) = t.cell.take() {
                                match t.rows.last_mut() {
                                    Some(r) => r.push(c),
                                    None => {
                                        t.rows.push(vec![c]);
                                        t.th.push(false);
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                    if t.depth == 0 {
                        let t = tbl.take().unwrap();
                        let rows: Vec<Vec<String>> =
                            t.rows.into_iter().filter(|r| !r.is_empty()).collect();
                        let header = t.th.first() == Some(&true) && rows.len() > 1;
                        let md = crate::tables::to_markdown(&rows, header);
                        if !md.is_empty() {
                            out.push(Block::Para(md));
                        }
                    }
                    continue;
                }
                if let Some((kind, text)) = capture.as_ref()
                    && ((*kind == "num" && name == "num")
                        || (*kind == "heading" && (name == "heading" || name == "subheading")))
                {
                    let text = normalise_ws(text);
                    if let Some(p) = pending.as_mut() {
                        if *kind == "num" {
                            p.2 = Some(clean_num(&text));
                        } else if p.3.is_none() {
                            p.3 = Some(text);
                        }
                    }
                    capture = None;
                    continue;
                }
                if XML_LEVELS.iter().any(|(n, _)| *n == name)
                    && !(name == "title" && stack.iter().any(|s| s == "heading"))
                {
                    flush_para(&mut para, &mut out);
                    flush_pending(&mut pending, &mut out);
                    depth = depth.saturating_sub(1);
                } else if XML_BLOCKS.contains(&name.as_str()) {
                    flush_para(&mut para, &mut out);
                }
            }
            Event::Text(t) => {
                if skip_depth.is_some() {
                    continue;
                }
                let s = t.xml10_content();
                if let Some(t) = tbl.as_mut() {
                    if let Some(c) = t.cell.as_mut() {
                        c.push_str(&s);
                    }
                    continue;
                }
                push_text(&mut capture, &mut para, &s);
            }
            Event::CData(t) => {
                if skip_depth.is_some() {
                    continue;
                }
                let s = t.into_inner().to_string();
                if let Some(t) = tbl.as_mut() {
                    if let Some(c) = t.cell.as_mut() {
                        c.push_str(&s);
                    }
                    continue;
                }
                push_text(&mut capture, &mut para, &s);
            }
            Event::GeneralRef(r) => {
                if skip_depth.is_some() {
                    continue;
                }
                let name = r.into_inner();
                let s = resolve_entity(&name);
                if let Some(t) = tbl.as_mut() {
                    if let Some(c) = t.cell.as_mut() {
                        c.push_str(&s);
                    }
                    continue;
                }
                push_text(&mut capture, &mut para, &s);
            }
            Event::Eof => break,
            _ => {}
        }
    }
    // A trailing bare number has no paragraph to join; keep it.
    let t = normalise_ws(&para);
    if !t.is_empty() {
        out.push(Block::Para(t));
    }
    para.clear();
    flush_pending(&mut pending, &mut out);
    Ok(out)
}

fn is_bare_number(t: &str) -> bool {
    let core = t.trim_start_matches('(').trim_end_matches(['.', ')']);
    let numeric = core.chars().all(|c| c.is_ascii_digit())
        || core.len() <= 2 && core.chars().all(|c| c.is_ascii_alphanumeric())
        || core
            .chars()
            .all(|c| matches!(c, 'i' | 'v' | 'x' | 'I' | 'V' | 'X'));
    !t.is_empty()
        && t.len() <= 6
        && !core.is_empty()
        && numeric
        && (t.ends_with('.') || t.ends_with(')'))
}

fn push_text(capture: &mut Option<(&'static str, String)>, para: &mut String, s: &str) {
    match capture {
        Some((_, buf)) => buf.push_str(s),
        None => para.push_str(s),
    }
}

fn resolve_entity(name: &str) -> String {
    match name {
        "amp" => "&".into(),
        "lt" => "<".into(),
        "gt" => ">".into(),
        "quot" => "\"".into(),
        "apos" => "'".into(),
        "nbsp" => " ".into(),
        n if n.starts_with("#x") || n.starts_with("#X") => u32::from_str_radix(&n[2..], 16)
            .ok()
            .and_then(char::from_u32)
            .map(String::from)
            .unwrap_or_default(),
        n if n.starts_with('#') => n[1..]
            .parse::<u32>()
            .ok()
            .and_then(char::from_u32)
            .map(String::from)
            .unwrap_or_default(),
        _ => String::new(),
    }
}

/// `§ 162.` → `162`; `Section 5` → `5`; `Article 12` → `12`; `(a)` stays.
fn clean_num(s: &str) -> String {
    let s = s.trim().trim_end_matches(['.', ':', '—', '–']).trim();
    let s = s.trim_start_matches('§').trim();
    let lower = s.to_lowercase();
    for prefix in [
        "section",
        "sec.",
        "article",
        "art.",
        "part",
        "chapter",
        "title",
        "subtitle",
        "subchapter",
        "division",
        "subdivision",
        "rule",
        "regulation",
        "clause",
        "schedule",
        "annex",
        "appendix",
        "book",
    ] {
        if lower.starts_with(prefix) {
            return s[prefix.len()..].trim().to_string();
        }
    }
    s.to_string()
}

/// DOCX: paragraphs from `word/document.xml`; `Heading N` / `Title` styles become headings.
pub fn docx(path: &Path) -> Result<Vec<Block>> {
    let f = std::fs::File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let mut zip = zip::ZipArchive::new(f).context("reading DOCX container")?;
    let mut xml_src = String::new();
    zip.by_name("word/document.xml")
        .context("DOCX has no word/document.xml")?
        .read_to_string(&mut xml_src)?;
    docx_document(&xml_src)
}

/// Blocks of a DOCX `word/document.xml`.
fn docx_document(xml_src: &str) -> Result<Vec<Block>> {
    let mut reader = quick_xml::Reader::from_str(xml_src);
    let mut out = Vec::new();
    let mut text = String::new();
    let mut style: Option<String> = None;
    let mut in_t = false;
    // Tables: nesting depth, rows of the outermost table, the cell being read.
    // Paragraphs inside a cell (and nested tables) become the cell's text.
    let mut tbl_depth = 0usize;
    let mut rows: Vec<crate::tables::Row> = Vec::new();
    let mut cell = String::new();
    loop {
        match reader.read_event().context("parsing DOCX XML")? {
            Event::Start(e) | Event::Empty(e) => {
                let name = e.local_name();
                match name.as_ref() {
                    "p" => {
                        text.clear();
                        style = None;
                    }
                    "pStyle" => {
                        for a in e.attributes().flatten() {
                            if a.key.local_name().as_ref() == "val" {
                                style = Some(a.value.to_string());
                            }
                        }
                    }
                    "t" => in_t = true,
                    "tbl" => {
                        tbl_depth += 1;
                        if tbl_depth == 1 {
                            rows.clear();
                        }
                    }
                    "tr" if tbl_depth == 1 => rows.push(Vec::new()),
                    "tc" if tbl_depth == 1 => cell.clear(),
                    "tab" => text.push('\t'),
                    "br" => text.push('\n'),
                    _ => {}
                }
            }
            Event::Text(t) if in_t => text.push_str(&t.xml10_content()),
            Event::GeneralRef(r) if in_t => text.push_str(&resolve_entity(&r.into_inner())),
            Event::End(e) => match e.local_name().as_ref() {
                "t" => in_t = false,
                "tc" if tbl_depth == 1 => {
                    if let Some(r) = rows.last_mut() {
                        r.push(cell.trim().to_string());
                    }
                }
                "tbl" => {
                    tbl_depth = tbl_depth.saturating_sub(1);
                    if tbl_depth == 0 && !rows.is_empty() {
                        let md = crate::tables::to_markdown(&rows, rows.len() > 1);
                        out.push(Block::Para(md));
                        rows.clear();
                    }
                }
                "p" if tbl_depth > 0 => {
                    let t = text.trim();
                    if !t.is_empty() {
                        if !cell.is_empty() {
                            cell.push(' ');
                        }
                        cell.push_str(t);
                    }
                    text.clear();
                }
                "p" => {
                    let t = text.trim().to_string();
                    if !t.is_empty() {
                        let depth = style.as_deref().and_then(heading_style_depth);
                        match depth {
                            Some(d) => out.push(Block::Heading {
                                depth: Some(d),
                                text: t,
                            }),
                            None => out.push(Block::Para(t)),
                        }
                    }
                    text.clear();
                }
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(out)
}

fn heading_style_depth(style: &str) -> Option<u8> {
    let s = style.to_ascii_lowercase().replace([' ', '-', '_'], "");
    if s == "title" {
        return Some(1);
    }
    s.strip_prefix("heading")
        .and_then(|n| n.parse::<u8>().ok())
        .filter(|d| (1..=9).contains(d))
}

/// PDF via Poppler's `pdftotext -layout`, if installed. Scanned PDFs need OCR first.
pub fn pdf(path: &Path) -> Result<Vec<Block>> {
    let out = std::process::Command::new("pdftotext")
        .arg("-layout")
        .arg("-enc")
        .arg("UTF-8")
        .arg(path)
        .arg("-")
        .output()
        .context("PDF ingestion needs `pdftotext` (poppler-utils) on PATH")?;
    if !out.status.success() {
        bail!(
            "pdftotext failed on {}: {}",
            path.display(),
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let text = String::from_utf8_lossy(&out.stdout);
    // Drop form feeds and bare page numbers.
    let cleaned: String = text
        .replace('\u{c}', "\n")
        .lines()
        .filter(|l| {
            let t = l.trim();
            !(t.len() <= 4 && t.chars().all(|c| c.is_ascii_digit()) && !t.is_empty())
        })
        .collect::<Vec<_>>()
        .join("\n");
    if cleaned.trim().is_empty() {
        bail!(
            "{} has no text layer (scanned?). Run OCR first, e.g. `ocrmypdf in.pdf out.pdf`.",
            path.display()
        );
    }
    Ok(text_with_tables(&cleaned))
}

/// Plain text with fixed-width tables rebuilt as pipe tables.
pub fn text_with_tables(src: &str) -> Vec<Block> {
    let mut out = Vec::new();
    for seg in crate::tables::detect_layout_tables(src) {
        match seg {
            crate::tables::Segment::Text(t) => out.extend(plain_text(&t)),
            crate::tables::Segment::Table(t) => out.push(Block::Para(t)),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const TABLE_MD: &str = "| Item | Asset | Cost |\n| --- | --- | --- |\n| 1 | Car | market value |\n| 2 | Gift | nil |";

    fn tables(b: &[Block]) -> Vec<&str> {
        b.iter()
            .filter_map(|b| match b {
                Block::Para(t) if t.starts_with('|') => Some(t.as_str()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn xml_table() {
        let src = r#"<akomaNtoso><act><body><section><num>12</num><heading>Cost</heading>
            <content><p>The cost is:</p>
            <table><tr><th>Item</th><th>Asset</th><th>Cost</th></tr>
            <tr><td>1</td><td><p>Car</p></td><td>market <i>value</i></td></tr>
            <tr><td>2</td><td>Gift</td><td>nil</td></tr></table>
            <p>After.</p></content></section></body></act></akomaNtoso>"#;
        let b = xml(src).unwrap();
        assert_eq!(tables(&b), vec![TABLE_MD], "{b:#?}");
        assert!(
            b.iter()
                .any(|x| matches!(x, Block::Para(t) if t == "After."))
        );
    }

    #[test]
    fn docx_table() {
        let src = r#"<w:document xmlns:w="x"><w:body>
            <w:p><w:r><w:t>The cost is:</w:t></w:r></w:p>
            <w:tbl>
            <w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Asset</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cost</w:t></w:r></w:p></w:tc></w:tr>
            <w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Car</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>market value</w:t></w:r></w:p></w:tc></w:tr>
            <w:tr><w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Gift</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>nil</w:t></w:r></w:p></w:tc></w:tr>
            </w:tbl></w:body></w:document>"#;
        let b = docx_document(src).unwrap();
        assert_eq!(tables(&b), vec![TABLE_MD], "{b:#?}");
    }

    #[test]
    fn html_table() {
        let src = "<p>The cost is:</p><table><thead><tr><th>Item</th><th>Asset</th><th>Cost</th></tr></thead>\
            <tbody><tr><td>1</td><td>Car</td><td>market value</td></tr><tr><td>2</td><td>Gift</td><td>nil</td></tr></tbody></table>";
        let b = html(src);
        assert_eq!(tables(&b), vec![TABLE_MD], "{b:#?}");
    }

    #[test]
    fn markdown_table_is_one_block() {
        let b = markdown(&format!("Intro.\n\n{TABLE_MD}\n\nAfter."));
        assert_eq!(tables(&b), vec![TABLE_MD], "{b:#?}");
    }

    #[test]
    fn plain_text_headings_only_at_block_start() {
        let b = plain_text(
            "1 The appellant appeals against\nthe decision below.\n\n2 The appeal fails.\n",
        );
        assert_eq!(
            b,
            vec![
                Block::Heading {
                    depth: None,
                    text: "1 The appellant appeals against".into()
                },
                Block::Para("the decision below.".into()),
                Block::Para("2 The appeal fails.".into()),
            ]
        );
        let b = plain_text("8-1 General deductions\n(1) You can deduct\nany loss.\n");
        assert_eq!(
            b[0],
            Block::Heading {
                depth: None,
                text: "8-1 General deductions".into()
            }
        );
        assert_eq!(b[1], Block::Para("(1) You can deduct any loss.".into()));
    }

    #[test]
    fn markdown_blocks() {
        let b = markdown(
            "+++\ntitle = \"x\"\n+++\n# Part 1 Intro\n\nSome text\nmore\n\n(1) one\n(2) two\n",
        );
        assert_eq!(
            b[0],
            Block::Heading {
                depth: Some(1),
                text: "Part 1 Intro".into()
            }
        );
        assert_eq!(b[1], Block::Para("Some text\nmore".into()));
        assert_eq!(b[2], Block::Para("(1) one".into()));
        assert_eq!(b[3], Block::Para("(2) two".into()));
    }

    #[test]
    fn xml_akn() {
        let src = r#"<akomaNtoso><act><meta><x>skip</x></meta><body>
            <part><num>Part 1</num><heading>Preliminary</heading>
              <section><num>1.</num><heading>Short title</heading>
                <content><p>This Act is the Example Act &amp; Co.</p></content>
              </section>
            </part></body></act></akomaNtoso>"#;
        let b = xml(src).unwrap();
        assert_eq!(
            b[0],
            Block::Element {
                depth: 1,
                label: "Part".into(),
                number: Some("1".into()),
                title: Some("Preliminary".into())
            }
        );
        assert_eq!(
            b[1],
            Block::Element {
                depth: 2,
                label: "Section".into(),
                number: Some("1".into()),
                title: Some("Short title".into())
            }
        );
        assert_eq!(
            b[2],
            Block::Para("This Act is the Example Act & Co.".into())
        );
        assert_eq!(b.len(), 3);
    }

    #[test]
    fn html_blocks() {
        let b = html(
            "<html><body><h2>Article 1</h2><p>Hello <b>world</b></p><ul><li>a</li></ul></body></html>",
        );
        assert_eq!(
            b,
            vec![
                Block::Heading {
                    depth: Some(2),
                    text: "Article 1".into()
                },
                Block::Para("Hello world".into()),
                Block::Para("- a".into()),
            ]
        );
    }
}
