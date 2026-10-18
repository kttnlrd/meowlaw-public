//! HTML and XML (Akoma Ntoso / USLM) rules.

use crate::text::{Lines, TextRules};
use crate::{
    Cx, Finding, LintOptions, Loc, Severity, meta, plural, resolve_target, structure, text,
};
use anyhow::{Context, Result};
use meowlaw_ingest::{Block, Format};
use quick_xml::events::Event;
use std::collections::BTreeMap;

fn read(cx: &mut Cx) -> Result<String> {
    let bytes = std::fs::read(cx.path).with_context(|| format!("reading {}", cx.path.display()))?;
    let d = text::decode(&bytes);
    text::report_decoding(cx, &d, &bytes);
    Ok(d.text)
}

fn rules() -> TextRules {
    TextRules {
        hygiene: false,
        furniture: false,
        hyphenation: false,
        layout_tables: false,
        markdown: false,
        bom_matters: false,
        skip_lines: 0,
    }
}

/// Character rules, duplicates and structure on the blocks the reader produced.
fn check_blocks(
    cx: &mut Cx,
    blocks: Vec<Block>,
    profile: &meowlaw_core::Profile,
    kind: Option<meowlaw_core::CorpusKind>,
    format: Format,
    summary_only: bool,
) {
    let joined = blocks
        .iter()
        .map(|b| match b {
            Block::Heading { text, .. } | Block::Para(text) => text.as_str(),
            Block::Element { title, .. } => title.as_deref().unwrap_or(""),
        })
        .collect::<Vec<_>>()
        .join("\n\n");
    let lines = Lines::extracted(&joined);
    text::check_text(cx, &lines, &joined, &rules());
    let sblocks = structure::unlocated(blocks);
    let paras: Vec<_> = sblocks
        .iter()
        .filter(|b| b.kind == structure::SKind::Para)
        .map(|b| (b.text.clone(), b.loc))
        .collect();
    text::check_duplicates(cx, &paras);
    structure::check(cx, &sblocks, profile, kind, format, summary_only);
}

pub(crate) fn lint_html(cx: &mut Cx, opts: &LintOptions) -> Result<()> {
    use scraper::{Html, Selector};
    let src = read(cx)?;
    if text::check_empty(cx, &src) {
        return Ok(());
    }
    let manifest = meta::collect(cx, None, false);
    let (profile, kind) = resolve_target(cx, opts, &manifest)?;

    if !src.contains('<') || !src.contains('>') {
        cx.add(
            Finding::new(
                "html.not-html",
                Severity::Warning,
                "file has an HTML extension but contains no tags",
            )
            .fix("Rename it to .txt (or .md) so the plain-text reader and heading patterns are used."),
        );
    }
    let doc = Html::parse_document(&src);
    if !doc.errors.is_empty() {
        let first: Vec<String> = doc.errors.iter().take(3).map(|e| e.to_string()).collect();
        cx.add(Finding::new(
            "html.parse-errors",
            Severity::Info,
            format!(
                "{} (the parser recovers, but content may nest unexpectedly): {}",
                plural(doc.errors.len(), "HTML parse error"),
                first.join("; ")
            ),
        ));
    }
    let tables = doc.select(&Selector::parse("table").unwrap()).count();
    cx.stat("tables", tables);
    if tables > 0 {
        cx.add(Finding::new(
            "html.tables",
            Severity::Info,
            format!("{}: each row is ingested as one \"| a | b |\" paragraph; nested tables and spans may scramble cells", plural(tables, "table")),
        ));
    }

    // Visible text versus what the block reader keeps.
    let body_sel = Selector::parse("body").unwrap();
    let mut visible = 0usize;
    if let Some(body) = doc.select(&body_sel).next() {
        for n in body.descendants() {
            let Some(t) = n.value().as_text() else {
                continue;
            };
            let hidden = n.ancestors().any(|a| {
                a.value().as_element().is_some_and(|e| {
                    matches!(e.name(), "script" | "style" | "noscript" | "template")
                })
            });
            if !hidden {
                visible += t.chars().filter(|c| !c.is_whitespace()).count();
            }
        }
    }
    let blocks = meowlaw_ingest::blocks::html(&src);
    let kept: usize = blocks
        .iter()
        .map(|b| match b {
            Block::Heading { text, .. } | Block::Para(text) => text
                .chars()
                .filter(|c| !c.is_whitespace() && *c != '|')
                .count(),
            _ => 0,
        })
        .sum();
    cx.stat("visible_chars", visible);
    cx.stat("ingested_chars", kept);
    if blocks.is_empty() {
        cx.add(
            Finding::new(
                "html.no-blocks",
                Severity::Error,
                "no p, li, h1–h6, tr, dt/dd, blockquote or pre elements: nothing will be ingested",
            )
            .fix("If the page is built by JavaScript, save the rendered page (browser \"Save as… complete\") or print to PDF; if the text is in <div>s, convert to Markdown."),
        );
        return Ok(());
    }
    if visible >= 500 && kept * 10 < visible * 6 {
        cx.add(
            Finding::new(
                "html.text-outside-blocks",
                Severity::Warning,
                format!(
                    "only {}% of the visible text is inside elements the reader keeps (p, li, h1–h6, tr, dt/dd, blockquote, pre); text sitting directly in div/span/section is dropped",
                    kept * 100 / visible.max(1)
                ),
            )
            .fix("Convert the page to Markdown (e.g. pandoc -t gfm) and ingest that, or wrap the text in <p>."),
        );
    }
    let summary = manifest.content.as_deref() == Some("summary");
    check_blocks(cx, blocks, &profile, kind, Format::Html, summary);
    Ok(())
}

pub(crate) fn lint_xml(cx: &mut Cx, opts: &LintOptions) -> Result<()> {
    let src = read(cx)?;
    if text::check_empty(cx, &src) {
        return Ok(());
    }
    let manifest = meta::collect(cx, None, false);
    let (profile, kind) = resolve_target(cx, opts, &manifest)?;

    // Well-formedness and a census of elements and entities.
    let mut r = quick_xml::Reader::from_str(&src);
    let mut root: Option<String> = None;
    let (mut nums, mut headings) = (0usize, 0usize);
    let mut unknown_entities: BTreeMap<String, usize> = BTreeMap::new();
    loop {
        match r.read_event() {
            Ok(Event::Start(e)) | Ok(Event::Empty(e)) => {
                let name = e.local_name().as_ref().to_string();
                if root.is_none() {
                    root = Some(name.clone());
                }
                match name.as_str() {
                    "num" => nums += 1,
                    "heading" | "subheading" => headings += 1,
                    _ => {}
                }
            }
            Ok(Event::GeneralRef(g)) => {
                let n = g.into_inner().to_string();
                if !matches!(n.as_str(), "amp" | "lt" | "gt" | "quot" | "apos" | "nbsp")
                    && !n.starts_with('#')
                {
                    *unknown_entities.entry(n).or_default() += 1;
                }
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(e) => {
                // A byte offset inside a multi-byte character is not sliceable.
                let mut pos = (r.error_position() as usize).min(src.len());
                while !src.is_char_boundary(pos) {
                    pos -= 1;
                }
                let line = src[..pos].matches('\n').count() + 1;
                let excerpt = src.lines().nth(line - 1).unwrap_or("");
                cx.add(
                    Finding::new(
                        "xml.parse",
                        Severity::Error,
                        format!("XML is not well-formed; ingest will fail: {e}"),
                    )
                    .at(Loc::line(line))
                    .excerpt(excerpt.trim())
                    .fix("Fix the markup (unclosed or mismatched tag, stray & that should be &amp;)."),
                );
                return Ok(());
            }
        }
    }
    let schema = match root.as_deref() {
        Some("akomaNtoso") => "akoma-ntoso",
        Some("uscDoc" | "lawDoc" | "uslm") => "uslm",
        _ => "unknown",
    };
    cx.stat("xml_root", root.clone().unwrap_or_default());
    cx.stat("xml_schema", schema);
    if nums == 0 && headings == 0 {
        cx.add(
            Finding::new(
                "xml.unknown-schema",
                Severity::Warning,
                format!(
                    "root <{}> has no <num> or <heading> elements: not Akoma Ntoso/USLM-like, so no structure will be read from the markup",
                    root.as_deref().unwrap_or("?")
                ),
            )
            .fix("Convert to Akoma Ntoso/USLM, or to Markdown/text so the profile's heading patterns apply."),
        );
    }
    if !unknown_entities.is_empty() {
        let list = unknown_entities
            .iter()
            .map(|(k, v)| format!("&{k}; ×{v}"))
            .collect::<Vec<_>>()
            .join(", ");
        cx.add(
            Finding::new(
                "xml.unknown-entity",
                Severity::Warning,
                format!(
                    "named entities the reader does not know are dropped from the text: {list}"
                ),
            )
            .fix(
                "Replace them with the characters themselves or numeric references (&#167; for §).",
            ),
        );
    }

    let blocks = match meowlaw_ingest::blocks::xml(&src) {
        Ok(b) => b,
        Err(e) => {
            cx.add(Finding::new(
                "xml.parse",
                Severity::Error,
                format!("the XML reader failed: {e:#}"),
            ));
            return Ok(());
        }
    };
    let elements: Vec<&Block> = blocks
        .iter()
        .filter(|b| matches!(b, Block::Element { .. }))
        .collect();
    let without_num = elements
        .iter()
        .filter(|b| matches!(b, Block::Element { number: None, .. }))
        .count();
    cx.stat("xml_elements", elements.len());
    if without_num > 0 {
        cx.add(Finding::new(
            "xml.missing-num",
            Severity::Info,
            format!(
                "{} of {} structural elements have no <num>; their ids fall back to titles or ordinals",
                without_num,
                elements.len()
            ),
        ));
    }
    if blocks.is_empty() {
        cx.add(Finding::new(
            "file.empty",
            Severity::Error,
            "the XML reader found no text outside metadata elements",
        ));
        return Ok(());
    }
    let summary = manifest.content.as_deref() == Some("summary");
    check_blocks(cx, blocks, &profile, kind, Format::Xml, summary);
    Ok(())
}
