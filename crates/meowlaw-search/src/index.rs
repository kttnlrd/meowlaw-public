//! Tantivy BM25 index over nodes.

use anyhow::{Context, Result};
use meowlaw_core::{Corpus, Node};
use meowlaw_store::Store;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use tantivy::collector::TopDocs;
use tantivy::query::{BooleanQuery, Occur, Query, QueryParser, TermQuery};
use tantivy::schema::{
    Field, IndexRecordOption, STORED, STRING, Schema, TextFieldIndexing, TextOptions, Value,
};
use tantivy::snippet::SnippetGenerator;
use tantivy::{Index, IndexReader, ReloadPolicy, TantivyDocument, Term, doc};

#[derive(Clone, Copy)]
struct Fields {
    id: Field,
    corpus: Field,
    corpus_kind: Field,
    jurisdiction: Field,
    node_kind: Field,
    heading: Field,
    context: Field,
    body: Field,
}

pub struct BmIndex {
    dir: PathBuf,
    index: Index,
    reader: IndexReader,
    f: Fields,
}

#[derive(Debug, Clone, Default, serde::Deserialize, serde::Serialize)]
pub struct Filters {
    pub corpus: Option<String>,
    /// Corpus kind (`legislation`, `case`, ...), or several separated by commas.
    pub kind: Option<String>,
    pub jurisdiction: Option<String>,
    /// Corpora to leave out (access control).
    pub exclude: Vec<String>,
    /// Search the versions in force on this date (`YYYY-MM-DD`) instead of
    /// the current ones. See `meowlaw_core::versions`.
    pub as_at: Option<String>,
}

#[derive(Debug, Clone)]
pub struct BmHit {
    pub id: String,
    pub score: f32,
    pub snippet: String,
    /// Matched every query term (the first pass), not just some.
    pub all_terms: bool,
}

/// Search language: which stemmer the text fields use ("stamping" finds
/// "stamp" in English). `none` lowercases and folds accents only.
pub const LANGUAGES: &[&str] = &[
    "none",
    "arabic",
    "danish",
    "dutch",
    "english",
    "finnish",
    "french",
    "german",
    "greek",
    "hungarian",
    "italian",
    "norwegian",
    "portuguese",
    "romanian",
    "russian",
    "spanish",
    "swedish",
    "tamil",
    "turkish",
];

fn analyser_name(language: &str) -> String {
    format!("meowlaw_{language}")
}

fn analyser(language: &str) -> Result<tantivy::tokenizer::TextAnalyzer> {
    use tantivy::tokenizer::{
        AsciiFoldingFilter, Language, LowerCaser, RemoveLongFilter, SimpleTokenizer, Stemmer,
        TextAnalyzer,
    };
    let lang = match language {
        "none" => None,
        "arabic" => Some(Language::Arabic),
        "danish" => Some(Language::Danish),
        "dutch" => Some(Language::Dutch),
        "english" => Some(Language::English),
        "finnish" => Some(Language::Finnish),
        "french" => Some(Language::French),
        "german" => Some(Language::German),
        "greek" => Some(Language::Greek),
        "hungarian" => Some(Language::Hungarian),
        "italian" => Some(Language::Italian),
        "norwegian" => Some(Language::Norwegian),
        "portuguese" => Some(Language::Portuguese),
        "romanian" => Some(Language::Romanian),
        "russian" => Some(Language::Russian),
        "spanish" => Some(Language::Spanish),
        "swedish" => Some(Language::Swedish),
        "tamil" => Some(Language::Tamil),
        "turkish" => Some(Language::Turkish),
        other => anyhow::bail!(
            "unknown search language `{other}`; use one of: {}",
            LANGUAGES.join(", ")
        ),
    };
    let b = TextAnalyzer::builder(SimpleTokenizer::default())
        .filter(RemoveLongFilter::limit(40))
        .filter(LowerCaser);
    Ok(match lang {
        // Stem before folding: the stemmers expect the language's own letters.
        Some(l) => b.filter(Stemmer::new(l)).filter(AsciiFoldingFilter).build(),
        None => b.filter(AsciiFoldingFilter).build(),
    })
}

fn stemmed(stored: bool, analyser: &str) -> TextOptions {
    let idx = TextFieldIndexing::default()
        .set_tokenizer(analyser)
        .set_index_option(IndexRecordOption::WithFreqsAndPositions);
    let o = TextOptions::default().set_indexing_options(idx);
    if stored { o.set_stored() } else { o }
}

fn schema(language: &str) -> (Schema, Fields) {
    let a = analyser_name(language);
    let mut b = Schema::builder();
    let f = Fields {
        id: b.add_text_field("id", STRING | STORED),
        corpus: b.add_text_field("corpus", STRING | STORED),
        corpus_kind: b.add_text_field("corpus_kind", STRING),
        jurisdiction: b.add_text_field("jurisdiction", STRING),
        node_kind: b.add_text_field("node_kind", STRING),
        heading: b.add_text_field("heading", stemmed(true, &a)),
        context: b.add_text_field("context", stemmed(false, &a)),
        body: b.add_text_field("body", stemmed(true, &a)),
    };
    (b.build(), f)
}

impl BmIndex {
    /// Open (or create) the index for `language`. A different language than the
    /// index was built with changes the schema, which starts a fresh index.
    pub fn open(dir: &Path, language: &str) -> Result<Self> {
        let tokenizer = analyser(language)?;
        std::fs::create_dir_all(dir)?;
        let (schema, f) = schema(language);
        let same = |i: &Index| {
            serde_json::to_string(&i.schema()).ok() == serde_json::to_string(&schema).ok()
        };
        let index = match Index::open_in_dir(dir) {
            Ok(i) if same(&i) => i,
            Ok(_) => {
                // Schema changed (new analyser or field): start a fresh index;
                // `rebuild` (or `serve`, when the index is empty) refills it.
                drop(std::fs::remove_dir_all(dir));
                std::fs::create_dir_all(dir)?;
                Index::create_in_dir(dir, schema).context("creating search index")?
            }
            Err(_) => Index::create_in_dir(dir, schema).context("creating search index")?,
        };
        index
            .tokenizers()
            .register(&analyser_name(language), tokenizer);
        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::OnCommitWithDelay)
            .try_into()?;
        Ok(Self {
            dir: dir.to_path_buf(),
            index,
            reader,
            f,
        })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Rebuild the index from the store. Returns the number of nodes indexed.
    pub fn rebuild(&self, store: &Store) -> Result<usize> {
        let mut w: tantivy::IndexWriter = self.index.writer(100_000_000)?;
        w.delete_all_documents()?;
        let mut count = 0;
        for d in store.documents()? {
            let headings: HashMap<&str, String> = d
                .nodes
                .iter()
                .map(|n| (n.id.as_str(), n.heading()))
                .collect();
            let parents: HashMap<&str, Option<&str>> = d
                .nodes
                .iter()
                .map(|n| (n.id.as_str(), n.parent.as_deref()))
                .collect();
            for n in &d.nodes {
                if n.text.trim().is_empty() && n.title.is_none() {
                    continue;
                }
                let context = ancestry_headings(n, &headings, &parents, &d.corpus);
                w.add_document(doc!(
                    self.f.id => n.id.clone(),
                    self.f.corpus => n.corpus.clone(),
                    self.f.corpus_kind => d.corpus.kind.as_str(),
                    self.f.jurisdiction => d.corpus.jurisdiction.clone().unwrap_or_default(),
                    self.f.node_kind => n.kind.as_str(),
                    self.f.heading => n.heading(),
                    self.f.context => context,
                    self.f.body => n.text.clone(),
                ))?;
                count += 1;
            }
        }
        w.commit()?;
        self.reader.reload()?;
        Ok(count)
    }

    pub fn num_docs(&self) -> u64 {
        self.reader.searcher().num_docs()
    }

    /// BM25 search. Terms are ANDed; if that finds nothing, ORed.
    pub fn search(&self, query: &str, filters: &Filters, limit: usize) -> Result<Vec<BmHit>> {
        let q = sanitise(query);
        if q.trim().is_empty() {
            return Ok(vec![]);
        }
        // All-terms matches first, then any-term matches to fill the list.
        let mut hits = self.run(&q, filters, limit, true)?;
        if hits.len() < limit {
            let seen: std::collections::HashSet<String> =
                hits.iter().map(|h| h.id.clone()).collect();
            hits.extend(
                self.run(&q, filters, limit, false)?
                    .into_iter()
                    .filter(|h| !seen.contains(&h.id))
                    .take(limit - hits.len()),
            );
        }
        Ok(hits)
    }

    fn run(
        &self,
        q: &str,
        filters: &Filters,
        limit: usize,
        conjunction: bool,
    ) -> Result<Vec<BmHit>> {
        let searcher = self.reader.searcher();
        let mut qp = QueryParser::for_index(
            &self.index,
            vec![self.f.heading, self.f.context, self.f.body],
        );
        qp.set_field_boost(self.f.heading, 3.0);
        qp.set_field_boost(self.f.context, 0.5);
        if conjunction {
            qp.set_conjunction_by_default();
        }
        let (parsed, _errors) = qp.parse_query_lenient(q);
        let mut clauses: Vec<(Occur, Box<dyn Query>)> = vec![(Occur::Must, parsed.box_clone())];
        let term = |field: Field, v: &str| -> Box<dyn Query> {
            Box::new(TermQuery::new(
                Term::from_field_text(field, v),
                IndexRecordOption::Basic,
            ))
        };
        if let Some(c) = &filters.corpus {
            clauses.push((Occur::Must, term(self.f.corpus, c)));
        }
        // `kind` may list several kinds ("guidance,treaty"): any of them.
        if let Some(k) = &filters.kind {
            let any: Vec<(Occur, Box<dyn Query>)> = k
                .split(',')
                .map(str::trim)
                .filter(|k| !k.is_empty())
                .map(|k| (Occur::Should, term(self.f.corpus_kind, k)))
                .collect();
            if !any.is_empty() {
                clauses.push((Occur::Must, Box::new(BooleanQuery::new(any))));
            }
        }
        for c in &filters.exclude {
            clauses.push((Occur::MustNot, term(self.f.corpus, c)));
        }
        if let Some(j) = &filters.jurisdiction {
            clauses.push((Occur::Must, term(self.f.jurisdiction, j)));
        }
        let query = BooleanQuery::new(clauses);
        let top = searcher.search(&query, &TopDocs::with_limit(limit).order_by_score())?;
        let mut snip = SnippetGenerator::create(&searcher, &*parsed, self.f.body)?;
        snip.set_max_num_chars(260);
        let mut out = Vec::new();
        for (score, addr) in top {
            let d: TantivyDocument = searcher.doc(addr)?;
            let id = d
                .get_first(self.f.id)
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            let s = snip.snippet_from_doc(&d);
            let snippet = if s.fragment().is_empty() {
                d.get_first(self.f.body)
                    .and_then(|v| v.as_str())
                    .map(|b| truncate(b, 260))
                    .unwrap_or_default()
            } else {
                mark(s.fragment(), s.highlighted())
            };
            out.push(BmHit {
                id,
                score,
                snippet,
                all_terms: conjunction,
            });
        }
        Ok(out)
    }
}

fn ancestry_headings(
    n: &Node,
    headings: &HashMap<&str, String>,
    parents: &HashMap<&str, Option<&str>>,
    corpus: &Corpus,
) -> String {
    let mut parts = vec![corpus.title.clone()];
    parts.extend(corpus.aliases.iter().cloned());
    parts.extend(corpus.citations.iter().cloned());
    let mut cur = n.parent.as_deref();
    let mut guard = 0;
    while let Some(p) = cur {
        if let Some(h) = headings.get(p) {
            parts.push(h.clone());
        }
        cur = parents.get(p).copied().flatten();
        guard += 1;
        if guard > 32 {
            break;
        }
    }
    parts.join(" | ")
}

/// Keep words, numbers, quotes and hyphens; drop query-syntax characters so
/// citations like `8-1(1)` or `[2024] HCA 1` cannot produce parse errors.
pub fn sanitise(q: &str) -> String {
    let balanced = q.matches('"').count().is_multiple_of(2);
    let mut out: Vec<String> = Vec::new();
    for tok in q.split_whitespace() {
        let keep_quotes = balanced && tok.contains('"');
        let parts: Vec<String> = tok
            .split(|c: char| !(c.is_alphanumeric() || (keep_quotes && c == '"')))
            .filter(|p| !p.is_empty())
            .map(str::to_string)
            .collect();
        match parts.len() {
            0 => {}
            1 => out.push(parts[0].clone()),
            // `8-1`, `5.3.2`, `2016/679`: keep the pieces together as a phrase.
            _ if !keep_quotes => out.push(format!("\"{}\"", parts.join(" "))),
            _ => out.push(parts.join(" ")),
        }
    }
    out.join(" ")
}

pub fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut end = max;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}…", &s[..end])
}

/// Wrap highlighted ranges in `«»` (plain-text friendly; the UI renders them).
fn mark(fragment: &str, ranges: &[std::ops::Range<usize>]) -> String {
    let mut out = String::new();
    let mut last = 0;
    for r in ranges {
        if r.start < last || r.end > fragment.len() {
            continue;
        }
        out.push_str(&fragment[last..r.start]);
        out.push('«');
        out.push_str(&fragment[r.start..r.end]);
        out.push('»');
        last = r.end;
    }
    out.push_str(&fragment[last..]);
    out
}
