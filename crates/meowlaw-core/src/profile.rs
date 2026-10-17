//! Jurisdiction profiles.
//!
//! A profile is a TOML file describing how a family of sources is written:
//! the structural headings it uses, how it cites provisions and cases, how it
//! defines terms, and which short names map to which corpus. Nothing in the
//! engine is specific to a country; everything country-specific lives here.
//!
//! Profiles can `extends` another profile. Lists are appended (the child's
//! entries first, so they win ties), maps are merged with the child winning.

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

use crate::model::NodeKind;

#[derive(Debug, thiserror::Error)]
pub enum ProfileError {
    #[error("profile `{0}` not found")]
    NotFound(String),
    #[error("profile `{id}`: {source}")]
    Toml {
        id: String,
        #[source]
        source: toml::de::Error,
    },
    #[error("profile `{id}`, pattern `{name}`: {source}")]
    Regex {
        id: String,
        name: String,
        #[source]
        source: regex::Error,
    },
    #[error("profile inheritance cycle at `{0}`")]
    Cycle(String),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

/// A structural level, detected from heading lines.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LevelSpec {
    /// Canonical label stored on nodes ("Part", "Section", "Article").
    pub label: String,
    /// Regex applied to a heading line. Named groups: `num`, `title`.
    pub pattern: String,
    /// Outer levels have lower rank. Used to build the tree from flat headings.
    pub rank: u8,
    #[serde(default = "default_level_kind")]
    pub kind: NodeKind,
    /// Short form for IDs (`s`, `pt`, `art`).
    pub abbrev: String,
}

fn default_level_kind() -> NodeKind {
    NodeKind::Container
}

/// What a citation pattern points at.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case", tag = "type")]
pub enum TargetSpec {
    /// A provision of a structured corpus. `label` restricts the level; when the
    /// regex has a `label` group, that wins. `num` or `nums` groups carry numbers;
    /// a `corpus` group names the corpus (resolved through aliases and titles).
    Provision {
        #[serde(default)]
        label: Option<String>,
    },
    /// A child of the citing provision ("subsection (2)", "paragraph (b)").
    Relative {
        #[serde(default)]
        label: Option<String>,
    },
    /// A case, resolved by normalised citation (whole match or `cite` group).
    Case,
    /// A whole corpus named in text ("the Privacy Act 1988").
    Corpus,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CitationSpec {
    pub name: String,
    pub pattern: String,
    pub target: TargetSpec,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefinitionPatternSpec {
    pub name: String,
    /// Regex applied to each paragraph. Named groups: `term` (required), `rest`.
    pub pattern: String,
    /// Only apply inside nodes whose title matches `section_titles`.
    #[serde(default)]
    pub definitions_section_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefinitionSpec {
    #[serde(default)]
    pub patterns: Vec<DefinitionPatternSpec>,
    /// Regex on a node title marking it as a definitions/interpretation provision.
    #[serde(default)]
    pub section_titles: Option<String>,
    /// Regex with a `label` group: "In this Part" scopes to the enclosing Part.
    #[serde(default)]
    pub scope_phrase: Option<String>,
    /// Words that can never be a term on their own.
    #[serde(default)]
    pub stop_terms: Vec<String>,
    #[serde(default = "default_max_term_words")]
    pub max_term_words: usize,
}

fn default_max_term_words() -> usize {
    8
}

impl Default for DefinitionSpec {
    fn default() -> Self {
        Self {
            patterns: Vec::new(),
            section_titles: None,
            scope_phrase: None,
            stop_terms: Vec::new(),
            max_term_words: default_max_term_words(),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CaseSpec {
    /// Regex for a numbered judgment paragraph at line start. Group `num`.
    #[serde(default)]
    pub paragraph: Option<String>,
}

/// Citations of table items. `item` finds "item 3 of the table" (group
/// `item`); when `link` matches the text between it and a following
/// provision citation ("in section 12"), the two join into one citation of
/// that provision pinned to the item. Otherwise the item is in the citing
/// provision's own table.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TableSpec {
    #[serde(default)]
    pub item: Option<String>,
    #[serde(default)]
    pub link: Option<String>,
}

/// Raw profile as written in TOML.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProfileSpec {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub extends: Option<String>,
    #[serde(default)]
    pub language: Option<String>,
    /// Shared fragment substituted into patterns as `{NUM}`.
    #[serde(default)]
    pub number: Option<String>,
    #[serde(default)]
    pub levels: Vec<LevelSpec>,
    #[serde(default)]
    pub citations: Vec<CitationSpec>,
    #[serde(default)]
    pub definitions: DefinitionSpec,
    #[serde(default)]
    pub cases: CaseSpec,
    #[serde(default)]
    pub tables: TableSpec,
    /// Label synonyms to canonical labels ("s" → "Section", "art" → "Article").
    #[serde(default)]
    pub label_aliases: BTreeMap<String, String>,
    /// Short names → corpus IDs, applied to every corpus using this profile.
    #[serde(default)]
    pub corpus_aliases: BTreeMap<String, String>,
    /// Jurisdiction tags written after Act names, e.g. "(Cth)", "(NSW)", "(UK)".
    /// Ignored when matching names and removed when stripping citations.
    #[serde(default)]
    pub jurisdiction_tags: Vec<String>,
    /// Source links generated from a corpus's citations (e.g. AustLII for `[2026] HCA 18`).
    #[serde(default)]
    pub links: Vec<LinkSpec>,
}

/// A link template. `citation` is matched against each corpus citation; named
/// groups are substituted into `url` as `{name}` (`{court_lower}` gives the
/// lowercase form of `court`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LinkSpec {
    pub label: String,
    pub citation: String,
    pub url: String,
}

/// A resolved link to an external copy of a source.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Link {
    pub label: String,
    pub url: String,
}

impl ProfileSpec {
    pub fn from_toml(src: &str) -> Result<Self, toml::de::Error> {
        toml::from_str(src)
    }

    fn merge_parent(mut self, parent: ProfileSpec) -> ProfileSpec {
        if self.name.is_none() {
            self.name = parent.name;
        }
        if self.language.is_none() {
            self.language = parent.language;
        }
        if self.number.is_none() {
            self.number = parent.number;
        }
        if self.levels.is_empty() {
            self.levels = parent.levels;
        }
        self.citations.extend(parent.citations);
        let d = &mut self.definitions;
        d.patterns.extend(parent.definitions.patterns);
        if d.section_titles.is_none() {
            d.section_titles = parent.definitions.section_titles;
        }
        if d.scope_phrase.is_none() {
            d.scope_phrase = parent.definitions.scope_phrase;
        }
        d.stop_terms.extend(parent.definitions.stop_terms);
        if self.cases.paragraph.is_none() {
            self.cases.paragraph = parent.cases.paragraph;
        }
        if self.tables.item.is_none() {
            self.tables.item = parent.tables.item;
        }
        if self.tables.link.is_none() {
            self.tables.link = parent.tables.link;
        }
        for (k, v) in parent.label_aliases {
            self.label_aliases.entry(k).or_insert(v);
        }
        for (k, v) in parent.corpus_aliases {
            self.corpus_aliases.entry(k).or_insert(v);
        }
        self.links.extend(parent.links);
        self.jurisdiction_tags.extend(parent.jurisdiction_tags);
        self
    }
}

#[derive(Debug, Clone)]
pub struct Level {
    pub spec: LevelSpec,
    pub re: Regex,
}

#[derive(Debug, Clone)]
pub struct Citation {
    pub name: String,
    pub target: TargetSpec,
    pub re: Regex,
}

#[derive(Debug, Clone)]
pub struct DefinitionPattern {
    pub name: String,
    pub re: Regex,
    pub definitions_section_only: bool,
}

/// A compiled, ready-to-use profile.
#[derive(Debug, Clone)]
pub struct Profile {
    pub spec: ProfileSpec,
    pub levels: Vec<Level>,
    pub citations: Vec<Citation>,
    pub definitions: Vec<DefinitionPattern>,
    pub definitions_title: Option<Regex>,
    pub scope_phrase: Option<Regex>,
    pub case_paragraph: Option<Regex>,
    pub table_item: Option<Regex>,
    /// Anchored at the start: `^(?:link)`.
    pub table_link: Option<Regex>,
    pub links: Vec<(LinkSpec, Regex)>,
}

impl Profile {
    pub fn id(&self) -> &str {
        &self.spec.id
    }

    pub fn compile(spec: ProfileSpec) -> Result<Self, ProfileError> {
        let num = spec
            .number
            .clone()
            .unwrap_or_else(|| DEFAULT_NUMBER.to_string());
        let id = spec.id.clone();
        let re = |name: &str, p: &str| -> Result<Regex, ProfileError> {
            let p = p.replace("{NUM}", &num);
            Regex::new(&p).map_err(|source| ProfileError::Regex {
                id: id.clone(),
                name: name.to_string(),
                source,
            })
        };
        let mut levels = Vec::new();
        for l in &spec.levels {
            levels.push(Level {
                spec: l.clone(),
                re: re(&l.label, &l.pattern)?,
            });
        }
        let mut citations = Vec::new();
        for c in &spec.citations {
            citations.push(Citation {
                name: c.name.clone(),
                target: c.target.clone(),
                re: re(&c.name, &c.pattern)?,
            });
        }
        let mut definitions = Vec::new();
        for d in &spec.definitions.patterns {
            definitions.push(DefinitionPattern {
                name: d.name.clone(),
                re: re(&d.name, &d.pattern)?,
                definitions_section_only: d.definitions_section_only,
            });
        }
        let definitions_title = spec
            .definitions
            .section_titles
            .as_deref()
            .map(|p| re("definitions.section_titles", p))
            .transpose()?;
        let scope_phrase = spec
            .definitions
            .scope_phrase
            .as_deref()
            .map(|p| re("definitions.scope_phrase", p))
            .transpose()?;
        let case_paragraph = spec
            .cases
            .paragraph
            .as_deref()
            .map(|p| re("cases.paragraph", p))
            .transpose()?;
        let table_item = spec
            .tables
            .item
            .as_deref()
            .map(|p| re("tables.item", p))
            .transpose()?;
        let table_link = spec
            .tables
            .link
            .as_deref()
            .map(|p| re("tables.link", &format!("^(?:{p})")))
            .transpose()?;
        let mut links = Vec::new();
        for l in &spec.links {
            links.push((l.clone(), re(&format!("links.{}", l.label), &l.citation)?));
        }
        Ok(Self {
            links,
            spec,
            levels,
            citations,
            definitions,
            definitions_title,
            scope_phrase,
            case_paragraph,
            table_item,
            table_link,
        })
    }

    /// External links for a corpus: templates applied to each citation.
    pub fn links_for(&self, citations: &[String]) -> Vec<Link> {
        let mut out: Vec<Link> = Vec::new();
        for c in citations {
            for (spec, re) in &self.links {
                let Some(cap) = re.captures(c) else { continue };
                let mut url = spec.url.clone();
                for name in re.capture_names().flatten() {
                    let v = cap.name(name).map(|m| m.as_str()).unwrap_or("");
                    url = url
                        .replace(&format!("{{{name}_lower}}"), &v.to_lowercase())
                        .replace(&format!("{{{name}}}"), v);
                }
                if !out.iter().any(|l| l.url == url) {
                    out.push(Link {
                        label: spec.label.clone(),
                        url,
                    });
                }
            }
        }
        out
    }

    /// Canonical label for a label as written ("ss" → "Section").
    pub fn canonical_label(&self, raw: &str) -> String {
        let key = raw.trim().trim_end_matches('.').to_lowercase();
        if let Some(v) = self.spec.label_aliases.get(&key) {
            return v.clone();
        }
        for l in &self.levels {
            if l.spec.label.to_lowercase() == key || l.spec.abbrev.to_lowercase() == key {
                return l.spec.label.clone();
            }
        }
        let singular = key.strip_suffix('s').unwrap_or(&key);
        for l in &self.levels {
            if l.spec.label.to_lowercase() == singular {
                return l.spec.label.clone();
            }
        }
        raw.trim().to_string()
    }

    pub fn abbrev_for(&self, label: &str) -> String {
        self.levels
            .iter()
            .find(|l| l.spec.label.eq_ignore_ascii_case(label))
            .map(|l| l.spec.abbrev.clone())
            .unwrap_or_else(|| crate::ids::slug(label))
    }

    /// Match a heading line against the structural levels.
    pub fn match_heading(&self, line: &str) -> Option<HeadingMatch> {
        let line = line.trim();
        for l in &self.levels {
            if let Some(c) = l.re.captures(line) {
                let num = c.name("num").map(|m| m.as_str().trim().to_string());
                let title = c
                    .name("title")
                    .map(|m| clean_title(m.as_str()))
                    .filter(|t| !t.is_empty());
                return Some(HeadingMatch {
                    label: l.spec.label.clone(),
                    rank: l.spec.rank,
                    kind: l.spec.kind,
                    number: num,
                    title,
                });
            }
        }
        None
    }
}

fn clean_title(s: &str) -> String {
    s.trim()
        .trim_start_matches(['—', '–', '-', ':', '.', ' '])
        .trim()
        .to_string()
}

#[derive(Debug, Clone, PartialEq)]
pub struct HeadingMatch {
    pub label: String,
    pub rank: u8,
    pub kind: NodeKind,
    pub number: Option<String>,
    pub title: Option<String>,
}

/// Default provision-number fragment: `8-1`, `12A`, `5.3.2`, `162`, `IV`.
pub const DEFAULT_NUMBER: &str = r"(?:[0-9]+[A-Za-z]{0,3}(?:[-.:][0-9]+[A-Za-z]{0,3})*|[IVXLC]+\b)";

/// Profiles shipped with the binary.
pub const BUILTIN: &[(&str, &str)] = &[
    ("generic", include_str!("../../../profiles/generic.toml")),
    ("au", include_str!("../../../profiles/au.toml")),
    ("gb", include_str!("../../../profiles/gb.toml")),
    ("us", include_str!("../../../profiles/us.toml")),
    ("eu", include_str!("../../../profiles/eu.toml")),
    ("de", include_str!("../../../profiles/de.toml")),
    // Starter profiles: sourced patterns, awaiting review by local lawyers.
    ("ca", include_str!("../../../profiles/ca.toml")),
    ("nz", include_str!("../../../profiles/nz.toml")),
    ("sg", include_str!("../../../profiles/sg.toml")),
    ("hk", include_str!("../../../profiles/hk.toml")),
    ("in", include_str!("../../../profiles/in.toml")),
    ("ie", include_str!("../../../profiles/ie.toml")),
    ("za", include_str!("../../../profiles/za.toml")),
    ("fr", include_str!("../../../profiles/fr.toml")),
];

/// Loads profiles from built-ins and an optional directory of `*.toml` files.
/// Files in the directory override built-ins with the same `id`.
#[derive(Debug, Default, Clone)]
pub struct ProfileRegistry {
    specs: BTreeMap<String, ProfileSpec>,
}

impl ProfileRegistry {
    pub fn builtin() -> Result<Self, ProfileError> {
        let mut r = Self::default();
        for (id, src) in BUILTIN {
            let spec = ProfileSpec::from_toml(src).map_err(|source| ProfileError::Toml {
                id: id.to_string(),
                source,
            })?;
            r.specs.insert(spec.id.clone(), spec);
        }
        Ok(r)
    }

    pub fn with_dir(mut self, dir: &Path) -> Result<Self, ProfileError> {
        if !dir.is_dir() {
            return Ok(self);
        }
        for entry in std::fs::read_dir(dir)? {
            let p = entry?.path();
            if p.extension().and_then(|e| e.to_str()) != Some("toml") {
                continue;
            }
            let src = std::fs::read_to_string(&p)?;
            let spec = ProfileSpec::from_toml(&src).map_err(|source| ProfileError::Toml {
                id: p.display().to_string(),
                source,
            })?;
            self.specs.insert(spec.id.clone(), spec);
        }
        Ok(self)
    }

    pub fn ids(&self) -> Vec<String> {
        self.specs.keys().cloned().collect()
    }

    pub fn spec(&self, id: &str) -> Option<&ProfileSpec> {
        self.specs.get(id)
    }

    /// Resolve inheritance and compile.
    pub fn get(&self, id: &str) -> Result<Profile, ProfileError> {
        Profile::compile(self.resolve(id, 0)?)
    }

    fn resolve(&self, id: &str, depth: usize) -> Result<ProfileSpec, ProfileError> {
        if depth > 16 {
            return Err(ProfileError::Cycle(id.to_string()));
        }
        let spec = self
            .specs
            .get(id)
            .cloned()
            .ok_or_else(|| ProfileError::NotFound(id.to_string()))?;
        match spec.extends.clone() {
            Some(parent) => Ok(spec.merge_parent(self.resolve(&parent, depth + 1)?)),
            None => Ok(spec),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtins_compile() {
        let reg = ProfileRegistry::builtin().unwrap();
        for id in reg.ids() {
            reg.get(&id).unwrap_or_else(|e| panic!("{id}: {e}"));
        }
    }

    #[test]
    fn generic_headings() {
        let p = ProfileRegistry::builtin().unwrap().get("generic").unwrap();
        let h = p.match_heading("Part 2 — Liability").unwrap();
        assert_eq!(h.label, "Part");
        assert_eq!(h.number.as_deref(), Some("2"));
        assert_eq!(h.title.as_deref(), Some("Liability"));
        let h = p.match_heading("Article 5 Principles").unwrap();
        assert_eq!(h.label, "Article");
        let h = p
            .match_heading("§ 162. Trade or business expenses")
            .unwrap();
        assert_eq!(h.label, "Section");
        assert_eq!(h.number.as_deref(), Some("162"));
        assert_eq!(p.canonical_label("ss"), "Section");
        assert_eq!(p.canonical_label("Arts."), "Article");
    }

    #[test]
    fn extends_merges() {
        let reg = ProfileRegistry::builtin().unwrap();
        let au = reg.get("au").unwrap();
        let generic = reg.get("generic").unwrap();
        assert!(au.citations.len() > generic.citations.len());
        assert!(!au.levels.is_empty());
    }
}
