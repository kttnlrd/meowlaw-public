//! Ingestion: any file or directory → [`Document`]s (a corpus plus its node tree).
//!
//! Corpus metadata comes from, in increasing priority:
//! 1. defaults derived from the file name and first heading,
//! 2. a `corpus.toml` in the directory (multi-file corpus) or a sidecar
//!    `<file>.meta.toml` / `<file>.meta.json`,
//! 3. TOML front matter between `+++` lines (Markdown and text),
//! 4. explicit overrides from the caller (CLI flags).

pub mod blocks;
pub mod paragraphs;
pub mod structure;
pub mod tables;

use anyhow::{Context, Result, bail};
use meowlaw_core::ids::{citation_key, slug};
use meowlaw_core::{Corpus, CorpusKind, Document, Meta, ProfileRegistry, TargetSpec};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub use blocks::{Block, Format};

/// Partial corpus metadata. Every field is optional so layers can be merged.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Manifest {
    pub id: Option<String>,
    pub title: Option<String>,
    pub kind: Option<String>,
    pub jurisdiction: Option<String>,
    pub profile: Option<String>,
    #[serde(default)]
    pub aliases: Vec<String>,
    #[serde(default)]
    pub citations: Vec<String>,
    pub source_url: Option<String>,
    pub as_at: Option<String>,
    pub licence: Option<String>,
    /// Multi-file corpus: files in reading order, relative to the manifest.
    #[serde(default)]
    pub files: Vec<String>,
    /// Force an input format instead of inferring it from the extension.
    pub format: Option<String>,
    /// Summary of the material (rulings, cases, guidance, books). Used, with
    /// citations stripped, as the document's vector in the similarity graph.
    pub summary: Option<String>,
    /// Where the summary came from ("ATO", "editor", "machine-generated", ...).
    pub summary_source: Option<String>,
    /// `full` (default): the body is the full text, split into paragraphs.
    /// `summary`: the body is itself a summary of a document not held here.
    pub content: Option<String>,
    /// Versions of the same instrument (language versions, compilations at
    /// different dates) share a work id. Stored in the corpus metadata.
    pub work: Option<String>,
    /// Language of this version, e.g. `en`, `fr`.
    pub language: Option<String>,
    #[serde(default)]
    pub meta: Meta,
}

impl Manifest {
    /// `other` wins where it sets a field.
    pub fn merge(mut self, other: Manifest) -> Manifest {
        macro_rules! take {
            ($($f:ident),*) => { $( if other.$f.is_some() { self.$f = other.$f; } )* };
        }
        take!(
            id,
            title,
            kind,
            jurisdiction,
            profile,
            source_url,
            as_at,
            licence,
            format,
            summary,
            summary_source,
            content,
            work,
            language
        );
        self.aliases.extend(other.aliases);
        self.citations.extend(other.citations);
        if !other.files.is_empty() {
            self.files = other.files;
        }
        self.meta.extend(other.meta);
        self
    }

    fn load(path: &Path) -> Result<Option<Manifest>> {
        if !path.exists() {
            return Ok(None);
        }
        let src = std::fs::read_to_string(path)?;
        let m = if path.extension().is_some_and(|e| e == "json") {
            serde_json::from_str(&src).with_context(|| format!("parsing {}", path.display()))?
        } else {
            toml::from_str(&src).with_context(|| format!("parsing {}", path.display()))?
        };
        Ok(Some(m))
    }
}

pub struct Ingester {
    pub profiles: ProfileRegistry,
}

impl Ingester {
    pub fn new(profiles: ProfileRegistry) -> Self {
        Self { profiles }
    }

    /// Ingest a file or directory.
    ///
    /// A directory containing `corpus.toml` is one corpus. Any other directory is
    /// a batch: each supported file becomes its own corpus (e.g. a folder of judgments).
    pub fn ingest_path(&self, path: &Path, overrides: &Manifest) -> Result<Vec<Document>> {
        if path.is_dir() {
            let manifest_path = path.join("corpus.toml");
            if manifest_path.exists() {
                let m = Manifest::load(&manifest_path)?
                    .unwrap_or_default()
                    .merge(overrides.clone());
                return Ok(vec![self.ingest_multi(path, m)?]);
            }
            let mut files: Vec<PathBuf> = walkdir::WalkDir::new(path)
                .sort_by_file_name()
                .into_iter()
                .filter_map(|e| e.ok())
                .filter(|e| e.file_type().is_file())
                .map(|e| e.into_path())
                .filter(|p| is_content_file(p))
                .collect();
            files.sort();
            let mut docs = Vec::new();
            for f in files {
                let mut o = overrides.clone();
                // A batch cannot share one ID or title.
                o.id = None;
                o.title = None;
                docs.extend(self.ingest_file(&f, &o)?);
            }
            return Ok(docs);
        }
        self.ingest_file(path, overrides)
    }

    pub fn ingest_file(&self, path: &Path, overrides: &Manifest) -> Result<Vec<Document>> {
        let format = match &overrides.format {
            Some(f) => Format::parse(f).with_context(|| format!("unknown format `{f}`"))?,
            None => Format::from_path(path)
                .with_context(|| format!("cannot infer format of {}", path.display()))?,
        };
        if format == Format::Json {
            return read_json_documents(path);
        }
        let mut manifest = sidecar(path)?.unwrap_or_default();
        if matches!(format, Format::Markdown | Format::Text) {
            let src = blocks::read_text(path)?;
            if let (Some(fm), _) = blocks::front_matter(&src) {
                let m: Manifest = toml::from_str(fm)
                    .with_context(|| format!("front matter in {}", path.display()))?;
                manifest = manifest.merge(m);
            }
        }
        let manifest = manifest.merge(overrides.clone());
        let blocks = blocks::read_blocks(path, format)?;
        Ok(vec![self.build(path, manifest, blocks)?])
    }

    fn ingest_multi(&self, dir: &Path, manifest: Manifest) -> Result<Document> {
        let files: Vec<PathBuf> = if manifest.files.is_empty() {
            let mut v: Vec<PathBuf> = std::fs::read_dir(dir)?
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.is_file() && is_content_file(p))
                .collect();
            v.sort();
            v
        } else {
            manifest.files.iter().map(|f| dir.join(f)).collect()
        };
        if files.is_empty() {
            bail!("{} has a corpus.toml but no content files", dir.display());
        }
        let mut all = Vec::new();
        for f in &files {
            let format = match &manifest.format {
                Some(x) => Format::parse(x).with_context(|| format!("unknown format `{x}`"))?,
                None => Format::from_path(f)
                    .with_context(|| format!("cannot infer format of {}", f.display()))?,
            };
            all.extend(blocks::read_blocks(f, format)?);
        }
        self.build(dir, manifest, all)
    }

    fn build(&self, path: &Path, m: Manifest, blocks: Vec<Block>) -> Result<Document> {
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("corpus")
            .trim_end_matches(".meta")
            .to_string();
        let kind = match &m.kind {
            Some(k) => CorpusKind::parse(k).with_context(|| format!("unknown kind `{k}`"))?,
            None => CorpusKind::Secondary,
        };
        let profile_id = m.profile.clone().unwrap_or_else(|| "generic".into());
        let profile = self
            .profiles
            .get(&profile_id)
            .with_context(|| format!("loading profile `{profile_id}`"))?;
        let first_heading = blocks.iter().find_map(|b| match b {
            Block::Heading { text, .. } => Some(text.clone()),
            Block::Para(t) if kind == CorpusKind::Case => {
                Some(t.lines().next().unwrap_or("").to_string())
            }
            _ => None,
        });
        let mut citations = m.citations.clone();
        if kind == CorpusKind::Case && citations.is_empty() {
            citations.extend(find_case_citation(&profile, &blocks));
        }
        let title = m
            .title
            .clone()
            .or(first_heading)
            .map(|t| t.trim().trim_matches('*').trim().to_string())
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| stem.replace(['_', '-'], " "));
        let id = m.id.clone().unwrap_or_else(|| {
            if kind == CorpusKind::Case
                && let Some(c) = citations.first()
            {
                return slug(c);
            }
            slug(&stem)
        });
        check_corpus_id(&id)?;
        let corpus = Corpus {
            id,
            title,
            kind,
            jurisdiction: m.jurisdiction,
            profile: profile_id,
            aliases: m.aliases,
            citations,
            source_url: m.source_url,
            as_at: m.as_at,
            licence: m.licence,
            meta: {
                let mut meta = m.meta;
                for (k, v) in [("work", m.work), ("language", m.language)] {
                    if let Some(v) = v {
                        meta.insert(k.into(), serde_json::Value::String(v));
                    }
                }
                meta
            },
        };
        let mut nodes = structure::structure(&corpus, &profile, blocks);
        if !corpus.kind.is_structured() {
            if m.content.as_deref() == Some("summary") {
                nodes = paragraphs::add_summary(
                    nodes,
                    &corpus,
                    paragraphs::SummarySource::Body(m.summary_source.as_deref()),
                );
            } else {
                // Cases are already split into numbered paragraphs.
                if corpus.kind != CorpusKind::Case {
                    nodes = paragraphs::split_paragraphs(nodes, &corpus);
                }
                let src = match &m.summary {
                    Some(s) => paragraphs::SummarySource::Supplied(s, m.summary_source.as_deref()),
                    None => paragraphs::SummarySource::Fallback,
                };
                nodes = paragraphs::add_summary(nodes, &corpus, src);
            }
        }
        Ok(Document { corpus, nodes })
    }
}

fn is_content_file(p: &Path) -> bool {
    let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if name.starts_with('.') || name == "corpus.toml" || name.contains(".meta.") {
        return false;
    }
    Format::from_path(p).is_some()
}

fn sidecar(path: &Path) -> Result<Option<Manifest>> {
    let base = path.with_extension("");
    for ext in ["meta.toml", "meta.json"] {
        let p = PathBuf::from(format!("{}.{ext}", base.display()));
        if let Some(m) = Manifest::load(&p)? {
            return Ok(Some(m));
        }
    }
    Ok(None)
}

/// First case citation in the opening blocks, by the profile's case patterns.
fn find_case_citation(profile: &meowlaw_core::Profile, blocks: &[Block]) -> Vec<String> {
    let head: String = blocks
        .iter()
        .take(12)
        .filter_map(|b| match b {
            Block::Para(t) | Block::Heading { text: t, .. } => Some(t.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n");
    // Prefer a medium-neutral citation (a case's own identifier); a report-series
    // citation in the header is often another case. Then earliest wins.
    let mut best: Option<(usize, bool, String)> = None;
    for c in &profile.citations {
        if c.target != TargetSpec::Case {
            continue;
        }
        if let Some(cap) = c.re.captures(&head) {
            let m = cap.name("cite").or_else(|| cap.get(0)).unwrap();
            let neutral = c.name.contains("neutral");
            let better = match &best {
                None => true,
                Some((pos, n, _)) => (neutral && !n) || (neutral == *n && m.start() < *pos),
            };
            if better {
                best = Some((m.start(), neutral, citation_key(m.as_str())));
            }
        }
    }
    if let Some((_, _, c)) = best {
        return vec![c];
    }
    vec![]
}

/// Canonical interchange format: one Document or an array of Documents.
/// This is the integration point for any external scraper or converter.
pub fn read_json_documents(path: &Path) -> Result<Vec<Document>> {
    let src = std::fs::read_to_string(path)?;
    let v: serde_json::Value =
        serde_json::from_str(&src).with_context(|| format!("parsing {}", path.display()))?;
    let docs: Vec<Document> = if v.is_array() {
        serde_json::from_value(v)?
    } else {
        vec![serde_json::from_value(v)?]
    };
    for d in &docs {
        validate(d)?;
    }
    Ok(docs)
}

/// A corpus id is a slug: node ids are `<corpus>:<rest>` and definition ids
/// `def:<corpus>:<term>`, so the id must not contain the separators and must
/// not be `def` itself, or the corpus of an id could not be read back from it.
pub fn check_corpus_id(id: &str) -> Result<()> {
    if id.is_empty() || id == "def" || id.contains(':') || id.contains('/') {
        bail!("corpus id `{id}` must be a non-empty slug without `:` or `/`, and not `def`");
    }
    Ok(())
}

/// Structural checks shared by every ingest path.
pub fn validate(doc: &Document) -> Result<()> {
    let cid = &doc.corpus.id;
    check_corpus_id(cid)?;
    let prefix = format!("{cid}:");
    let mut seen = std::collections::HashSet::new();
    let mut has_root = false;
    for n in &doc.nodes {
        if n.corpus != *cid {
            bail!("node {} belongs to corpus {} not {}", n.id, n.corpus, cid);
        }
        // Access control and graph lookups read a node's corpus from its id.
        if n.id != *cid && !n.id.starts_with(&prefix) {
            bail!(
                "node id `{}` must be the corpus id or start with `{prefix}`",
                n.id
            );
        }
        if !seen.insert(n.id.as_str()) {
            bail!("duplicate node id {}", n.id);
        }
        if let Some(p) = &n.parent {
            if !seen.contains(p.as_str()) {
                bail!("node {} lists parent {} before it is defined", n.id, p);
            }
        } else {
            has_root = true;
        }
    }
    if !has_root {
        bail!("corpus {} has no root node", doc.corpus.id);
    }
    Ok(())
}

#[cfg(test)]
mod validate_tests {
    use super::validate;
    use serde_json::json;

    fn doc(corpus: &str, node_id: &str) -> meowlaw_core::Document {
        serde_json::from_value(json!({
            "corpus": { "id": corpus, "title": "T", "kind": "secondary" },
            "nodes": [
                { "id": corpus, "corpus": corpus, "kind": "root", "parent": null, "ordinal": 0, "text": "", "title": "T" },
                { "id": node_id, "corpus": corpus, "kind": "chunk", "parent": corpus, "ordinal": 1, "text": "x", "title": "c" }
            ]
        }))
        .unwrap()
    }

    #[test]
    fn node_ids_carry_the_corpus_prefix() {
        assert!(validate(&doc("handbook", "handbook:c1")).is_ok());
        let e = validate(&doc("handbook", "c1")).unwrap_err().to_string();
        assert!(
            e.contains("must be the corpus id or start with `handbook:`"),
            "{e}"
        );
        assert!(validate(&doc("handbook", "handbook-2:c1")).is_err());
    }

    #[test]
    fn corpus_ids_are_slugs() {
        assert!(validate(&doc("def", "def:c1")).is_err());
        assert!(validate(&doc("a:b", "a:b:c1")).is_err());
        assert!(validate(&doc("a/b", "a/b:c1")).is_err());
    }

    #[test]
    fn text_front_matter_is_metadata_not_body() {
        let dir = std::env::temp_dir().join(format!("meowlaw-ingest-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("plain.txt");
        std::fs::write(
            &path,
            "+++\ntitle = \"Plain Act 2020\"\nkind = \"legislation\"\n+++\n\n1 Short title\nThis Act is the Plain Act 2020.\n",
        )
        .unwrap();
        let ing = super::Ingester::new(meowlaw_core::ProfileRegistry::builtin().unwrap());
        let docs = ing.ingest_path(&path, &super::Manifest::default()).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
        let d = &docs[0];
        assert_eq!(d.corpus.title, "Plain Act 2020");
        for n in &d.nodes {
            assert!(
                !n.text.contains("+++"),
                "front matter leaked into {}: {:?}",
                n.id,
                n.text
            );
            assert!(
                !n.text.contains("title ="),
                "front matter leaked into {}: {:?}",
                n.id,
                n.text
            );
        }
    }
}
