//! Search: basic BM25 and enriched (citation resolution + definitions + graph
//! + optional vectors, fused with reciprocal rank fusion).

pub mod diagnose;
pub mod index;
pub mod summarise;
pub mod sync;
pub mod vector;

use anyhow::Result;
use meowlaw_core::{CorpusKind, Definition, EdgeKind, Profile, ProfileRegistry};
use meowlaw_extract::{Catalog, Cited, build_catalog, load_profiles, parse_citations};
use meowlaw_store::Store;
use serde::Serialize;
use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::RwLock;

pub use index::{BmIndex, Filters};
pub use vector::{Embedder, VectorIndex};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize, Serialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    /// BM25 only.
    Basic,
    /// Citation resolution, definitions, graph expansion, authority and vectors.
    #[default]
    Enriched,
}

impl Mode {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "basic" => Some(Self::Basic),
            "enriched" | "quality" => Some(Self::Enriched),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Hit {
    pub id: String,
    pub corpus: String,
    pub corpus_title: String,
    pub corpus_kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub jurisdiction: Option<String>,
    pub heading: String,
    /// Ancestor headings, outermost first.
    pub path: Vec<String>,
    pub snippet: String,
    pub score: f32,
    /// Why this result is here: `citation`, `bm25`, `definition`, `graph`, `vector`.
    pub matched_by: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ResolvedCitation {
    pub raw: String,
    pub targets: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchResponse {
    pub query: String,
    pub mode: Mode,
    /// Citations recognised in the query and what they resolved to.
    pub citations: Vec<ResolvedCitation>,
    /// Definitions of terms that appear in the query.
    pub definitions: Vec<Definition>,
    pub results: Vec<Hit>,
}

/// Reciprocal-rank-fusion weights for each evidence list.
#[derive(Debug, Clone, Copy)]
pub struct Weights {
    pub citation: f32,
    pub bm25: f32,
    pub definition: f32,
    pub graph: f32,
    pub vector: f32,
    /// Multiplier on ln(1 + inbound citations).
    pub authority: f32,
    pub k: f32,
}

impl Default for Weights {
    fn default() -> Self {
        Self {
            citation: 4.0,
            bm25: 1.2,
            definition: 0.6,
            graph: 0.3,
            vector: 0.9,
            authority: 0.05,
            k: 30.0,
        }
    }
}

#[derive(Default)]
struct Fusion {
    score: HashMap<String, f32>,
    why: HashMap<String, BTreeSet<String>>,
}

impl Fusion {
    fn add(&mut self, id: &str, s: f32, reason: &str) {
        *self.score.entry(id.to_string()).or_default() += s;
        self.why
            .entry(id.to_string())
            .or_default()
            .insert(reason.to_string());
    }
}

struct Cache {
    /// Corpora with a summary node.
    summarised: std::collections::HashSet<String>,
    catalog: Catalog,
    profiles: HashMap<String, Profile>,
    authority: HashMap<String, u32>,
    terms: Option<(aho_corasick::AhoCorasick, Vec<String>)>,
}

pub struct Engine {
    pub store: Store,
    pub bm: BmIndex,
    vectors: RwLock<Option<VectorIndex>>,
    pub embedder: Option<Box<dyn Embedder>>,
    pub weights: Weights,
    /// Mode used by callers that do not name one (`[search] mode`).
    pub default_mode: Mode,
    registry: ProfileRegistry,
    cache: RwLock<Option<Cache>>,
    /// Store generation the cache and vectors were built from.
    cached_gen: std::sync::atomic::AtomicU64,
    /// Serialises cache rebuilds so concurrent first requests build it once.
    build_lock: std::sync::Mutex<()>,
}

impl Engine {
    /// Open with English stemming.
    pub fn open(store: Store, index_dir: &Path, registry: ProfileRegistry) -> Result<Self> {
        Self::open_with_language(store, index_dir, registry, "english")
    }

    /// Open with the search language's stemmer (see `index::LANGUAGES`).
    pub fn open_with_language(
        store: Store,
        index_dir: &Path,
        registry: ProfileRegistry,
        language: &str,
    ) -> Result<Self> {
        let bm = BmIndex::open(index_dir, language)?;
        Ok(Self {
            store,
            bm,
            vectors: RwLock::new(None),
            embedder: None,
            weights: Weights::default(),
            default_mode: Mode::default(),
            registry,
            cache: RwLock::new(None),
            cached_gen: std::sync::atomic::AtomicU64::new(u64::MAX),
            build_lock: std::sync::Mutex::new(()),
        })
    }

    /// Mode for requests that do not name one.
    pub fn with_default_mode(mut self, mode: Mode) -> Self {
        self.default_mode = mode;
        self
    }

    /// Attach an embedder; its stored vectors load with the cache.
    pub fn with_embedder(mut self, e: Box<dyn Embedder>) -> Result<Self> {
        self.embedder = Some(e);
        Ok(self)
    }

    /// Number of chunk vectors loaded for the attached embedder.
    pub fn vector_count(&self) -> Result<usize> {
        self.with_cache(|_| ())?;
        Ok(self.vectors.read().unwrap().as_ref().map_or(0, |v| v.len()))
    }

    pub fn registry(&self) -> &ProfileRegistry {
        &self.registry
    }

    /// Drop cached lookups (call after ingest/extract).
    pub fn invalidate(&self) {
        *self.cache.write().unwrap() = None;
        self.cached_gen
            .store(u64::MAX, std::sync::atomic::Ordering::Release);
    }

    fn with_cache<T>(&self, f: impl FnOnce(&Cache) -> T) -> Result<T> {
        use std::sync::atomic::Ordering;
        let current = self.store.generation()?;
        // Read under the lock and only use a cache that is present and fresh;
        // an `invalidate()` racing with the rebuild just causes another pass
        // instead of an unwrap on `None`.
        loop {
            {
                let g = self.cache.read().unwrap();
                if let Some(c) = g.as_ref()
                    && self.cached_gen.load(Ordering::Acquire) == current
                {
                    return Ok(f(c));
                }
            }
            let _guard = self.build_lock.lock().unwrap();
            let stale = self.cache.read().unwrap().is_none()
                || self.cached_gen.load(Ordering::Acquire) != current;
            if stale {
                self.rebuild_cache()?;
                self.cached_gen.store(current, Ordering::Release);
            }
        }
    }

    fn rebuild_cache(&self) -> Result<()> {
        {
            let docs = self.store.documents()?;
            let profiles = load_profiles(&self.registry, docs.iter().map(|d| &d.corpus))?;
            let catalog = build_catalog(
                docs.iter().map(|d| (&d.corpus, d.nodes.as_slice())),
                &profiles,
            );
            let authority = self.store.citation_counts()?;
            let defs = self.store.all_definitions()?;
            let mut terms: Vec<String> = defs.iter().map(|d| d.term.to_lowercase()).collect();
            terms.sort();
            terms.dedup();
            let ac = if terms.is_empty() {
                None
            } else {
                aho_corasick::AhoCorasickBuilder::new()
                    .ascii_case_insensitive(true)
                    .match_kind(aho_corasick::MatchKind::LeftmostLongest)
                    .build(&terms)
                    .ok()
                    .map(|a| (a, terms))
            };
            let summarised = self.store.summarised_corpora()?;
            *self.cache.write().unwrap() = Some(Cache {
                summarised,
                catalog,
                profiles,
                authority,
                terms: ac,
            });
        }
        if let Some(e) = &self.embedder {
            let vi = VectorIndex::load(&self.store, e.model())?;
            *self.vectors.write().unwrap() = (!vi.is_empty()).then_some(vi);
        }
        Ok(())
    }

    /// External links for a corpus: explicit `meta.links` first, then the
    /// profile's citation templates (e.g. AustLII).
    pub fn links_for(&self, corpus: &meowlaw_core::Corpus) -> Result<Vec<meowlaw_core::Link>> {
        let mut out: Vec<meowlaw_core::Link> = corpus
            .meta
            .get("links")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();
        out.retain(|l| l.url.starts_with("https://") || l.url.starts_with("http://"));
        let generated = self.with_cache(|c| {
            c.profiles
                .get(&corpus.profile)
                .map(|p| p.links_for(&corpus.citations))
                .unwrap_or_default()
        })?;
        for l in generated {
            if !out.iter().any(|x| x.url == l.url) {
                out.push(l);
            }
        }
        Ok(out)
    }

    /// Text with citations removed, using the corpus profile and `generic`.
    pub fn strip_citations(&self, corpus: &meowlaw_core::Corpus, text: &str) -> Result<String> {
        let generic = self.registry.get("generic").ok();
        self.with_cache(|c| {
            let mut ps: Vec<&Profile> = c.profiles.get(&corpus.profile).into_iter().collect();
            if corpus.profile != "generic"
                && let Some(g) = generic.as_ref()
            {
                ps.push(g);
            }
            meowlaw_extract::strip_citations(text, &ps)
        })
    }

    /// Resolve citations in free text (used by search and the `resolve` MCP tool).
    pub fn resolve_citations(
        &self,
        text: &str,
        corpus_hint: Option<&str>,
    ) -> Result<Vec<ResolvedCitation>> {
        self.with_cache(|c| resolve_in(c, &self.registry, text, corpus_hint))
    }

    pub fn search(
        &self,
        query: &str,
        mode: Mode,
        filters: &Filters,
        limit: usize,
    ) -> Result<SearchResponse> {
        let limit = limit.clamp(1, 200);
        // Versions of a work: current ones, or those in force on `as_at`.
        // Naming a corpus explicitly always searches it.
        let mut filters = filters.clone();
        {
            let corpora = self.store.corpora()?;
            let mut hide = match &filters.as_at {
                Some(d) => meowlaw_core::versions::not_in_force_at(&corpora, d),
                None => meowlaw_core::versions::superseded(&corpora),
            };
            if let Some(c) = &filters.corpus {
                hide.remove(c);
            }
            filters.exclude.extend(hide);
        }
        let filters = &filters;
        let bm = self.bm.search(query, filters, limit.max(50))?;
        if mode == Mode::Basic {
            let ids: Vec<(String, f32, String)> = bm
                .into_iter()
                .take(limit)
                .map(|h| (h.id, h.score, h.snippet))
                .collect();
            let corpora: HashMap<String, meowlaw_core::Corpus> = self
                .store
                .corpora()?
                .into_iter()
                .map(|c| (c.id.clone(), c))
                .collect();
            let results = self.hydrate(
                ids.iter()
                    .map(|(id, s, sn)| (id.clone(), *s, sn.clone(), vec!["bm25".to_string()]))
                    .collect(),
                &corpora,
            )?;
            return Ok(SearchResponse {
                query: query.to_string(),
                mode,
                citations: vec![],
                definitions: vec![],
                results,
            });
        }

        let w = self.weights;
        let citations = self.resolve_citations(query, filters.corpus.as_deref())?;
        let definitions = self.definitions_in_query(query, filters)?;
        let authority = self.with_cache(|c| c.authority.clone())?;
        let summarised = self.with_cache(|c| c.summarised.clone())?;

        let mut fu = Fusion::default();
        let mut snippets: HashMap<String, String> = HashMap::new();

        let mut rank = 0usize;
        for c in &citations {
            for t in &c.targets {
                fu.add(t, w.citation / (w.k + rank as f32), "citation");
                rank += 1;
            }
        }
        for (i, h) in bm.iter().enumerate() {
            // Hits containing every query term count for more than partial ones.
            let boost = if h.all_terms { 1.5 } else { 1.0 };
            fu.add(&h.id, boost * w.bm25 / (w.k + i as f32), "bm25");
            snippets.insert(h.id.clone(), h.snippet.clone());
        }
        // One vote per defining provision, however many query terms it defines.
        let mut def_targets: Vec<&str> = Vec::new();
        for d in &definitions {
            for t in std::iter::once(d.source.as_str()).chain(d.points_to.as_deref()) {
                if !def_targets.contains(&t) {
                    def_targets.push(t);
                }
            }
        }
        for (i, t) in def_targets.iter().enumerate() {
            fu.add(t, w.definition / (w.k + i as f32), "definition");
        }
        // Graph expansion: provisions cited by, or citing, the strongest hits.
        let seeds: Vec<String> = {
            let mut v: Vec<(&String, &f32)> = fu.score.iter().collect();
            v.sort_by(|a, b| b.1.total_cmp(a.1));
            v.into_iter().take(10).map(|(id, _)| id.clone()).collect()
        };
        let mut graph_votes: HashMap<String, f32> = HashMap::new();
        for (i, s) in seeds.iter().enumerate() {
            let weight = 1.0 / (i as f32 + 1.0);
            for e in self.store.edges_from(s, &[EdgeKind::Cites])? {
                if !e.target.starts_with("def:") {
                    *graph_votes.entry(e.target).or_default() += weight;
                }
            }
            for e in self.store.edges_to(s, &[EdgeKind::Cites])? {
                *graph_votes.entry(e.source).or_default() += weight * 0.5;
            }
            // Semantic neighbours count in proportion to their cosine.
            for e in self.store.edges_from(s, &[EdgeKind::Similar])? {
                *graph_votes.entry(e.target).or_default() += weight * 0.5 * e.weight;
            }
        }
        let mut gv: Vec<(String, f32)> = graph_votes.into_iter().collect();
        gv.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
        for (i, (id, _)) in gv.iter().take(30).enumerate() {
            fu.add(
                &self.readable(id, &summarised),
                w.graph / (w.k + i as f32),
                "graph",
            );
        }
        let vectors = self.vectors.read().unwrap();
        if let (Some(e), Some(vi)) = (&self.embedder, vectors.as_ref())
            && let Ok(qv) = e.embed_query(query)
        {
            for (i, (id, _)) in vi.search(&qv, 50).into_iter().enumerate() {
                fu.add(
                    &self.readable(&id, &summarised),
                    w.vector / (w.k + i as f32),
                    "vector",
                );
            }
        }
        // Authority prior and filters.
        let Fusion { score, mut why } = fu;
        let mut ranked: Vec<(String, f32)> = score
            .into_iter()
            .map(|(id, s)| {
                let a = authority.get(&id).copied().unwrap_or(0) as f32;
                (id, s * (1.0 + w.authority * (1.0 + a).ln()))
            })
            .collect();
        ranked.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
        // Filter on corpus metadata before hydrating, then hydrate only `limit`.
        let corpora: HashMap<String, meowlaw_core::Corpus> = self
            .store
            .corpora()?
            .into_iter()
            .map(|c| (c.id.clone(), c))
            .collect();
        let mut items = Vec::new();
        for (id, s) in ranked {
            if items.len() >= limit {
                break;
            }
            let Some(c) = corpora.get(meowlaw_store::corpus_of(&id)) else {
                continue;
            };
            if !passes(c, filters) {
                continue;
            }
            let reasons = why.remove(&id).unwrap_or_default().into_iter().collect();
            let sn = snippets.remove(&id).unwrap_or_default();
            items.push((id, s, sn, reasons));
        }
        let results = self.hydrate(items, &corpora)?;
        Ok(SearchResponse {
            query: query.to_string(),
            mode,
            citations,
            definitions,
            results,
        })
    }

    /// A summarised document's vector and similarity edges sit on its root;
    /// as a search result, its summary node is the readable form.
    fn readable(&self, id: &str, summarised: &std::collections::HashSet<String>) -> String {
        if summarised.contains(id) {
            format!("{id}:summary")
        } else {
            id.to_string()
        }
    }

    fn definitions_in_query(&self, query: &str, filters: &Filters) -> Result<Vec<Definition>> {
        let terms: Vec<String> = self.with_cache(|c| {
            let Some((ac, terms)) = &c.terms else {
                return vec![];
            };
            let bytes = query.as_bytes();
            ac.find_iter(query)
                .filter(|m| {
                    (m.start() == 0 || !bytes[m.start() - 1].is_ascii_alphanumeric())
                        && (m.end() >= bytes.len() || !bytes[m.end()].is_ascii_alphanumeric())
                })
                .map(|m| terms[m.pattern().as_usize()].clone())
                .collect()
        })?;
        let mut out = Vec::new();
        for t in terms {
            for d in self
                .store
                .definitions_for(&t, filters.corpus.as_deref(), 20)?
            {
                if meowlaw_store::term_key(&d.term) == t {
                    out.push(d);
                }
            }
        }
        out.truncate(20);
        Ok(out)
    }

    fn hydrate(
        &self,
        items: Vec<(String, f32, String, Vec<String>)>,
        corpora: &HashMap<String, meowlaw_core::Corpus>,
    ) -> Result<Vec<Hit>> {
        let mut out = Vec::new();
        for (id, score, snippet, matched_by) in items {
            let Some(n) = self.store.node(&id)? else {
                continue;
            };
            let path = self
                .store
                .ancestors(&id)?
                .into_iter()
                .skip(1)
                .map(|a| a.heading())
                .collect();
            let c = corpora.get(&n.corpus);
            let ct = c.map(|c| c.title.clone()).unwrap_or_default();
            let ck = c.map(|c| c.kind.as_str().to_string()).unwrap_or_default();
            let jurisdiction = c.and_then(|c| c.jurisdiction.clone());
            let snippet = if snippet.is_empty() {
                index::truncate(&n.text, 260)
            } else {
                snippet
            };
            out.push(Hit {
                heading: n.heading(),
                corpus: n.corpus,
                corpus_title: ct,
                corpus_kind: ck,
                jurisdiction,
                id,
                path,
                snippet,
                score,
                matched_by,
            });
        }
        Ok(out)
    }
}

fn passes(c: &meowlaw_core::Corpus, f: &Filters) -> bool {
    !f.exclude.contains(&c.id)
        && f.corpus.as_deref().is_none_or(|x| x == c.id)
        && f.kind
            .as_deref()
            .is_none_or(|k| k.split(',').any(|k| k.trim() == c.kind.as_str()))
        && f.jurisdiction
            .as_deref()
            .is_none_or(|j| c.jurisdiction.as_deref() == Some(j))
}

fn resolve_in(
    c: &Cache,
    registry: &ProfileRegistry,
    text: &str,
    hint: Option<&str>,
) -> Vec<ResolvedCitation> {
    // Try every profile in use (plus generic), merge by span.
    let mut profiles: Vec<&Profile> = c.profiles.values().collect();
    let generic = registry.get("generic").ok();
    if let Some(g) = &generic
        && !c.profiles.contains_key("generic")
    {
        profiles.push(g);
    }
    let mut seen_spans: Vec<(usize, usize)> = Vec::new();
    let mut out = Vec::new();
    for p in profiles {
        for pc in parse_citations(text, p, Some(&c.catalog)) {
            if seen_spans.iter().any(|(s, e)| pc.start < *e && pc.end > *s) {
                continue;
            }
            seen_spans.push((pc.start, pc.end));
            let mut targets = Vec::new();
            match &pc.cited {
                Cited::Case { citation, pin } => {
                    targets.extend(c.catalog.document(citation, pin.as_deref()))
                }
                Cited::Corpus { name } => {
                    targets.extend(c.catalog.corpus_by_name(name).map(str::to_string))
                }
                Cited::Provision {
                    label,
                    numbers,
                    corpus_name,
                    ..
                } => {
                    let named = corpus_name.as_deref().and_then(|n| {
                        c.catalog
                            .corpus_by_name(n)
                            .or_else(|| c.catalog.corpus_suffix(n).map(|(id, _)| id))
                    });
                    let candidates: Vec<String> = match (named, hint) {
                        (Some(id), _) => vec![id.to_string()],
                        (None, Some(h)) if corpus_name.is_none() => vec![h.to_string()],
                        (None, None) if corpus_name.is_none() => c
                            .catalog
                            .corpora
                            .values()
                            .filter(|cp| cp.kind.is_structured() && cp.kind != CorpusKind::Case)
                            .map(|cp| cp.id.clone())
                            .collect(),
                        _ => vec![],
                    };
                    for num in numbers {
                        for cid in &candidates {
                            if let Some(id) = c.catalog.provision(cid, label.as_deref(), num) {
                                targets.push(id.to_string());
                            }
                        }
                    }
                }
            }
            targets.sort();
            targets.dedup();
            targets.truncate(20);
            out.push(ResolvedCitation {
                raw: pc.raw,
                targets,
            });
        }
    }
    out
}

#[derive(Debug, Clone, Serialize)]
pub struct RebuildReport {
    pub corpora: usize,
    pub nodes: usize,
    pub extraction: meowlaw_extract::ExtractionStats,
    pub indexed: usize,
}

/// Re-run extraction over the whole store and rebuild the BM25 index.
pub fn rebuild(engine: &Engine) -> Result<RebuildReport> {
    // replace_extraction bumps the store generation, so running servers reload.
    let docs = engine.store.documents()?;
    let profiles = load_profiles(&engine.registry, docs.iter().map(|d| &d.corpus))?;
    let catalog = build_catalog(
        docs.iter().map(|d| (&d.corpus, d.nodes.as_slice())),
        &profiles,
    );
    let ex = meowlaw_extract::extract(&docs, &catalog, &profiles);
    engine
        .store
        .replace_extraction(&ex.references, &ex.definitions, &ex.edges)?;
    let indexed = engine.bm.rebuild(&engine.store)?;
    engine.invalidate();
    Ok(RebuildReport {
        corpora: docs.len(),
        nodes: docs.iter().map(|d| d.nodes.len()).sum(),
        extraction: ex.stats(),
        indexed,
    })
}
