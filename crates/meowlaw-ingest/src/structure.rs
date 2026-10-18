//! Build a node tree from blocks, driven by the corpus kind and profile.

use meowlaw_core::ids::{number_slug, slug};
use meowlaw_core::{Corpus, CorpusKind, Meta, Node, NodeKind, Profile};
use regex::Regex;
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use crate::blocks::Block;

/// Split a chunk of secondary material once it passes this many bytes.
pub const MAX_CHUNK_BYTES: usize = 6_000;

pub fn structure(corpus: &Corpus, profile: &Profile, blocks: Vec<Block>) -> Vec<Node> {
    match corpus.kind {
        CorpusKind::Case => structure_case(corpus, profile, blocks),
        CorpusKind::Secondary | CorpusKind::Guidance => structure_chunks(corpus, profile, blocks),
        _ => structure_provisions(corpus, profile, blocks),
    }
}

fn root(corpus: &Corpus) -> Node {
    Node {
        id: corpus.id.clone(),
        corpus: corpus.id.clone(),
        kind: NodeKind::Root,
        label: None,
        number: None,
        title: Some(corpus.title.clone()),
        text: String::new(),
        parent: None,
        ordinal: 0,
        meta: Meta::new(),
    }
}

fn append(text: &mut String, para: &str) {
    if !text.is_empty() {
        text.push_str("\n\n");
    }
    text.push_str(para.trim());
}

struct Builder<'a> {
    corpus: &'a Corpus,
    nodes: Vec<Node>,
    used: HashSet<String>,
    /// Container path segments from the root to each node index (inclusive).
    paths: Vec<Vec<String>>,
    child_count: HashMap<usize, u32>,
}

impl<'a> Builder<'a> {
    fn new(corpus: &'a Corpus) -> Self {
        let mut used = HashSet::new();
        used.insert(corpus.id.clone());
        Self {
            corpus,
            nodes: vec![root(corpus)],
            used,
            paths: vec![Vec::new()],
            child_count: HashMap::new(),
        }
    }

    /// Container path segments from the root down to `idx` (cached per node).
    fn ancestry_path(&self, idx: usize) -> Vec<String> {
        self.paths.get(idx).cloned().unwrap_or_default()
    }

    fn unique(&mut self, candidates: Vec<String>) -> String {
        for c in &candidates {
            if !self.used.contains(c) {
                self.used.insert(c.clone());
                return c.clone();
            }
        }
        let base = candidates
            .last()
            .cloned()
            .unwrap_or_else(|| self.corpus.id.clone());
        let mut n = 2;
        loop {
            let c = format!("{base}~{n}");
            if !self.used.contains(&c) {
                self.used.insert(c.clone());
                return c;
            }
            n += 1;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        parent: usize,
        kind: NodeKind,
        label: Option<String>,
        number: Option<String>,
        title: Option<String>,
        seg: String,
        flat_id: bool,
    ) -> usize {
        let path = self.ancestry_path(parent);
        let nested = if path.is_empty() {
            format!("{}:{}", self.corpus.id, seg)
        } else {
            format!("{}:{}/{}", self.corpus.id, path.join("/"), seg)
        };
        let id = if flat_id {
            self.unique(vec![format!("{}:{}", self.corpus.id, seg), nested])
        } else {
            self.unique(vec![nested])
        };
        let ord = self.child_count.entry(parent).or_insert(0);
        *ord += 1;
        let ordinal = *ord;
        let parent_id = self.nodes[parent].id.clone();
        self.nodes.push(Node {
            id,
            corpus: self.corpus.id.clone(),
            kind,
            label,
            number,
            title,
            text: String::new(),
            parent: Some(parent_id),
            ordinal,
            meta: Meta::new(),
        });
        let idx = self.nodes.len() - 1;
        let mut p = path;
        if !flat_id {
            p.push(seg);
        }
        self.paths.push(p);
        idx
    }
}

fn default_kind_for_label(label: &str) -> NodeKind {
    match label.to_ascii_lowercase().as_str() {
        "section" | "article" | "rule" | "regulation" | "clause" | "paragraph" | "artikel" => {
            NodeKind::Provision
        }
        "schedule" | "annex" | "appendix" => NodeKind::Schedule,
        _ => NodeKind::Container,
    }
}

/// Legislation, instruments, rule sets, treaties.
fn structure_provisions(corpus: &Corpus, profile: &Profile, blocks: Vec<Block>) -> Vec<Node> {
    let mut b = Builder::new(corpus);
    // (rank, node index)
    let mut stack: Vec<(u16, usize)> = Vec::new();
    for block in blocks {
        let heading = match &block {
            Block::Element {
                depth,
                label,
                number,
                title,
            } => {
                let canon = profile.canonical_label(label);
                let level = profile
                    .levels
                    .iter()
                    .find(|l| l.spec.label.eq_ignore_ascii_case(&canon));
                // XML already encodes nesting: rank by element depth, not profile rank,
                // so levels the profile does not define (Subtitle, Subchapter) nest correctly.
                Some((
                    *depth as u16,
                    level
                        .map(|l| l.spec.kind)
                        .unwrap_or_else(|| default_kind_for_label(&canon)),
                    canon,
                    number.clone(),
                    title.clone(),
                ))
            }
            Block::Heading { text, .. } => profile
                .match_heading(&strip_md(text))
                .map(|m| (m.rank as u16, m.kind, m.label, m.number, m.title)),
            Block::Para(_) => None,
        };
        match heading {
            Some((rank, kind, label, number, title)) => {
                while stack.last().is_some_and(|(r, _)| *r >= rank) {
                    stack.pop();
                }
                let parent = stack.last().map(|(_, i)| *i).unwrap_or(0);
                let abbrev = profile.abbrev_for(&label);
                let seg = match &number {
                    Some(n) => format!("{abbrev}{}", number_slug(n)),
                    None => format!("{abbrev}-{}", b.child_count.get(&parent).unwrap_or(&0) + 1),
                };
                let idx = b.push(
                    parent,
                    kind,
                    Some(label),
                    number,
                    title,
                    seg,
                    kind == NodeKind::Provision,
                );
                stack.push((rank, idx));
            }
            None => {
                let cur = stack.last().map(|(_, i)| *i).unwrap_or(0);
                let text = match block {
                    Block::Para(t) => t,
                    Block::Heading {
                        depth: Some(_),
                        text,
                    } => format!("**{}**", strip_md(&text)),
                    Block::Heading { depth: None, text } => text,
                    Block::Element { title, .. } => title.unwrap_or_default(),
                };
                append(&mut b.nodes[cur].text, &text);
            }
        }
    }
    b.nodes
}

fn strip_md(s: &str) -> String {
    s.trim()
        .trim_matches('*')
        .trim_matches('_')
        .trim()
        .to_string()
}

/// Judgments: unnumbered prelude on the root, headings as containers,
/// sequentially numbered paragraphs as paragraph nodes.
fn structure_case(corpus: &Corpus, profile: &Profile, blocks: Vec<Block>) -> Vec<Node> {
    let mut b = Builder::new(corpus);
    let para_re = profile.case_paragraph.clone();
    let mut last_num: u32 = 0;
    let mut current_section: usize = 0;
    let mut current: usize = 0;
    for block in blocks {
        // Plain-text heading candidates are just lines in a judgment.
        let block = match block {
            Block::Heading { depth: None, text } => Block::Para(text),
            b => b,
        };
        match block {
            Block::Heading { text, .. }
            | Block::Element {
                title: Some(text), ..
            } => {
                if last_num == 0 && current_section == 0 {
                    // Headings before the first paragraph (parties, catchwords) stay on the root.
                    append(&mut b.nodes[0].text, &format!("**{}**", strip_md(&text)));
                    continue;
                }
                let seg = format!("h-{}", slug(&text).chars().take(40).collect::<String>());
                let idx = b.push(
                    0,
                    NodeKind::Container,
                    None,
                    None,
                    Some(strip_md(&text)),
                    seg,
                    true,
                );
                current_section = idx;
                current = idx;
            }
            Block::Element { .. } => {}
            Block::Para(text) => {
                let num = para_re
                    .as_ref()
                    .and_then(|re| re.captures(&text))
                    .and_then(|c| c.name("num"))
                    .and_then(|m| m.as_str().parse::<u32>().ok())
                    // Sequential numbering only (gaps of up to 3 tolerated), so a
                    // quoted "1. 2." list or a year at line start is not a paragraph.
                    .filter(|n| {
                        (last_num == 0 && *n == 1) || (*n > last_num && *n <= last_num + 3)
                    });
                match num {
                    Some(n) => {
                        last_num = n;
                        let idx = b.push(
                            current_section,
                            NodeKind::Paragraph,
                            None,
                            Some(n.to_string()),
                            None,
                            format!("para{n}"),
                            true,
                        );
                        b.nodes[idx].text = strip_para_number(&text);
                        current = idx;
                    }
                    None => append(&mut b.nodes[current].text, &text),
                }
            }
        }
    }
    b.nodes
}

fn strip_para_number(text: &str) -> String {
    static RE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\s*\[?[0-9]{1,4}\]?[.)]?\s+").unwrap());
    RE.replace(text, "").to_string()
}

/// Textbooks, handbooks, guidance: headings nest by depth; long chunks split.
fn structure_chunks(corpus: &Corpus, profile: &Profile, blocks: Vec<Block>) -> Vec<Node> {
    static NUMBERED: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^(?P<num>[0-9]+(?:\.[0-9]+)*)\.?\s+(?P<title>[A-Z][^.]{1,150})$").unwrap()
    });
    static CAPS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^[A-Z][A-Z0-9 ,'&:()\-]{3,100}$").unwrap());
    let mut b = Builder::new(corpus);
    let mut stack: Vec<(u8, usize)> = Vec::new();
    let mut blocks = blocks;
    // A leading heading that repeats the corpus title is the title, not a chunk.
    if let Some(Block::Heading { text, .. }) = blocks.first()
        && strip_md(text).eq_ignore_ascii_case(corpus.title.trim())
    {
        blocks.remove(0);
    }
    for block in blocks {
        let heading = match &block {
            Block::Heading {
                depth: Some(d),
                text,
            } => Some((*d, strip_md(text))),
            Block::Heading { depth: None, text } => {
                let t = text.trim();
                if let Some(c) = NUMBERED.captures(t) {
                    let depth = c["num"].matches('.').count() as u8 + 1;
                    Some((depth, t.to_string()))
                } else if CAPS.is_match(t) && t.chars().any(|c| c.is_alphabetic()) {
                    Some((1, t.to_string()))
                } else {
                    None
                }
            }
            Block::Element {
                depth,
                title,
                label,
                number,
            } => Some((
                *depth,
                title.clone().unwrap_or_else(|| {
                    format!("{label} {}", number.clone().unwrap_or_default())
                        .trim()
                        .to_string()
                }),
            )),
            Block::Para(_) => None,
        };
        match heading {
            Some((depth, text)) => {
                while stack.last().is_some_and(|(d, _)| *d >= depth) {
                    stack.pop();
                }
                let parent = stack.last().map(|(_, i)| *i).unwrap_or(0);
                let m = profile.match_heading(&text);
                let seg = slug(&text).chars().take(48).collect::<String>();
                let seg = if seg.is_empty() {
                    "chunk".to_string()
                } else {
                    seg
                };
                let idx = b.push(
                    parent,
                    NodeKind::Chunk,
                    m.as_ref().map(|m| m.label.clone()),
                    m.as_ref().and_then(|m| m.number.clone()),
                    Some(m.as_ref().and_then(|m| m.title.clone()).unwrap_or(text)),
                    seg,
                    false,
                );
                stack.push((depth, idx));
            }
            None => {
                let cur = stack.last().map(|(_, i)| *i).unwrap_or(0);
                let text = match block {
                    Block::Para(t) => t,
                    Block::Heading { text, .. } => text,
                    Block::Element { .. } => String::new(),
                };
                append(&mut b.nodes[cur].text, &text);
            }
        }
    }
    split_long_chunks(b.nodes)
}

fn split_long_chunks(nodes: Vec<Node>) -> Vec<Node> {
    let mut used: HashSet<String> = nodes.iter().map(|n| n.id.clone()).collect();
    let mut out = Vec::with_capacity(nodes.len());
    for mut n in nodes {
        if n.text.len() <= MAX_CHUNK_BYTES {
            out.push(n);
            continue;
        }
        let paras: Vec<&str> = n.text.split("\n\n").collect();
        let mut pieces: Vec<String> = Vec::new();
        let mut cur = String::new();
        for p in paras {
            if !cur.is_empty() && cur.len() + p.len() > MAX_CHUNK_BYTES {
                pieces.push(std::mem::take(&mut cur));
            }
            append(&mut cur, p);
        }
        if !cur.is_empty() {
            pieces.push(cur);
        }
        n.text = pieces.remove(0);
        let parent_id = n.id.clone();
        let title = n.title.clone();
        let corpus = n.corpus.clone();
        out.push(n);
        for (i, text) in pieces.into_iter().enumerate() {
            out.push(Node {
                id: {
                    let mut id = format!("{parent_id}/p{}", i + 2);
                    while !used.insert(id.clone()) {
                        id.push('~');
                    }
                    id
                },
                corpus: corpus.clone(),
                kind: NodeKind::Chunk,
                label: None,
                number: None,
                title: title.as_ref().map(|t| format!("{t} (part {})", i + 2)),
                text,
                parent: Some(parent_id.clone()),
                ordinal: 10_000 + i as u32,
                meta: Meta::new(),
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::blocks::markdown;
    use meowlaw_core::ProfileRegistry;

    fn corpus(kind: CorpusKind) -> Corpus {
        Corpus {
            id: "ex".into(),
            title: "Example Act 2024".into(),
            kind,
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

    #[test]
    fn act_tree() {
        let p = ProfileRegistry::builtin().unwrap().get("generic").unwrap();
        let md = "# Part 1 Preliminary\n\n## Section 1 Short title\n\nThis Act is the Example Act 2024.\n\n## Section 2 Definitions\n\n\"widget\" means a thing.\n\n# Part 2 Rules\n\n## Section 3 Duty\n\nA person must not sell a widget.\n\n# Schedule 1 Forms\n\n## Section 1 Form A\n\nForm text.\n";
        let nodes = structure(&corpus(CorpusKind::Legislation), &p, markdown(md));
        let ids: Vec<&str> = nodes.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(
            ids,
            vec![
                "ex",
                "ex:pt1",
                "ex:s1",
                "ex:s2",
                "ex:pt2",
                "ex:s3",
                "ex:sch1",
                "ex:sch1/s1"
            ]
        );
        assert_eq!(nodes[2].parent.as_deref(), Some("ex:pt1"));
        assert_eq!(nodes[3].text, "\"widget\" means a thing.");
        assert_eq!(nodes[7].parent.as_deref(), Some("ex:sch1"));
    }

    #[test]
    fn case_paragraphs() {
        let p = ProfileRegistry::builtin().unwrap().get("generic").unwrap();
        let md = "Smith v Jones\n\nCATCHWORDS: contract\n\n1 The appellant appeals.\n\nQuoted passage.\n\n# Consideration\n\n2 The appeal fails.\n\n2019 was a long year.\n";
        let nodes = structure(&corpus(CorpusKind::Case), &p, markdown(md));
        let ids: Vec<&str> = nodes.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(
            ids,
            vec!["ex", "ex:para1", "ex:h-consideration", "ex:para2"]
        );
        assert!(nodes[1].text.contains("Quoted passage"));
        assert!(nodes[3].text.contains("2019 was"));
        assert_eq!(nodes[3].parent.as_deref(), Some("ex:h-consideration"));
    }

    #[test]
    fn handbook_chunks() {
        let p = ProfileRegistry::builtin().unwrap().get("generic").unwrap();
        let md = "# Leave\n\nIntro.\n\n## Annual leave\n\nFour weeks.\n\n## Sick leave\n\nTen days.\n\n# Conduct\n\nBe nice.\n";
        let nodes = structure(&corpus(CorpusKind::Secondary), &p, markdown(md));
        let ids: Vec<&str> = nodes.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(
            ids,
            vec![
                "ex",
                "ex:leave",
                "ex:leave/annual-leave",
                "ex:leave/sick-leave",
                "ex:conduct"
            ]
        );
    }
}
