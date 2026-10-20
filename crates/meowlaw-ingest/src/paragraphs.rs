//! Paragraph-level nodes and summaries for rulings, guidance, cases and
//! secondary material.
//!
//! Two representations of each document, both kept:
//!
//! 1. **Summary**: a `<corpus>:summary` node. Supplied in the manifest
//!    (`summary`), or the whole body when the file is itself a summary
//!    (`content = "summary"`), or the opening paragraphs as a labelled
//!    fallback. The search layer embeds it with citations stripped and uses it
//!    as the document's vector in the similarity graph.
//! 2. **Full text**: every paragraph is its own node, so it can be cited,
//!    fetched and searched on its own. Numbered rulings keep their numbers
//!    (`tr-2018-5:para12`); unnumbered material is numbered in reading order
//!    (`handbook:p7`).

use meowlaw_core::{Corpus, Meta, Node, NodeKind};
use regex::Regex;
use std::collections::HashMap;
use std::sync::LazyLock;

/// Characters of opening text used when no summary is supplied.
pub const FALLBACK_SUMMARY_CHARS: usize = 1_200;

/// Minimum sequential numbered paragraphs before a document counts as numbered.
const MIN_NUMBERED: usize = 3;

/// "12. " at a line start or after sentence punctuation, before text.
static MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?:^|[\s.;:?!)\]])(?P<num>[0-9]{1,3})\.\s+["'‘“(\[•A-Z]"#).unwrap()
});

/// Split every text-bearing node into paragraph children.
pub fn split_paragraphs(nodes: Vec<Node>, corpus: &Corpus) -> Vec<Node> {
    let numbered = numbered_markers(&nodes);
    let mut out: Vec<Node> = Vec::with_capacity(nodes.len() * 4);
    let mut child_ord: HashMap<String, u32> = HashMap::new();
    let mut seq = 0u32;
    for mut n in nodes {
        if n.text.trim().is_empty() || n.kind == NodeKind::Summary {
            out.push(n);
            continue;
        }
        let pieces: Vec<(Option<u32>, String)> = match numbered.get(&n.id) {
            Some(cuts) => split_at_markers(&n.text, cuts),
            None if numbered.is_empty() => n
                .text
                .split("\n\n")
                .map(|p| (None, tidy_lines(p.trim())))
                .filter(|(_, p)| !p.is_empty())
                .collect(),
            // Numbered document, but this node has no numbers (preamble, contents,
            // footnotes): keep its text on the node.
            None => vec![(None, n.text.clone())],
        };
        // Leading text before the first number stays on the node itself.
        let (keep, paras): (Vec<_>, Vec<_>) = if numbered.is_empty() {
            (vec![], pieces)
        } else {
            pieces.into_iter().partition(|(num, _)| num.is_none())
        };
        if paras.is_empty() {
            out.push(n);
            continue;
        }
        n.text = tidy_lines(
            &keep
                .into_iter()
                .map(|(_, t)| t)
                .collect::<Vec<_>>()
                .join("\n\n"),
        );
        let parent = n.id.clone();
        out.push(n);
        for (num, text) in paras {
            seq += 1;
            let ord = child_ord.entry(parent.clone()).or_insert(0);
            *ord += 1;
            let (id, number) = match num {
                Some(k) => (format!("{}:para{k}", corpus.id), k.to_string()),
                None => (format!("{}:p{seq}", corpus.id), seq.to_string()),
            };
            out.push(Node {
                id,
                corpus: corpus.id.clone(),
                kind: NodeKind::Paragraph,
                label: None,
                number: Some(number),
                title: None,
                text,
                parent: Some(parent.clone()),
                ordinal: 1_000 + *ord,
                meta: Meta::new(),
            });
        }
    }
    dedupe_ids(out)
}

/// Accepted paragraph markers per node: byte offset of the number and its value.
/// Numbers must run in sequence across the document (gaps of up to 3 allowed),
/// which rejects footnote numbers, list items and years.
fn numbered_markers(nodes: &[Node]) -> HashMap<String, Vec<(usize, u32)>> {
    let mut last = 0u32;
    let mut found: HashMap<String, Vec<(usize, u32)>> = HashMap::new();
    let mut total = 0;
    for n in nodes {
        for c in MARKER.captures_iter(&n.text) {
            let m = c.name("num").unwrap();
            if in_table_row(&n.text, m.start()) {
                continue;
            }
            let Ok(k) = m.as_str().parse::<u32>() else {
                continue;
            };
            if (last == 0 && k == 1) || (last > 0 && k > last && k <= last + 3) {
                found.entry(n.id.clone()).or_default().push((m.start(), k));
                last = k;
                total += 1;
            }
        }
    }
    if total < MIN_NUMBERED {
        found.clear();
    }
    found
}

fn split_at_markers(text: &str, cuts: &[(usize, u32)]) -> Vec<(Option<u32>, String)> {
    let mut out = Vec::new();
    let Some(first) = cuts.first() else {
        let head = text.trim();
        return if head.is_empty() {
            Vec::new()
        } else {
            vec![(None, head.to_string())]
        };
    };
    let head = text[..first.0].trim();
    if !head.is_empty() {
        out.push((None, head.to_string()));
    }
    for (i, (start, k)) in cuts.iter().enumerate() {
        let end = cuts.get(i + 1).map_or(text.len(), |c| c.0);
        let body = text[*start..end].trim();
        // Drop the "12." itself; the number is on the node.
        let body = body
            .trim_start_matches(|c: char| c.is_ascii_digit())
            .trim_start_matches('.')
            .trim();
        out.push((Some(*k), tidy_lines(body)));
    }
    out
}

/// Whether `pos` falls on a pipe-table line (a table cell is never a
/// paragraph marker).
fn in_table_row(text: &str, pos: usize) -> bool {
    let start = text[..pos].rfind('\n').map_or(0, |i| i + 1);
    text[start..].trim_start().starts_with('|')
}

/// Join hard-wrapped lines into running text, keeping list items ("•", "-",
/// "(a)", "(1)") and pipe-table rows on their own lines and attaching a lone
/// bullet to its item.
pub fn tidy_lines(text: &str) -> String {
    static ITEM: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^(?:[•\-*]\s|\([0-9a-zA-Z]{1,4}\)\s)").unwrap());
    let mut out = String::with_capacity(text.len());
    let mut pending_bullet = false;
    for para in text.split("\n\n") {
        if !out.is_empty() {
            out.push_str("\n\n");
        }
        let mut first = true;
        let mut prev_row = false;
        for line in para.lines().map(str::trim).filter(|l| !l.is_empty()) {
            if line == "•" || line == "-" {
                pending_bullet = true;
                continue;
            }
            // Pipe-table rows, and the line after one, keep their own lines.
            let row = line.starts_with('|');
            let item = pending_bullet || ITEM.is_match(line) || row || prev_row;
            prev_row = row;
            if !first {
                out.push(if item { '\n' } else { ' ' });
            }
            if pending_bullet {
                out.push_str("• ");
                pending_bullet = false;
            }
            out.push_str(line);
            first = false;
        }
    }
    out
}

fn dedupe_ids(mut nodes: Vec<Node>) -> Vec<Node> {
    let mut seen = std::collections::HashSet::new();
    for n in &mut nodes {
        while !seen.insert(n.id.clone()) {
            n.id.push('~');
        }
    }
    nodes
}

/// Where a document's summary comes from.
pub enum SummarySource<'a> {
    /// Supplied text, with a note on its origin.
    Supplied(&'a str, Option<&'a str>),
    /// The whole body is the summary (the file is a summary, not the full text).
    Body(Option<&'a str>),
    /// No summary given: use the opening paragraphs.
    Fallback,
}

/// Insert `<corpus>:summary` right after the root.
pub fn add_summary(mut nodes: Vec<Node>, corpus: &Corpus, src: SummarySource) -> Vec<Node> {
    let (text, origin) = match src {
        SummarySource::Supplied(t, o) => {
            (t.trim().to_string(), o.unwrap_or("supplied").to_string())
        }
        SummarySource::Body(o) => {
            // Fold the whole document into the summary node.
            let mut parts = Vec::new();
            for n in nodes.iter().skip(1) {
                if let Some(t) = &n.title
                    && n.kind != NodeKind::Paragraph
                {
                    parts.push(format!("**{t}**"));
                }
                if !n.text.trim().is_empty() {
                    parts.push(n.text.trim().to_string());
                }
            }
            if !nodes[0].text.trim().is_empty() {
                parts.insert(0, nodes[0].text.trim().to_string());
            }
            nodes.truncate(1);
            nodes[0].text.clear();
            (
                parts.join("\n\n"),
                o.unwrap_or("the source is a summary").to_string(),
            )
        }
        SummarySource::Fallback => {
            let mut t = String::new();
            for n in nodes.iter() {
                if n.text.trim().is_empty() {
                    continue;
                }
                if !t.is_empty() {
                    t.push_str("\n\n");
                }
                t.push_str(n.text.trim());
                if t.len() >= FALLBACK_SUMMARY_CHARS {
                    break;
                }
            }
            let mut end = t.len().min(FALLBACK_SUMMARY_CHARS);
            while !t.is_char_boundary(end) {
                end -= 1;
            }
            t.truncate(end);
            (t, "extract: opening text (no summary supplied)".to_string())
        }
    };
    if text.is_empty() {
        return nodes;
    }
    let mut meta = Meta::new();
    meta.insert("summary_source".into(), serde_json::Value::String(origin));
    let summary = Node {
        id: format!("{}:summary", corpus.id),
        corpus: corpus.id.clone(),
        kind: NodeKind::Summary,
        label: None,
        number: None,
        title: Some("Summary".into()),
        text,
        parent: Some(corpus.id.clone()),
        ordinal: 0,
        meta,
    };
    nodes.insert(1, summary);
    dedupe_ids(nodes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use meowlaw_core::CorpusKind;

    fn corpus() -> Corpus {
        Corpus {
            id: "tr".into(),
            title: "TR".into(),
            kind: CorpusKind::Guidance,
            jurisdiction: None,
            profile: "generic".into(),
            aliases: vec![],
            citations: vec![],
            source_url: None,
            as_at: None,
            licence: None,
            meta: Meta::new(),
        }
    }

    fn node(id: &str, parent: Option<&str>, text: &str) -> Node {
        Node {
            id: id.into(),
            corpus: "tr".into(),
            kind: if parent.is_none() {
                NodeKind::Root
            } else {
                NodeKind::Chunk
            },
            label: None,
            number: None,
            title: None,
            text: text.into(),
            parent: parent.map(String::from),
            ordinal: 0,
            meta: Meta::new(),
        }
    }

    #[test]
    fn numbered_ruling() {
        let nodes = vec![
            node("tr", None, "Taxation Ruling. Contents 1 Ruling 3"),
            node(
                "tr:a",
                Some("tr"),
                "Preamble text. 1. This Ruling explains X. 2. It does not deal with Y in 2019. Ruling",
            ),
            node(
                "tr:b",
                Some("tr"),
                "3. A company is resident. What is control? 4. Control means [1] direction.\n\n[1] Footnote 1. Not a paragraph.",
            ),
        ];
        let out = split_paragraphs(nodes, &corpus());
        let ids: Vec<&str> = out.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(
            ids,
            vec![
                "tr", "tr:a", "tr:para1", "tr:para2", "tr:b", "tr:para3", "tr:para4"
            ]
        );
        assert_eq!(out[1].text, "Preamble text.");
        assert_eq!(out[2].text, "This Ruling explains X.");
        assert!(
            out[3].text.contains("2019"),
            "a year is not a paragraph number"
        );
        assert!(
            out[6].text.contains("Footnote 1."),
            "footnotes stay with their paragraph"
        );
        assert_eq!(out[5].parent.as_deref(), Some("tr:b"));
    }

    #[test]
    fn tidies_wrapped_lines() {
        assert_eq!(
            tidy_lines(
                "test of residency\n[3]\nif it:\n•\ncarries on business, and\n•\nhas control.\n(a) item"
            ),
            "test of residency [3] if it:\n• carries on business, and\n• has control.\n(a) item"
        );
    }

    #[test]
    fn tables_survive_tidying_and_markers() {
        let table = "| Item | Rule |\n| --- | --- |\n| 1 | 2. Not a paragraph. |";
        assert_eq!(
            tidy_lines(&format!("The table:\n{table}\nAfter it.")),
            format!("The table:\n{table}\nAfter it.")
        );
        assert!(in_table_row(table, table.find("2.").unwrap()));
        assert!(!in_table_row("x\n2. Yes", 2));
    }

    #[test]
    fn split_at_markers_accepts_no_markers() {
        let out = split_at_markers("Just text.", &[]);
        assert_eq!(out, vec![(None, "Just text.".to_string())]);
    }

    #[test]
    fn unnumbered_and_summary() {
        let nodes = vec![
            node("tr", None, ""),
            node("tr:leave", Some("tr"), "Four weeks.\n\nTen days sick."),
        ];
        let out = split_paragraphs(nodes, &corpus());
        let ids: Vec<&str> = out.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(ids, vec!["tr", "tr:leave", "tr:p1", "tr:p2"]);
        let out = add_summary(out, &corpus(), SummarySource::Fallback);
        assert_eq!(out[1].id, "tr:summary");
        assert_eq!(out[1].text, "Four weeks.\n\nTen days sick.");
        let out = add_summary(
            vec![
                node("tr", None, "Smith v Jones"),
                node("tr:h", Some("tr"), "Facts here."),
            ],
            &corpus(),
            SummarySource::Body(Some("machine-generated")),
        );
        assert_eq!(out.len(), 2);
        assert_eq!(out[1].text, "Smith v Jones\n\nFacts here.");
    }
}
