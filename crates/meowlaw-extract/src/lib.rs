//! Reference and definition extraction.
//!
//! Runs after ingestion, over every node of every corpus, using each corpus's
//! profile. Produces resolved and unresolved [`Reference`]s, [`Definition`]s
//! and the graph [`Edge`]s derived from them.

pub mod catalog;
pub mod versions;

use aho_corasick::{AhoCorasick, AhoCorasickBuilder, MatchKind};
use meowlaw_core::ids::slug;
use meowlaw_core::{
    Corpus, Definition, Document, Edge, EdgeKind, Method, Node, NodeKind, Profile, ProfileRegistry,
    Reference, TargetSpec,
};
use regex::Regex;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::LazyLock;

pub use catalog::Catalog;

/// Cap on `uses_term` edges from one node, so boilerplate terms cannot flood the graph.
pub const MAX_TERM_EDGES_PER_NODE: usize = 100;

#[derive(Debug, Default, Clone)]
pub struct Extraction {
    pub references: Vec<Reference>,
    pub definitions: Vec<Definition>,
    pub edges: Vec<Edge>,
}

impl Extraction {
    pub fn stats(&self) -> ExtractionStats {
        let resolved = self
            .references
            .iter()
            .filter(|r| r.target.is_some())
            .count();
        ExtractionStats {
            references: self.references.len(),
            resolved,
            unresolved: self.references.len() - resolved,
            definitions: self.definitions.len(),
            edges: self.edges.len(),
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ExtractionStats {
    pub references: usize,
    pub resolved: usize,
    pub unresolved: usize,
    pub definitions: usize,
    pub edges: usize,
}

/// Build a catalog over a set of documents.
pub fn build_catalog<'a>(
    docs: impl IntoIterator<Item = (&'a Corpus, &'a [Node])>,
    profiles: &HashMap<String, Profile>,
) -> Catalog {
    let mut cat = Catalog::default();
    for (c, nodes) in docs {
        let aliases: Vec<(String, String)> = profiles
            .get(&c.profile)
            .map(|p| {
                p.spec
                    .corpus_aliases
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            })
            .unwrap_or_default();
        cat.add_corpus(c, &aliases);
        for n in nodes {
            cat.add_node(n);
        }
    }
    cat.superseded = meowlaw_core::versions::superseded(cat.corpora.values());
    cat
}

/// Compile the profiles used by a set of corpora.
pub fn load_profiles<'a>(
    registry: &ProfileRegistry,
    corpora: impl IntoIterator<Item = &'a Corpus>,
) -> anyhow::Result<HashMap<String, Profile>> {
    let mut out = HashMap::new();
    for c in corpora {
        if !out.contains_key(&c.profile) {
            out.insert(c.profile.clone(), registry.get(&c.profile)?);
        }
    }
    Ok(out)
}

/// Extract everything for the given documents, resolving against `catalog`
/// (which may cover more corpora than `docs`, so cross-corpus links resolve).
pub fn extract(
    docs: &[Document],
    catalog: &Catalog,
    profiles: &HashMap<String, Profile>,
) -> Extraction {
    let mut out = Extraction::default();
    for d in docs {
        let Some(profile) = profiles.get(&d.corpus.profile) else {
            continue;
        };
        for n in &d.nodes {
            if let Some(p) = &n.parent {
                out.edges.push(Edge {
                    source: p.clone(),
                    target: n.id.clone(),
                    kind: EdgeKind::Contains,
                    method: Method::Structure,
                    weight: 1.0,
                    evidence: None,
                });
            }
        }
        let mut def_ids = HashSet::new();
        // Legislation cites its own sections bare; other material carries the
        // last named Act from paragraph to paragraph, in reading order.
        let mut context = None;
        for n in &d.nodes {
            out.references.extend(extract_references_in_context(
                n,
                &d.corpus,
                profile,
                catalog,
                &mut context,
            ));
            for def in extract_definitions(n, profile, catalog, &mut def_ids) {
                out.edges.push(Edge {
                    source: def.source.clone(),
                    target: def.id.clone(),
                    kind: EdgeKind::Defines,
                    method: Method::Pattern,
                    weight: 1.0,
                    evidence: Some(def.term.clone()),
                });
                out.definitions.push(def);
            }
        }
        let corpus_defs: Vec<&Definition> = out
            .definitions
            .iter()
            .filter(|df| df.corpus == d.corpus.id)
            .collect();
        out.edges
            .extend(term_usage_edges(&d.nodes, &corpus_defs, catalog));
    }
    out.edges.extend(versions::version_edges(docs));
    // Resolve "has the meaning given by" pointers.
    let by_source: HashMap<&str, Vec<&Reference>> =
        out.references.iter().fold(HashMap::new(), |mut m, r| {
            m.entry(r.source.as_str()).or_default().push(r);
            m
        });
    // The pointer is the first resolved reference after "meaning given by"
    // inside the definition's own text, not any reference in the provision.
    let texts: HashMap<&str, &str> = docs
        .iter()
        .flat_map(|d| d.nodes.iter().map(|n| (n.id.as_str(), n.text.as_str())))
        .collect();
    for def in &mut out.definitions {
        let (Some(text), Some(rs)) = (
            texts.get(def.source.as_str()),
            by_source.get(def.source.as_str()),
        ) else {
            continue;
        };
        let Some(dstart) = text.find(def.text.as_str()) else {
            continue;
        };
        let dend = dstart + def.text.len();
        let Some(m) = def
            .text
            .find("meaning given by")
            .or_else(|| def.text.find("same meaning as"))
        else {
            continue;
        };
        let from = dstart + m;
        if let Some(r) = rs
            .iter()
            .filter(|r| r.start >= from && r.start < dend && r.target.is_some())
            .min_by_key(|r| r.start)
        {
            def.points_to = r.target.clone();
        }
    }
    let mut seen = HashSet::new();
    for r in &out.references {
        if let Some(t) = &r.target
            && t != &r.source
            && seen.insert((r.source.clone(), t.clone()))
        {
            out.edges.push(Edge {
                source: r.source.clone(),
                target: t.clone(),
                kind: EdgeKind::Cites,
                method: Method::Pattern,
                weight: 1.0,
                evidence: Some(r.raw.clone()),
            });
        }
    }
    out
}

static LIST_SPLIT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s*(?:,|\band\b|\bor\b|\bund\b|\bet\b|\bou\b)\s*").unwrap());
static RANGE_SEP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s+(?:to|bis|à)\s+|\s*–\s*").unwrap());
static RANGE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?P<a>[0-9]+)\s*(?:\bto\b|\bbis\b|\sà\s|–|\s-\s)\s*(?P<b>[0-9]+)$").unwrap()
});

/// Split `5, 6 and 8 to 10` into individual numbers. Ranges expand only when
/// both ends are plain integers less than 200 apart.
pub fn split_numbers(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    for piece in LIST_SPLIT.split(s.trim()) {
        let piece = piece.trim();
        if piece.is_empty() {
            continue;
        }
        if let Some(c) = RANGE.captures(piece) {
            let a: u32 = c["a"].parse().unwrap_or(0);
            let b: u32 = c["b"].parse().unwrap_or(0);
            if b > a && b - a < 200 {
                out.extend((a..=b).map(|n| n.to_string()));
                continue;
            }
        }
        // "8 to 12A" or other non-integer ranges: keep both ends.
        let parts: Vec<&str> = RANGE_SEP.split(piece).collect();
        out.extend(parts.into_iter().map(|p| p.trim().to_string()));
    }
    out
}

/// What a citation in text refers to, before resolution.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Cited {
    Provision {
        label: Option<String>,
        numbers: Vec<String>,
        /// Corpus named in or right after the citation, as written.
        corpus_name: Option<String>,
        relative: bool,
        /// Table item pinpoint ("item 3 of the table in section 12").
        #[serde(skip_serializing_if = "Option::is_none")]
        item: Option<String>,
    },
    /// A case or other cited document (ruling), optionally with a paragraph
    /// pinpoint ("[2026] HCA 18 at [30]", "TR 2018/5 para 12").
    Case {
        citation: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        pin: Option<String>,
    },
    Corpus {
        name: String,
    },
}

/// A citation found in text by a profile pattern.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ParsedCitation {
    pub raw: String,
    pub start: usize,
    pub end: usize,
    pub pattern: String,
    pub cited: Cited,
}

/// A corpus group can start a word early ("See Income Tax Act 2004"). The
/// regex crate has no look-ahead, so drop a leading sentence word here.
fn strip_leading_stop_word(name: &str) -> String {
    const STOP: [&str; 14] = [
        "See", "Under", "In", "By", "Pursuant", "Whereas", "And", "But", "If", "Where", "When",
        "This", "That", "Per",
    ];
    let mut s = name.trim();
    loop {
        let mut stripped = false;
        for w in STOP {
            if let Some(rest) = s.strip_prefix(w).and_then(|r| r.strip_prefix(' ')) {
                s = rest.trim_start();
                stripped = true;
                break;
            }
        }
        if !stripped {
            return s.to_string();
        }
    }
}

/// Find citations in `text`. Patterns are tried in profile order (a child
/// profile's patterns first); a span claimed by one pattern is not re-matched.
/// Corpus-naming patterns run first so they can give later citations context.
/// When `catalog` is given, a known corpus alias directly after a citation
/// ("s 8-1 ITAA 1997") is absorbed into it.
pub fn parse_citations(
    text: &str,
    profile: &Profile,
    catalog: Option<&Catalog>,
) -> Vec<ParsedCitation> {
    let mut claimed: Vec<(usize, usize)> = Vec::new();
    let mut found = Vec::new();
    let mut ordered: Vec<&meowlaw_core::profile::Citation> = profile.citations.iter().collect();
    ordered.sort_by_key(|c| !matches!(c.target, TargetSpec::Corpus));
    for cit in ordered {
        for cap in cit.re.captures_iter(text) {
            let m = cap.get(0).unwrap();
            let (start, mut end) = (m.start(), m.end());
            if claimed.iter().any(|(s, e)| start < *e && end > *s) {
                continue;
            }
            // "entity's CGT assets": "s" after an apostrophe is a possessive,
            // not "s CGT". Elision ("l'article 5") is still a citation.
            if text[..start].ends_with(['\'', '’'])
                && m.as_str().starts_with('s')
                && m.as_str()[1..].starts_with(char::is_whitespace)
            {
                continue;
            }
            let cited = match &cit.target {
                TargetSpec::Corpus => Cited::Corpus {
                    name: cap
                        .name("corpus")
                        .map(|c| c.as_str())
                        .unwrap_or(m.as_str())
                        .to_string(),
                },
                TargetSpec::Case => Cited::Case {
                    citation: cap
                        .name("cite")
                        .map(|c| c.as_str())
                        .unwrap_or(m.as_str())
                        .to_string(),
                    pin: cap.name("pin").map(|p| p.as_str().to_string()),
                },
                TargetSpec::Provision { label } | TargetSpec::Relative { label } => {
                    let relative = matches!(cit.target, TargetSpec::Relative { .. });
                    let label = cap
                        .name("label")
                        .map(|l| profile.canonical_label(l.as_str()))
                        .or_else(|| label.clone());
                    let nums = cap
                        .name("nums")
                        .or_else(|| cap.name("num"))
                        .map(|n| n.as_str())
                        .unwrap_or("");
                    let mut corpus_name = cap
                        .name("corpus")
                        .map(|c| strip_leading_stop_word(c.as_str()));
                    if corpus_name.is_none()
                        && !relative
                        && let Some(cat) = catalog
                        && let Some((_, len)) = cat.corpus_suffix(&text[end..])
                    {
                        corpus_name = Some(text[end..end + len].trim().to_string());
                        end += len;
                    }
                    Cited::Provision {
                        label,
                        numbers: split_numbers(nums),
                        corpus_name,
                        relative,
                        item: None,
                    }
                }
            };
            claimed.push((start, end));
            found.push(ParsedCitation {
                raw: text[start..end].to_string(),
                start,
                end,
                pattern: cit.name.clone(),
                cited,
            });
        }
    }
    table_items(text, profile, &mut found);
    found.sort_by_key(|c| c.start);
    found
}

/// "item 3 of the table in section 12": join the item to the provision
/// citation that follows (pinning it), or, with nothing after it, cite the
/// citing provision's own table.
fn table_items(text: &str, profile: &Profile, found: &mut Vec<ParsedCitation>) {
    let Some(item_re) = &profile.table_item else {
        return;
    };
    for cap in item_re.captures_iter(text) {
        let m = cap.get(0).unwrap();
        let Some(item) = cap.name("item").map(|i| i.as_str().to_string()) else {
            continue;
        };
        let joined_at = profile
            .table_link
            .as_ref()
            .and_then(|l| l.find(&text[m.end()..]))
            .map(|l| m.end() + l.end());
        // A pattern may have claimed "item 3" on its own; the table form wins.
        found.retain(|c| !(c.start >= m.start() && c.end <= m.end()));
        let next = joined_at.and_then(|at| {
            found.iter().position(|c| {
                c.start == at
                    && matches!(
                        &c.cited,
                        Cited::Provision {
                            relative: false,
                            item: None,
                            ..
                        }
                    )
            })
        });
        match next {
            Some(i) => {
                let c = &mut found[i];
                c.start = m.start();
                c.raw = text[c.start..c.end].to_string();
                if let Cited::Provision { item: slot, .. } = &mut c.cited {
                    *slot = Some(item);
                }
            }
            None if !found.iter().any(|c| m.start() < c.end && m.end() > c.start) => {
                found.push(ParsedCitation {
                    raw: m.as_str().to_string(),
                    start: m.start(),
                    end: m.end(),
                    pattern: "table_item".into(),
                    cited: Cited::Provision {
                        label: None,
                        numbers: Vec::new(),
                        corpus_name: None,
                        relative: true,
                        item: Some(item),
                    },
                });
            }
            None => {}
        }
    }
}

/// Remove every citation the profiles recognise, for embedding: the vector
/// should capture what a document is about, not which sections it lists.
/// Leftover connectives ("of the", "(Cth)", empty brackets, doubled
/// punctuation) are cleaned up.
pub fn strip_citations(text: &str, profiles: &[&Profile]) -> String {
    let mut spans: Vec<(usize, usize)> = Vec::new();
    for p in profiles {
        for pc in parse_citations(text, p, None) {
            spans.push((pc.start, pc.end));
        }
    }
    spans.sort();
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    for (s, e) in spans {
        if s < pos {
            pos = pos.max(e);
            continue;
        }
        out.push_str(&text[pos..s]);
        out.push(' ');
        pos = e;
    }
    out.push_str(&text[pos..]);
    static TIDY: LazyLock<Vec<(Regex, &'static str)>> = LazyLock::new(|| {
        [
            (r"\(\s*[,;]?\s*\)|\[\s*\]", ""),
            (r"[ \t]+([,;.:)])", "$1"),
            // A preposition left dangling where a citation was removed.
            (r"\s+(?:under|of the|of|in|by|see|cf)([,;.)])", "$1"),
            (r"([,;])\s*([,;.])", "$2"),
            (r"[ \t]{2,}", " "),
        ]
        .into_iter()
        .map(|(p, r)| (Regex::new(p).unwrap(), r))
        .collect()
    });
    let mut t = out;
    // Jurisdiction tags left behind by a removed Act name, from the profiles.
    for p in profiles {
        for tag in &p.spec.jurisdiction_tags {
            t = t.replace(&format!("({tag})"), "");
        }
    }
    for (re, rep) in TIDY.iter() {
        t = re.replace_all(&t, *rep).to_string();
    }
    t.trim().to_string()
}

/// Extract and resolve references from one node.
pub fn extract_references(
    node: &Node,
    corpus: &Corpus,
    profile: &Profile,
    catalog: &Catalog,
) -> Vec<Reference> {
    extract_references_in_context(node, corpus, profile, catalog, &mut None)
}

/// As [`extract_references`], with the most recently named corpus carried in
/// from earlier nodes. Judgments and rulings often name the Act once, then
/// cite bare sections in later paragraphs.
pub fn extract_references_in_context(
    node: &Node,
    corpus: &Corpus,
    profile: &Profile,
    catalog: &Catalog,
    context: &mut Option<String>,
) -> Vec<Reference> {
    let self_structured = corpus.kind.is_structured();
    let mut out = Vec::new();
    for pc in parse_citations(&node.text, profile, Some(catalog)) {
        let pin = match &pc.cited {
            Cited::Provision { item, .. } => item.clone(),
            _ => None,
        };
        let mut push = |raw: String, target: Option<String>| {
            out.push(Reference {
                source: node.id.clone(),
                raw,
                start: pc.start,
                end: pc.end,
                pattern: pc.pattern.clone(),
                target,
                pin: pin.clone(),
            })
        };
        match &pc.cited {
            Cited::Corpus { name } => {
                let t = catalog
                    .corpus_by_name(name)
                    .map(|c| catalog.prefer_version(c, corpus));
                if t.is_some() {
                    *context = t.clone();
                }
                push(pc.raw.clone(), t);
            }
            Cited::Case { citation, pin } => {
                let t = catalog.document(citation, pin.as_deref());
                push(pc.raw.clone(), t);
            }
            Cited::Provision {
                label,
                numbers,
                corpus_name,
                relative,
                ..
            } => {
                if *relative {
                    let t = catalog
                        .enclosing_provision(&node.id)
                        .or_else(|| Some(node.id.clone()));
                    push(pc.raw.clone(), t);
                    continue;
                }
                let named = corpus_name.as_deref().and_then(|name| {
                    // "this Act", "this Ruling", "this judgment": the citing corpus.
                    if name.to_lowercase().starts_with("this ") {
                        Some(corpus.id.clone())
                    } else {
                        catalog
                            .corpus_by_name(name)
                            .or_else(|| catalog.corpus_suffix(name).map(|(id, _)| id))
                            .map(|c| catalog.prefer_version(c, corpus))
                    }
                });
                // Naming any Act, loaded or not, changes the Act later bare
                // citations in this node refer to.
                if corpus_name.is_some() {
                    *context = named.clone();
                }
                let target_corpus = named.or_else(|| {
                    if corpus_name.is_some() {
                        // Named a corpus we do not hold: never guess.
                        None
                    } else {
                        // In legislation a bare "section X" means this Act (drafting
                        // convention); elsewhere use the most recently named corpus.
                        if self_structured {
                            Some(corpus.id.clone())
                        } else {
                            context.clone()
                        }
                    }
                });
                let many = numbers.len() > 1;
                for num in numbers {
                    let t = target_corpus.as_deref().and_then(|c| {
                        catalog
                            .provision(c, label.as_deref(), num)
                            .map(str::to_string)
                    });
                    let raw = if many {
                        match label {
                            Some(l) => format!("{l} {num}"),
                            None => num.clone(),
                        }
                    } else {
                        pc.raw.clone()
                    };
                    push(raw, t);
                }
            }
        }
    }
    out
}

/// Extract definitions from one node.
pub fn extract_definitions(
    node: &Node,
    profile: &Profile,
    catalog: &Catalog,
    used_ids: &mut HashSet<String>,
) -> Vec<Definition> {
    let mut out = Vec::new();
    if node.text.is_empty() {
        return out;
    }
    let is_def_section = profile
        .definitions_title
        .as_ref()
        .is_some_and(|re| node.title.as_deref().is_some_and(|t| re.is_match(t)));
    let stop: HashSet<String> = profile
        .spec
        .definitions
        .stop_terms
        .iter()
        .map(|s| s.to_lowercase())
        .collect();
    let max_words = profile.spec.definitions.max_term_words;
    let node_scope_phrase = profile.scope_phrase.as_ref().and_then(|re| {
        re.captures(head(&node.text, 400))
            .and_then(|c| c.name("label").map(|l| l.as_str().to_string()))
    });

    let mut seen_terms = HashSet::new();
    for para in node.text.split("\n\n") {
        let para_scope = profile.scope_phrase.as_ref().and_then(|re| {
            re.captures(para)
                .and_then(|c| c.name("label").map(|l| l.as_str().to_string()))
        });
        let lines: Vec<&str> = para.lines().collect();
        for (li, line) in lines.iter().enumerate() {
            for pat in &profile.definitions {
                if pat.definitions_section_only && !is_def_section {
                    continue;
                }
                let unanchored = !pat.re.as_str().starts_with('^');
                let hay = if unanchored { para } else { line };
                if unanchored && li > 0 {
                    continue;
                }
                for cap in pat.re.captures_iter(hay) {
                    let Some(term) = cap.name("term").map(|t| clean_term(t.as_str())) else {
                        continue;
                    };
                    if !valid_term(&term, &stop, max_words) {
                        continue;
                    }
                    let key = term.to_lowercase();
                    if !seen_terms.insert(key.clone()) {
                        continue;
                    }
                    let def_text = if unanchored {
                        sentence_around(para, cap.name("term").map(|t| t.start()).unwrap_or(0))
                    } else {
                        // This line plus following lines until the next definition-looking line.
                        let mut t = vec![*line];
                        for next in &lines[li + 1..] {
                            if profile
                                .definitions
                                .iter()
                                .any(|p| p.re.as_str().starts_with('^') && p.re.is_match(next))
                            {
                                break;
                            }
                            t.push(next);
                        }
                        t.join("\n")
                    };
                    let scope_label = para_scope.clone().or_else(|| node_scope_phrase.clone());
                    let scope = match scope_label {
                        Some(l) => catalog.enclosing_labelled(&node.id, &l),
                        None => None,
                    }
                    .unwrap_or_else(|| default_scope(node, is_def_section, &pat.name, catalog));
                    let base = format!("def:{}:{}", node.corpus, slug(&term));
                    let mut id = base.clone();
                    let mut i = 2;
                    while !used_ids.insert(id.clone()) {
                        id = format!("{base}~{i}");
                        i += 1;
                    }
                    out.push(Definition {
                        id,
                        term,
                        source: node.id.clone(),
                        scope,
                        corpus: node.corpus.clone(),
                        text: def_text.trim().to_string(),
                        pattern: pat.name.clone(),
                        points_to: None,
                    });
                    if !unanchored {
                        break;
                    }
                }
            }
        }
    }
    out
}

fn default_scope(node: &Node, is_def_section: bool, pattern: &str, catalog: &Catalog) -> String {
    let root = catalog
        .ancestry(&node.id)
        .last()
        .cloned()
        .unwrap_or_else(|| node.corpus.clone());
    if is_def_section || pattern == "parenthetical" || node.kind != NodeKind::Provision {
        root
    } else {
        node.id.clone()
    }
}

/// Longest prefix of `s` of at most `max` bytes that ends on a char boundary.
fn head(s: &str, max: usize) -> &str {
    let mut end = s.len().min(max);
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

fn clean_term(s: &str) -> String {
    s.trim()
        .trim_matches(|c: char| matches!(c, '*' | '"' | '“' | '”' | '‘' | '’' | '\'' | '_' | ','))
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn valid_term(term: &str, stop: &HashSet<String>, max_words: usize) -> bool {
    let words = term.split_whitespace().count();
    term.chars().count() >= 2
        && words >= 1
        && words <= max_words
        && !stop.contains(&term.to_lowercase())
        && term.chars().any(|c| c.is_alphabetic())
        && !term.ends_with(['.', ':', ';'])
        && !is_enumerator(term)
}

/// "(a)", "(ii)", "(4A)", "3": paragraph labels, never defined terms.
fn is_enumerator(term: &str) -> bool {
    let wrapped = term.starts_with('(') && term.ends_with(')');
    let core = term.trim_start_matches('(').trim_end_matches([')', '.']);
    (wrapped && core.len() <= 5 && core.chars().all(|c| c.is_ascii_alphanumeric()))
        || (!core.is_empty() && core.chars().all(|c| c.is_ascii_digit()))
}

fn sentence_around(para: &str, pos: usize) -> String {
    let start = para[..pos].rfind(['.', '\n']).map(|i| i + 1).unwrap_or(0);
    let end = para[pos..]
        .find(". ")
        .map(|i| pos + i + 1)
        .unwrap_or(para.len());
    para[start..end].trim().to_string()
}

/// `uses_term` edges: a node inside a definition's scope whose text contains the term.
pub fn term_usage_edges(nodes: &[Node], defs: &[&Definition], catalog: &Catalog) -> Vec<Edge> {
    if defs.is_empty() {
        return Vec::new();
    }
    // One pattern per distinct term; a term can have several definitions with
    // different scopes (s 6-5 for itself, s 995-1 for the whole Act).
    let mut terms: Vec<String> = Vec::new();
    let mut defs_of: Vec<Vec<usize>> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    for (i, d) in defs.iter().enumerate() {
        let key = d.term.to_lowercase();
        let t = *index.entry(key.clone()).or_insert_with(|| {
            terms.push(key);
            defs_of.push(Vec::new());
            terms.len() - 1
        });
        defs_of[t].push(i);
    }
    // Standard + overlapping: a longer term that fails the word-boundary test
    // ("net income" inside "cabinet income") must not hide a shorter one.
    let Ok(ac): Result<AhoCorasick, _> = AhoCorasickBuilder::new()
        .ascii_case_insensitive(true)
        .match_kind(MatchKind::Standard)
        .build(&terms)
    else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for n in nodes {
        if n.text.is_empty() {
            continue;
        }
        let ancestry = catalog.ancestry(&n.id);
        let mut hit: BTreeSet<usize> = BTreeSet::new();
        for m in ac.find_overlapping_iter(&n.text) {
            if word_boundary(&n.text, m.start(), m.end()) {
                hit.insert(m.pattern().as_usize());
            }
        }
        let mut count = 0;
        for t in hit {
            // The definition with the innermost scope that covers this node.
            let best = defs_of[t]
                .iter()
                .map(|&i| defs[i])
                .filter(|d| d.source != n.id)
                .filter_map(|d| ancestry.iter().position(|a| *a == d.scope).map(|p| (p, d)))
                .min_by_key(|(p, _)| *p);
            let Some((_, d)) = best else { continue };
            out.push(Edge {
                source: n.id.clone(),
                target: d.id.clone(),
                kind: EdgeKind::UsesTerm,
                method: Method::Pattern,
                weight: 1.0,
                evidence: Some(d.term.clone()),
            });
            count += 1;
            if count >= MAX_TERM_EDGES_PER_NODE {
                break;
            }
        }
    }
    out
}

/// True when `text[start..end]` is a whole word (Unicode-aware).
fn word_boundary(text: &str, start: usize, end: usize) -> bool {
    let is_word = |c: char| c.is_alphanumeric() || c == '_';
    let before = text[..start]
        .chars()
        .next_back()
        .is_none_or(|c| !is_word(c));
    let after = text[end..].chars().next().is_none_or(|c| !is_word(c));
    before && after
}

#[cfg(test)]
mod tests;
