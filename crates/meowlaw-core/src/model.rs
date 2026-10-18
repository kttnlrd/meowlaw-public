//! The jurisdiction-agnostic data model.
//!
//! Every source (an Act, a code, a set of court rules, a judgment, a textbook)
//! is a [`Corpus`]. Its content is a tree of [`Node`]s. Cross-references and
//! definitions are extracted into [`Edge`]s and [`Definition`]s, which together
//! form the graph used for enriched search and the MCP tools.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub type Meta = BTreeMap<String, serde_json::Value>;

/// What a corpus is. Drives defaults (structure detection, ID scheme, UI icon)
/// but never hard-codes a country.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CorpusKind {
    /// Primary legislation: statutes, Acts, codes, constitutions.
    Legislation,
    /// Delegated or secondary instruments: regulations, rules, orders.
    Instrument,
    /// Procedural or professional rule sets (court rules, conduct rules, standards).
    RuleSet,
    /// A judgment or tribunal decision.
    Case,
    /// Treaties and international instruments.
    Treaty,
    /// Regulator guidance, rulings, practice notes.
    Guidance,
    /// Textbooks, commentary, internal handbooks, precedents and anything else.
    Secondary,
}

impl CorpusKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Legislation => "legislation",
            Self::Instrument => "instrument",
            Self::RuleSet => "rule_set",
            Self::Case => "case",
            Self::Treaty => "treaty",
            Self::Guidance => "guidance",
            Self::Secondary => "secondary",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s.to_ascii_lowercase().replace('-', "_").as_str() {
            "legislation" | "act" | "statute" | "code" => Self::Legislation,
            "instrument" | "regulation" | "regulations" => Self::Instrument,
            "rule_set" | "rules" | "ruleset" => Self::RuleSet,
            "case" | "cases" | "judgment" | "decision" => Self::Case,
            "treaty" | "treaties" => Self::Treaty,
            "guidance" | "ruling" | "rulings" => Self::Guidance,
            "secondary" | "book" | "textbook" | "handbook" | "general" | "other" => Self::Secondary,
            _ => return None,
        })
    }

    /// Kinds whose provisions are addressable by label + number (s 12, Art 5).
    pub fn is_structured(&self) -> bool {
        matches!(
            self,
            Self::Legislation | Self::Instrument | Self::RuleSet | Self::Treaty
        )
    }
}

/// One ingested source.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Corpus {
    /// Stable slug, unique in the store (e.g. `privacy-act-1988`, `usc-26`).
    pub id: String,
    pub title: String,
    pub kind: CorpusKind,
    /// Free-form jurisdiction code. ISO 3166 plus a sub-code is recommended
    /// (`AU-CTH`, `GB`, `US-CA`, `EU`, `INT`) but nothing depends on it.
    #[serde(default)]
    pub jurisdiction: Option<String>,
    /// Profile used to parse and extract (see [`crate::profile`]).
    #[serde(default = "default_profile")]
    pub profile: String,
    /// Short names that should resolve to this corpus in references
    /// ("ITAA 1997", "the Code", "GDPR").
    #[serde(default)]
    pub aliases: Vec<String>,
    /// Citations by which the whole corpus is known (cases: `[2024] HCA 1`).
    #[serde(default)]
    pub citations: Vec<String>,
    #[serde(default)]
    pub source_url: Option<String>,
    /// Point-in-time the text represents (compilation date, judgment date).
    #[serde(default)]
    pub as_at: Option<String>,
    /// Licence of the source text. Shown in the UI and every MCP fetch.
    #[serde(default)]
    pub licence: Option<String>,
    #[serde(default)]
    pub meta: Meta,
}

pub fn default_profile() -> String {
    "generic".to_string()
}

/// What a node in the tree represents.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeKind {
    /// The corpus root.
    Root,
    /// A grouping level with no operative text of its own (Part, Chapter, Title).
    Container,
    /// An addressable unit of operative text (section, article, rule, clause).
    Provision,
    /// A numbered paragraph of a judgment.
    Paragraph,
    /// A heading-delimited chunk of secondary material.
    Chunk,
    /// A schedule, annex or appendix.
    Schedule,
    /// A summary of the whole document (`<corpus>:summary`).
    Summary,
}

impl NodeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Root => "root",
            Self::Container => "container",
            Self::Provision => "provision",
            Self::Paragraph => "paragraph",
            Self::Chunk => "chunk",
            Self::Schedule => "schedule",
            Self::Summary => "summary",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "root" => Self::Root,
            "container" => Self::Container,
            "provision" => Self::Provision,
            "paragraph" => Self::Paragraph,
            "chunk" => Self::Chunk,
            "schedule" => Self::Schedule,
            "summary" => Self::Summary,
            _ => return None,
        })
    }
}

/// A node of a corpus tree.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Node {
    pub id: String,
    pub corpus: String,
    pub kind: NodeKind,
    /// Structural label as written in the source ("Section", "Article", "Rule", "§").
    #[serde(default)]
    pub label: Option<String>,
    /// Number as written ("8-1", "5", "12A", "(2)(b)").
    #[serde(default)]
    pub number: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    /// Body text (Markdown-flavoured plain text). Children's text is not repeated.
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub parent: Option<String>,
    /// Position among siblings.
    #[serde(default)]
    pub ordinal: u32,
    #[serde(default)]
    pub meta: Meta,
}

impl Node {
    /// Human heading, e.g. "Section 8-1 General deductions".
    pub fn heading(&self) -> String {
        if self.kind == NodeKind::Paragraph
            && self.label.is_none()
            && let Some(n) = &self.number
        {
            return format!("[{n}]");
        }
        let mut parts = Vec::new();
        if let Some(l) = &self.label {
            parts.push(l.clone());
        }
        if let Some(n) = &self.number {
            parts.push(n.clone());
        }
        if let Some(t) = &self.title {
            parts.push(t.clone());
        }
        if parts.is_empty() {
            self.id.clone()
        } else {
            parts.join(" ")
        }
    }
}

/// Relationship types in the graph.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum EdgeKind {
    /// Parent contains child (tree edge).
    Contains,
    /// Text of the source cites the target.
    Cites,
    /// Source node defines a term that is a Definition node.
    Defines,
    /// Source node uses a term defined at the target node.
    UsesTerm,
    /// Source amends, repeals or inserts the target.
    Amends,
    /// Manually or externally asserted relationship (e.g. "see also", "explained in").
    Related,
    /// Same provision under a different ID (renumbering, consolidations).
    SameAs,
    /// Semantic similarity from embeddings. Weighted by cosine score.
    Similar,
}

impl EdgeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Contains => "contains",
            Self::Cites => "cites",
            Self::Defines => "defines",
            Self::UsesTerm => "uses_term",
            Self::Amends => "amends",
            Self::Related => "related",
            Self::SameAs => "same_as",
            Self::Similar => "similar",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "contains" => Self::Contains,
            "cites" => Self::Cites,
            "defines" => Self::Defines,
            "uses_term" => Self::UsesTerm,
            "amends" => Self::Amends,
            "related" => Self::Related,
            "same_as" => Self::SameAs,
            "similar" => Self::Similar,
            _ => return None,
        })
    }
}

/// How an edge or reference was produced. Kept so quality can be audited.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Method {
    Structure,
    Pattern,
    Alias,
    Manual,
    Model,
}

impl Method {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Structure => "structure",
            Self::Pattern => "pattern",
            Self::Alias => "alias",
            Self::Manual => "manual",
            Self::Model => "model",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Edge {
    pub source: String,
    pub target: String,
    pub kind: EdgeKind,
    pub method: Method,
    /// The text that produced the edge (citation as written, or the score).
    #[serde(default)]
    pub evidence: Option<String>,
    /// 1.0 for explicit edges; cosine similarity for `similar` edges.
    #[serde(default = "one")]
    pub weight: f32,
}

fn one() -> f32 {
    1.0
}

/// A reference found in text, resolved or not. Unresolved references are kept:
/// they are the main signal of a missing corpus or a broken pattern.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Reference {
    pub source: String,
    /// Citation as written.
    pub raw: String,
    /// Byte offsets in the source node's text.
    pub start: usize,
    pub end: usize,
    /// Name of the profile pattern that matched.
    pub pattern: String,
    /// Resolved target node, if any.
    #[serde(default)]
    pub target: Option<String>,
    /// Pinpoint inside the target that is not a node of its own, such as a
    /// table item ("item 3 of the table in section 12"): the UI links to the row.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pin: Option<String>,
}

/// A defined term.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Definition {
    pub id: String,
    pub term: String,
    /// Node whose text contains the definition.
    pub source: String,
    /// Node whose subtree the definition applies to (corpus root for "In this Act").
    pub scope: String,
    pub corpus: String,
    /// The defining sentence or paragraph.
    pub text: String,
    /// Pattern name (`means`, `includes`, `has_meaning`, ...).
    pub pattern: String,
    /// For "has the meaning given by s 995-1": the referenced provision, if resolved.
    #[serde(default)]
    pub points_to: Option<String>,
}
