//! `profile test`: run a profile over a sample file and show what it matched
//! and what it missed, without storing anything. This is the loop for writing
//! or tuning a profile for a new jurisdiction.

use anyhow::{Context, Result};
use meowlaw_core::{Document, NodeKind, ProfileRegistry};
use meowlaw_extract::{
    build_catalog, extract_definitions, extract_references_in_context, load_profiles,
};
use meowlaw_ingest::{Block, Format, Ingester, Manifest};
use meowlaw_store::Store;
use serde::Serialize;
use std::collections::{BTreeMap, HashSet};
use std::path::Path;

/// Examples kept per bucket.
const EXAMPLES: usize = 5;

#[derive(Debug, Serialize)]
pub struct ProfileReport {
    pub file: String,
    pub corpus: String,
    pub kind: String,
    pub profile: String,
    pub nodes: usize,
    /// Nodes per kind (provision, container, paragraph, ...).
    pub node_kinds: BTreeMap<String, usize>,
    /// Headings recognised, per structural label, with examples.
    pub headings: BTreeMap<String, Bucket>,
    /// Heading-like lines no level matched: candidates for a new `[[levels]]`.
    pub unmatched_headings: Bucket,
    /// Citations per pattern, with resolved / unresolved counts.
    pub citations: BTreeMap<String, CitationBucket>,
    pub definitions: Vec<DefinitionSample>,
    pub definitions_total: usize,
    pub paragraphs: usize,
    /// Plain-language suggestions.
    pub notes: Vec<String>,
}

#[derive(Debug, Default, Serialize)]
pub struct Bucket {
    pub count: usize,
    pub examples: Vec<String>,
}

impl Bucket {
    fn add(&mut self, s: impl Into<String>) {
        self.count += 1;
        if self.examples.len() < EXAMPLES {
            self.examples.push(s.into());
        }
    }
}

#[derive(Debug, Default, Serialize)]
pub struct CitationBucket {
    pub count: usize,
    pub resolved: usize,
    pub examples_resolved: Vec<String>,
    pub examples_unresolved: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct DefinitionSample {
    pub term: String,
    pub pattern: String,
    pub source: String,
    pub scope: String,
}

/// Test `profile` (or the file's own profile) on `path`. When `store` is
/// given, citations to already-ingested sources resolve too.
pub fn profile_test(
    path: &Path,
    overrides: &Manifest,
    registry: &ProfileRegistry,
    store: Option<&Store>,
) -> Result<ProfileReport> {
    let ing = Ingester::new(registry.clone());
    let doc: Document = ing
        .ingest_file(path, overrides)?
        .into_iter()
        .next()
        .context("the file produced no document")?;
    let profile = registry
        .get(&doc.corpus.profile)
        .with_context(|| format!("loading profile `{}`", doc.corpus.profile))?;

    // Headings, from the raw blocks (the tree hides what did not match).
    let mut headings: BTreeMap<String, Bucket> = BTreeMap::new();
    let mut unmatched = Bucket::default();
    let format = match &overrides.format {
        Some(f) => Format::parse(f),
        None => Format::from_path(path),
    };
    if let Some(format) = format.filter(|f| *f != Format::Json) {
        for b in meowlaw_ingest::blocks::read_blocks(path, format)? {
            match b {
                Block::Heading { text, depth } => match profile.match_heading(&text) {
                    Some(m) => headings.entry(m.label).or_default().add(text),
                    // Plain-text heading candidates are every short line; only
                    // report those that look like headings (start with a word
                    // followed by a number, or a number then a capital).
                    // Rulings, cases and books nest by heading depth, so their
                    // topic headings are expected not to match a level.
                    None if doc.corpus.kind.is_structured()
                        && (depth.is_some() || looks_like_heading(&text)) =>
                    {
                        unmatched.add(text)
                    }
                    None => {}
                },
                Block::Element {
                    label,
                    number,
                    title,
                    ..
                } => headings.entry(profile.canonical_label(&label)).or_default().add(
                    format!(
                        "{label} {} {}",
                        number.unwrap_or_default(),
                        title.unwrap_or_default()
                    )
                    .trim()
                    .to_string(),
                ),
                Block::Para(_) => {}
            }
        }
    }

    // Citations and definitions against this document plus the store.
    let mut docs: Vec<Document> = match store {
        Some(s) => s
            .documents()?
            .into_iter()
            .filter(|d| d.corpus.id != doc.corpus.id)
            .collect(),
        None => Vec::new(),
    };
    docs.push(doc);
    let profiles = load_profiles(registry, docs.iter().map(|d| &d.corpus))?;
    let catalog = build_catalog(
        docs.iter().map(|d| (&d.corpus, d.nodes.as_slice())),
        &profiles,
    );
    let doc = docs.pop().unwrap();
    let mut citations: BTreeMap<String, CitationBucket> = BTreeMap::new();
    let mut definitions = Vec::new();
    let mut used = HashSet::new();
    let mut context = None;
    for n in &doc.nodes {
        for r in extract_references_in_context(n, &doc.corpus, &profile, &catalog, &mut context) {
            let b = citations.entry(r.pattern.clone()).or_default();
            b.count += 1;
            match &r.target {
                Some(t) => {
                    b.resolved += 1;
                    if b.examples_resolved.len() < EXAMPLES {
                        b.examples_resolved.push(format!("{} → {t}", one_line(&r.raw)));
                    }
                }
                None => {
                    if b.examples_unresolved.len() < EXAMPLES {
                        b.examples_unresolved.push(one_line(&r.raw));
                    }
                }
            }
        }
        definitions.extend(extract_definitions(n, &profile, &catalog, &mut used));
    }
    let mut node_kinds: BTreeMap<String, usize> = BTreeMap::new();
    for n in &doc.nodes {
        *node_kinds.entry(n.kind.as_str().to_string()).or_default() += 1;
    }
    let paragraphs = node_kinds.get("paragraph").copied().unwrap_or(0);
    let provisions = doc
        .nodes
        .iter()
        .filter(|n| n.kind == NodeKind::Provision)
        .count();

    let mut notes = Vec::new();
    if doc.corpus.kind.is_structured() && provisions == 0 {
        notes.push(format!(
            "No provisions found. Check the `[[levels]]` patterns of profile `{}` against the headings below, or try another --profile.",
            profile.id()
        ));
    }
    if unmatched.count > 0 {
        notes.push(format!(
            "{} heading-like lines matched no level (examples under unmatched_headings). Add a `[[levels]]` entry if they are structure.",
            unmatched.count
        ));
    }
    for (name, b) in &citations {
        if b.count >= 5 && b.resolved * 2 < b.count {
            notes.push(format!(
                "Pattern `{name}`: {} of {} citations unresolved. Usually the cited source is not loaded; otherwise add an alias.",
                b.count - b.resolved,
                b.count
            ));
        }
    }
    if definitions.is_empty() && doc.corpus.kind.is_structured() {
        notes.push("No definitions found. Check `[definitions]` patterns and `section_titles`.".into());
    }
    let definitions_total = definitions.len();
    Ok(ProfileReport {
        file: path.display().to_string(),
        corpus: doc.corpus.id.clone(),
        kind: doc.corpus.kind.as_str().to_string(),
        profile: profile.id().to_string(),
        nodes: doc.nodes.len(),
        node_kinds,
        headings,
        unmatched_headings: unmatched,
        citations,
        definitions: definitions
            .into_iter()
            .take(20)
            .map(|d| DefinitionSample {
                term: d.term,
                pattern: d.pattern,
                source: d.source,
                scope: d.scope,
            })
            .collect(),
        definitions_total,
        paragraphs,
        notes,
    })
}

fn one_line(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// "Part 3 Liability", "12 Application", "ARTICLE IV": a word then a number,
/// or a number then a capitalised word, in a short line.
fn looks_like_heading(t: &str) -> bool {
    let t = t.trim();
    if t.chars().count() > 100 || t.ends_with(['.', ',', ';']) {
        return false;
    }
    let mut words = t.split_whitespace();
    let (Some(a), Some(b)) = (words.next(), words.next()) else {
        return false;
    };
    let num = |w: &str| w.chars().next().is_some_and(|c| c.is_ascii_digit()) || w.chars().all(|c| "IVXLC".contains(c));
    (a.chars().next().is_some_and(char::is_uppercase) && num(b))
        || (num(a) && b.chars().next().is_some_and(char::is_uppercase))
}

impl ProfileReport {
    /// Human-readable report for the CLI.
    pub fn render(&self) -> String {
        let mut o = String::new();
        let line = |o: &mut String, s: String| {
            o.push_str(&s);
            o.push('\n');
        };
        line(&mut o, format!("{}  →  corpus `{}` ({}), profile `{}`", self.file, self.corpus, self.kind, self.profile));
        line(&mut o, format!("nodes: {} {:?}", self.nodes, self.node_kinds));
        line(&mut o, "\nHeadings recognised".into());
        if self.headings.is_empty() {
            line(&mut o, "  (none)".into());
        }
        for (label, b) in &self.headings {
            line(&mut o, format!("  {label:<14} {:>5}   e.g. {}", b.count, b.examples.join(" | ")));
        }
        if self.unmatched_headings.count > 0 {
            line(&mut o, format!("\nHeading-like lines with no matching level: {}", self.unmatched_headings.count));
            for e in &self.unmatched_headings.examples {
                line(&mut o, format!("  {e}"));
            }
        }
        line(&mut o, "\nCitations".into());
        if self.citations.is_empty() {
            line(&mut o, "  (none)".into());
        }
        for (name, b) in &self.citations {
            line(&mut o, format!("  {name:<22} {:>5} found, {:>5} resolved", b.count, b.resolved));
            for e in &b.examples_resolved {
                line(&mut o, format!("      ✓ {e}"));
            }
            for e in &b.examples_unresolved {
                line(&mut o, format!("      ✗ {e}"));
            }
        }
        line(&mut o, format!("\nDefinitions: {}", self.definitions_total));
        for d in &self.definitions {
            line(&mut o, format!("  “{}” [{}] in {} → scope {}", d.term, d.pattern, d.source, d.scope));
        }
        if self.paragraphs > 0 {
            line(&mut o, format!("\nParagraph nodes: {}", self.paragraphs));
        }
        if !self.notes.is_empty() {
            line(&mut o, "\nNotes".into());
            for n in &self.notes {
                line(&mut o, format!("  - {n}"));
            }
        }
        o
    }
}
