// Spike 001 server: hybrid retrieval = usearch ANN + tantivy BM25, fused by RRF,
// with an OpenAI query embed, result slicers (type/act) and a static UI.
//
//   cargo run --release --bin serve -- <dir> [port]
//
// Serves GET /            -> index.html (embedded)
//        GET /api/search  -> ?q=...&k=20&type=section,ruling&act=itaa-1936&series=TR,TD
//                            &outcome=yes&year=2010,2011&court=HCA
//                            {embed_ms, ann_ms, bm25_ms, total_ms, hits[], facets{}}
//
// Slicers filter at retrieval time (tantivy filter clause + usearch
// filtered_search), not by trimming a top-N list, so a narrow slice still
// returns its best hits. Facet counts are computed over the *unfiltered*
// candidate pool so every option stays visible — they count chunks (rows) in
// that pool, not documents and not the corpus; facets.scope/pool say so.
//
// SEARCHD_EXPANSION_SEARCH=<n> overrides the HNSW search beam (ef) after the
// index loads; unset keeps the index's own value (usearch auto = 64).
use axum::{
    extract::{Query, State},
    response::Html,
    routing::get,
    Json, Router,
};
use memmap2::Mmap;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tantivy as tv;
use tantivy::query::{BooleanQuery, Occur, Query as TvQuery, TermQuery};
use tantivy::schema::{IndexRecordOption, Value as _};
use usearch::Index as UIndex;

const RRF_K: f32 = 60.0;
const CANDIDATES: usize = 100; // per retriever, before fusion
const WIDE: usize = 2000; // fallback pool when a sliced ANN search comes back thin
/// Above this, a sliced exact scan costs more than filtered HNSW traversal.
const EXACT_SCAN_MAX: usize = 60_000;
const DIM: usize = 1536;

struct App {
    us: UIndex,
    lex: tv::Index,
    lex_reader: tv::IndexReader,
    f_row: tv::schema::Field,
    f_sec: tv::schema::Field,
    f_act: tv::schema::Field,
    f_type: tv::schema::Field,
    f_series: tv::schema::Field,
    /// None only when meta.json lacks the column too (older build)
    f_outcome: Option<tv::schema::Field>,
    f_year: Option<tv::schema::Field>,
    f_court: Option<tv::schema::Field>,
    lex_fields: Vec<tv::schema::Field>,
    meta: Meta,
    /// exact section reference (lowercased) -> row indices, for identifier lookup
    sections: HashMap<String, Vec<u32>>,
    /// slicer indexes, also the facet universe: source type -> rows, source key -> rows
    type_rows: HashMap<String, Vec<u32>>,
    act_rows: HashMap<String, Vec<u32>>,
    /// lowercased value -> rows; rows without the dimension are in none of them
    outcome_rows: HashMap<String, Vec<u32>>,
    year_rows: HashMap<String, Vec<u32>>,
    court_rows: HashMap<String, Vec<u32>>,
    /// mmap of matrix.f32 (rows x DIM f32, unit-normalised) for exact scans of a slice
    vecs: Mmap,
    embed_cache: Mutex<EmbedCache>,
    /// served requests, exposed on /health so the caller can confirm it is
    /// actually reaching us rather than silently falling back
    requests: AtomicU64,
    http: reqwest::Client,
    key: String,
}

#[derive(Deserialize)]
struct Meta {
    t: Vec<String>,
    a: Vec<String>,
    s: Vec<String>,
    h: Vec<String>,
    x: Vec<String>,
    /// embedding id per row — the app's cross-reference lookup keys on it, so it
    /// has to survive the round trip
    id: Vec<i64>,
    /// public-ruling series ("TR", "PS LA"), "" for every other row
    series: Vec<String>,
    /// Slicer dimensions, "" where the dimension does not apply. Absent from an
    /// older meta.json -> empty vec -> that facet comes back empty and its filter
    /// errors, rather than standing in zeroes for real data.
    /// private rulings: "yes" / "no" / "mixed"
    #[serde(default)]
    outcome: Vec<String>,
    /// "2010": rulings, cases, private rulings (date of advice)
    #[serde(default)]
    year: Vec<String>,
    /// cases: "HCA", "FCAFC", "AATA", ...
    #[serde(default)]
    court: Vec<String>,
}

#[derive(Deserialize)]
struct SearchQ {
    q: String,
    k: Option<usize>,
    /// comma-separated source types: section,ruling,case,regulatory_guide,afsa_guide
    #[serde(rename = "type")]
    type_filter: Option<String>,
    /// comma-separated source/act keys: itaa-1997, gst-1999, private, ...
    act: Option<String>,
    /// comma-separated public-ruling series: TR,TD,PS LA (case-insensitive)
    series: Option<String>,
    /// comma-separated private-ruling outcomes: yes,no,mixed
    outcome: Option<String>,
    /// comma-separated years: 2010,2011
    year: Option<String>,
    /// comma-separated courts: HCA,FCAFC (case-insensitive)
    court: Option<String>,
    /// "lex" = BM25 only, no network call at all (the instant first paint)
    mode: Option<String>,
}

/// The slicer selection.
#[derive(Default, Clone)]
struct Filter {
    types: Option<HashSet<String>>,
    acts: Option<HashSet<String>>,
    /// Mirrors the app's rtype mask: constrains public rulings to these series
    /// and lets every other row through (sections, cases, private rulings).
    series: Option<HashSet<String>>,
    /// Strict, unlike series: only rows that *have* one of these values pass, so
    /// court=HCA is HCA cases alone, not HCA cases plus every non-case row.
    outcome: Option<HashSet<String>>,
    year: Option<HashSet<String>>,
    court: Option<HashSet<String>>,
}

impl Filter {
    fn active(&self) -> bool {
        self.types.is_some()
            || self.acts.is_some()
            || self.series.is_some()
            || self.outcome.is_some()
            || self.year.is_some()
            || self.court.is_some()
    }
}

/// `x=` means no filter, as for series.
fn split_dim(v: &Option<String>) -> Option<HashSet<String>> {
    split_filter(v).filter(|s| !s.is_empty())
}

/// Lowercased series set; "psla" is the same series as "ps la".
fn split_series(v: &Option<String>) -> Option<HashSet<String>> {
    split_filter(v)
        .map(|s| s.into_iter().map(|x| if x == "psla" { "ps la".into() } else { x }).collect::<HashSet<_>>())
        .filter(|s| !s.is_empty()) // `series=` means no filter
}

#[derive(Serialize)]
struct Hit {
    row: u64,
    embedding_id: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    score: Option<f32>,
    rrf: f32,
    ann: bool,
    bm25: bool,
    exact: bool,
    kind: String,
    act: String,
    section: String,
    title: String,
    snippet: String,
}

#[derive(Serialize)]
struct Facet {
    value: String,
    count: u32,
}

#[derive(Serialize, Default)]
struct Facets {
    types: Vec<Facet>,
    acts: Vec<Facet>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    series: Vec<Facet>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    outcome: Vec<Facet>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    year: Vec<Facet>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    court: Vec<Facet>,
    /// What the counts are over: "candidates" = the fused, unfiltered retrieval
    /// pool (ANN ∪ BM25, <= 2 x CANDIDATES rows), not the page and not the
    /// corpus. A count is chunks in that pool; a document with many chunks
    /// counts many times.
    #[serde(skip_serializing_if = "str::is_empty")]
    scope: &'static str,
    /// distinct rows in that pool
    #[serde(skip_serializing_if = "Option::is_none")]
    pool: Option<usize>,
}

#[derive(Serialize)]
struct Resp {
    query: String,
    embed_ms: f64,
    ann_ms: f64,
    bm25_ms: f64,
    total_ms: f64,
    /// true when this is the instant BM25-only paint (no embed was spent)
    lexical_only: bool,
    hits: Vec<Hit>,
    facets: Facets,
    error: Option<String>,
}

/// Small LRU of query embeddings: re-typing or re-running a query then costs no
/// network at all.
#[derive(Default)]
struct EmbedCache {
    map: HashMap<String, Vec<f32>>,
    order: std::collections::VecDeque<String>,
}

impl EmbedCache {
    const CAP: usize = 512;

    fn get(&self, k: &str) -> Option<Vec<f32>> {
        self.map.get(k).cloned()
    }

    fn put(&mut self, k: String, v: Vec<f32>) {
        if self.map.insert(k.clone(), v).is_none() {
            self.order.push_back(k);
        }
        while self.order.len() > Self::CAP {
            if let Some(old) = self.order.pop_front() {
                self.map.remove(&old);
            }
        }
    }
}

fn env_key(name: &str) -> String {
    if let Ok(v) = std::env::var(name) {
        if !v.is_empty() {
            return v;
        }
    }
    std::env::var("HERMES_ENV_FILE")
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .unwrap_or_default()
        .lines()
        .find_map(|l| l.strip_prefix(&format!("{name}=")).map(|v| v.trim().to_string()))
        .unwrap_or_default()
}

async fn embed(app: &App, q: &str) -> Result<Vec<f32>, String> {
    let key = q.trim().to_lowercase();
    let hit = app.embed_cache.lock().ok().and_then(|c| c.get(&key));
    if let Some(v) = hit {
        return Ok(v);
    }
    let v = embed_uncached(app, q).await?;
    if let Ok(mut c) = app.embed_cache.lock() {
        c.put(key, v.clone());
    }
    Ok(v)
}

async fn embed_uncached(app: &App, q: &str) -> Result<Vec<f32>, String> {
    let body = serde_json::json!({
        "model": "text-embedding-3-small",
        "input": [q],
        "dimensions": 1536
    });
    let r = app
        .http
        .post("https://api.openai.com/v1/embeddings")
        .bearer_auth(&app.key)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("openai: {e}"))?;
    let status = r.status();
    let v: serde_json::Value = r.json().await.map_err(|e| format!("openai body: {e}"))?;
    if !status.is_success() {
        return Err(format!("openai {status}: {v}"));
    }
    let arr = v["data"][0]["embedding"]
        .as_array()
        .ok_or_else(|| format!("no embedding in response: {v}"))?;
    Ok(arr.iter().map(|x| x.as_f64().unwrap_or(0.0) as f32).collect())
}

fn split_filter(v: &Option<String>) -> Option<HashSet<String>> {
    v.as_ref().map(|s| {
        s.split(',')
            .map(|x| x.trim().to_lowercase())
            .filter(|x| !x.is_empty())
            .collect::<HashSet<_>>()
    })
}

/// Does this row survive the slicers? (also used as the usearch predicate)
fn passes(app: &App, row: u64, f: &Filter) -> bool {
    let i = row as usize;
    if i >= app.meta.t.len() {
        return false;
    }
    if let Some(ts) = &f.types {
        if !ts.contains(&app.meta.t[i].to_lowercase()) {
            return false;
        }
    }
    if let Some(as_) = &f.acts {
        if !as_.contains(&app.meta.a[i].to_lowercase()) {
            return false;
        }
    }
    if let Some(ss) = &f.series {
        if app.meta.t[i] == "ruling" && !ss.contains(&app.meta.series[i].to_lowercase()) {
            return false;
        }
    }
    for (set, col) in [(&f.outcome, &app.meta.outcome), (&f.year, &app.meta.year), (&f.court, &app.meta.court)] {
        if let Some(vals) = set {
            // strict: a row without the dimension is in no slice of it
            match col.get(i) {
                Some(v) if vals.contains(&v.to_lowercase()) => {}
                _ => return false,
            }
        }
    }
    true
}

/// OR of exact terms — the tantivy-side slicer clause.
fn or_terms(field: tv::schema::Field, vals: &HashSet<String>) -> Box<dyn TvQuery> {
    let ors: Vec<(Occur, Box<dyn TvQuery>)> = vals
        .iter()
        .map(|v| {
            let tq: Box<dyn TvQuery> = Box::new(TermQuery::new(
                tv::Term::from_field_text(field, v),
                IndexRecordOption::Basic,
            ));
            (Occur::Should, tq)
        })
        .collect();
    Box::new(BooleanQuery::new(ors))
}

fn filter_clause(app: &App, f: &Filter) -> Option<Box<dyn TvQuery>> {
    let mut parts: Vec<(Occur, Box<dyn TvQuery>)> = Vec::new();
    if let Some(ts) = &f.types {
        parts.push((Occur::Must, or_terms(app.f_type, ts)));
    }
    if let Some(as_) = &f.acts {
        parts.push((Occur::Must, or_terms(app.f_act, as_)));
    }
    if let Some(ss) = &f.series {
        // in the series, or not a public ruling at all
        let not_ruling: Box<dyn TvQuery> = Box::new(BooleanQuery::new(vec![
            (Occur::Must, Box::new(tv::query::AllQuery) as Box<dyn TvQuery>),
            (
                Occur::MustNot,
                Box::new(TermQuery::new(
                    tv::Term::from_field_text(app.f_type, "ruling"),
                    IndexRecordOption::Basic,
                )),
            ),
        ]));
        parts.push((
            Occur::Must,
            Box::new(BooleanQuery::new(vec![
                (Occur::Should, or_terms(app.f_series, ss)),
                (Occur::Should, not_ruling),
            ])),
        ));
    }
    for (set, field) in [(&f.outcome, app.f_outcome), (&f.year, app.f_year), (&f.court, app.f_court)] {
        if let (Some(vals), Some(field)) = (set, field) {
            parts.push((Occur::Must, or_terms(field, vals)));
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(Box::new(BooleanQuery::new(parts)))
    }
}

/// BM25 over the corpus, optionally restricted by the slicers. Best-first.
fn lex_search(app: &App, q: &str, limit: usize, f: &Filter) -> Vec<(u64, f32)> {
    let mut parser = tv::query::QueryParser::for_index(&app.lex, app.lex_fields.clone());
    // a literal section-number match should outrank prose that merely mentions it
    parser.set_field_boost(app.f_sec, 8.0);
    let user = match parser.parse_query(q) {
        Ok(query) => query,
        Err(_) => return vec![],
    };
    let query: Box<dyn TvQuery> = match filter_clause(app, f) {
        Some(clause) => Box::new(BooleanQuery::new(vec![
            (Occur::Must, user),
            (Occur::Must, clause),
        ])),
        None => user,
    };
    let searcher = app.lex_reader.searcher();
    let collector = tv::collector::TopDocs::with_limit(limit).order_by_score();
    let top = match searcher.search(&query, &collector) {
        Ok(t) => t,
        Err(_) => return vec![],
    };
    top.into_iter()
        .filter_map(|(score, addr)| {
            let doc: tv::TantivyDocument = searcher.doc(addr).ok()?;
            let row = doc.get_first(app.f_row)?.as_value().as_u64()?;
            Some((row, score))
        })
        .collect()
}

/// HNSW search, optionally restricted by the slicers.
fn ann_search(app: &App, emb: &[f32], n: usize, f: &Filter) -> Vec<(u64, f32)> {
    let res = if f.active() {
        app.us.filtered_search(emb, n, |key| passes(app, key, f))
    } else {
        app.us.search(emb, n)
    };
    match res {
        Ok(m) => m
            .keys
            .iter()
            .zip(m.distances.iter())
            .map(|(k, d)| (*k, 1.0 - *d))
            .collect(),
        Err(_) => vec![],
    }
}

fn intersect_sorted(a: &[u32], b: &[u32]) -> Vec<u32> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0usize, 0usize);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out
}

/// The row set the slicers select: OR within a facet, AND between facets.
fn slice_rows(app: &App, f: &Filter) -> Option<Vec<u32>> {
    let mut rows: Option<Vec<u32>> = None;
    let add = |rows: Option<Vec<u32>>, vals: &HashSet<String>, map: &HashMap<String, Vec<u32>>| {
        let mut v: Vec<u32> = vals
            .iter()
            .filter_map(|k| map.get(&k.to_lowercase()))
            .flatten()
            .copied()
            .collect();
        v.sort_unstable();
        v.dedup();
        Some(match rows {
            None => v,
            Some(prev) => intersect_sorted(&prev, &v),
        })
    };
    if let Some(ts) = &f.types {
        rows = add(rows, ts, &app.type_rows);
    }
    if let Some(as_) = &f.acts {
        rows = add(rows, as_, &app.act_rows);
    }
    for (set, map) in [(&f.outcome, &app.outcome_rows), (&f.year, &app.year_rows), (&f.court, &app.court_rows)] {
        if let Some(vals) = set {
            rows = add(rows, vals, map);
        }
    }
    // series is a predicate over whatever the other slicers selected (or the
    // whole corpus), so a narrow type=ruling&series=TR still gets the exact scan
    if f.series.is_some() {
        let base = rows.unwrap_or_else(|| (0..app.meta.t.len() as u32).collect());
        rows = Some(base.into_iter().filter(|r| passes(app, *r as u64, f)).collect());
    }
    rows
}

/// Exact cosine search over a slice of rows. matrix.f32 is unit-normalised, so
/// the dot product *is* the cosine. Beats filtered HNSW on a narrow slice, and
/// returns 100% recall inside the slice.
fn slice_search(app: &App, emb: &[f32], rows: &[u32], n: usize) -> Vec<(u64, f32)> {
    let bytes = app.vecs.as_ref();
    let mut scored: Vec<(f32, u64)> = rows
        .par_iter()
        .map(|r| {
            let off = (*r as usize) * DIM * 4;
            // mmap base is page-aligned and off is a multiple of 4, so the f32
            // view is aligned; the file is a fixed read-only dump.
            let v = unsafe { std::slice::from_raw_parts(bytes.as_ptr().add(off) as *const f32, DIM) };
            let mut s = 0.0f32;
            for i in 0..DIM {
                s += v[i] * emb[i];
            }
            (s, *r as u64)
        })
        .collect();
    scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    scored.truncate(n);
    scored.into_iter().map(|(s, r)| (r, s)).collect()
}

fn top_facets(m: HashMap<String, u32>, n: usize) -> Vec<Facet> {
    let mut v: Vec<Facet> = m.into_iter().map(|(value, count)| Facet { value, count }).collect();
    v.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.value.cmp(&b.value)));
    v.truncate(n);
    v
}

/// Facet counts over the whole unfiltered candidate pool, so every option
/// stays visible and pickable while a slice is applied.
fn compute_facets(app: &App, bm: &[(u64, f32)], ann: &[(u64, f32)]) -> Facets {
    let mut seen = HashSet::new();
    let mut tcount: HashMap<String, u32> = HashMap::new();
    let mut acount: HashMap<String, u32> = HashMap::new();
    // series, outcome, year, court: rows without the dimension are not counted
    let cols = [&app.meta.series, &app.meta.outcome, &app.meta.year, &app.meta.court];
    let mut dims: [HashMap<String, u32>; 4] = Default::default();
    for row in ann.iter().map(|x| x.0).chain(bm.iter().map(|x| x.0)) {
        if !seen.insert(row) {
            continue;
        }
        let i = row as usize;
        if i >= app.meta.t.len() {
            continue;
        }
        *tcount.entry(app.meta.t[i].clone()).or_default() += 1;
        *acount.entry(app.meta.a[i].clone()).or_default() += 1;
        for (d, col) in dims.iter_mut().zip(cols) {
            if let Some(v) = col.get(i).filter(|v| !v.is_empty()) {
                *d.entry(v.clone()).or_default() += 1;
            }
        }
    }

    // Source types are a fixed enumeration — always offer all of them, even when
    // the current pool holds none: the slice is where the answer is.
    let mut types: Vec<Facet> = app
        .type_rows
        .iter()
        .map(|(v, _)| Facet { value: v.clone(), count: *tcount.get(v).unwrap_or(&0) })
        .collect();
    types.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.value.cmp(&b.value)));

    // Acts: whatever the query surfaced, plus the biggest sources, so the
    // legislation stays reachable from a ruling-dominated pool.
    for (k, _) in top_sources(app, 16) {
        acount.entry(k).or_insert(0);
    }
    let global = |k: &str| app.act_rows.get(k).map(|v| v.len()).unwrap_or(0);
    let mut acts: Vec<Facet> = acount
        .into_iter()
        .map(|(value, count)| Facet { value, count })
        .collect();
    acts.sort_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| global(&b.value).cmp(&global(&a.value)))
            .then_with(|| a.value.cmp(&b.value))
    });
    acts.truncate(16);

    let [series, outcome, year, court] = dims.map(|m| top_facets(m, usize::MAX));
    Facets { types, acts, series, outcome, year, court, scope: "candidates", pool: Some(seen.len()) }
}

/// The largest sources in the corpus, by row count.
fn top_sources(app: &App, n: usize) -> Vec<(String, usize)> {
    let mut v: Vec<(String, usize)> = app.act_rows.iter().map(|(k, r)| (k.clone(), r.len())).collect();
    v.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    v.truncate(n);
    v
}

fn empty(q: String, embed_ms: f64, ann_ms: f64, bm25_ms: f64, total_ms: f64, error: Option<String>) -> Json<Resp> {
    Json(Resp {
        query: q,
        embed_ms,
        ann_ms,
        bm25_ms,
        total_ms,
        lexical_only: false,
        hits: vec![],
        facets: Facets::default(),
        error,
    })
}

async fn search(State(app): State<Arc<App>>, Query(p): Query<SearchQ>) -> Json<Resp> {
    let t0 = Instant::now();
    app.requests.fetch_add(1, Ordering::Relaxed);
    let q = p.q.trim().to_string();
    let k = p.k.unwrap_or(20).clamp(1, 100);
    if q.is_empty() {
        return empty(q, 0.0, 0.0, 0.0, 0.0, None);
    }
    let f = Filter {
        types: split_filter(&p.type_filter),
        acts: split_filter(&p.act),
        series: split_series(&p.series),
        outcome: split_dim(&p.outcome),
        year: split_dim(&p.year),
        court: split_dim(&p.court),
    };
    let none = Filter::default();
    // a filter on a dimension this meta.json does not carry cannot be honoured
    for (name, set, col) in [
        ("outcome", &f.outcome, &app.meta.outcome),
        ("year", &f.year, &app.meta.year),
        ("court", &f.court, &app.meta.court),
    ] {
        if set.is_some() && col.is_empty() {
            let msg = format!("{name} filter unavailable: meta.json has no {name} column (rerun build_index.py + lexbuild)");
            return empty(q, 0.0, 0.0, 0.0, t0.elapsed().as_secs_f64() * 1000.0, Some(msg));
        }
    }

    // BM25 is real work (~1ms warm, ~80ms on a cold page-in), and each path
    // needs it in a different place — lex needs it alone, ann needs none of it
    // at all — so each path computes it on demand rather than all three paying
    // for it up front.

    // legal search is often by section number: an exact match is decisive
    let exact: Vec<u64> = section_ref(&q)
        .and_then(|r| app.sections.get(&r).cloned())
        .map(|rows| rows.into_iter().map(|r| r as u64).collect::<Vec<u64>>())
        .unwrap_or_default()
        .into_iter()
        .filter(|r| passes(&app, *r, &f))
        .collect();

    // --- instant path: BM25 only, no embed, no network ---
    // The UI paints this straight away (top 10) and then upgrades to hybrid.
    if p.mode.as_deref() == Some("lex") {
        let t = Instant::now();
        let bm_base = lex_search(&app, &q, CANDIDATES, &none);
        let bm = if f.active() { lex_search(&app, &q, CANDIDATES, &f) } else { bm_base.clone() };
        let bm25_ms = t.elapsed().as_secs_f64() * 1000.0;
        let hits = build_hits(&app, &bm, &[], &exact, k);
        return Json(Resp {
            query: q,
            embed_ms: 0.0,
            ann_ms: 0.0,
            bm25_ms,
            total_ms: t0.elapsed().as_secs_f64() * 1000.0,
            lexical_only: true,
            hits,
            facets: compute_facets(&app, &bm_base, &[]),
            error: None,
        });
    }

    // --- semantic (ANN): needs the query embed ---
    let t = Instant::now();
    let emb = match embed(&app, &q).await {
        Ok(v) => v,
        Err(e) => {
            let embed_ms = t.elapsed().as_secs_f64() * 1000.0;
            // degrade to BM25-only rather than fail
            let t = Instant::now();
            let bm_base = lex_search(&app, &q, CANDIDATES, &none);
            let bm = if f.active() { lex_search(&app, &q, CANDIDATES, &f) } else { bm_base.clone() };
            let bm25_ms = t.elapsed().as_secs_f64() * 1000.0;
            let hits = build_hits(&app, &bm, &[], &exact, k);
            return Json(Resp {
                query: q,
                embed_ms,
                ann_ms: 0.0,
                bm25_ms,
                total_ms: t0.elapsed().as_secs_f64() * 1000.0,
                lexical_only: false,
                hits,
                facets: compute_facets(&app, &bm_base, &[]),
                error: Some(format!("{e} (BM25-only results shown)")),
            });
        }
    };
    let embed_ms = t.elapsed().as_secs_f64() * 1000.0;

    let t = Instant::now();
    let ann_base = ann_search(&app, &emb, CANDIDATES, &none);
    let ann: Vec<(u64, f32)> = if f.active() {
        match slice_rows(&app, &f) {
            // small slice: an exact scan is exact, and faster than HNSW with a
            // selective predicate (which otherwise walks the graph for ages)
            Some(rows) if rows.len() <= EXACT_SCAN_MAX => slice_search(&app, &emb, &rows, CANDIDATES),
            _ => {
                let sliced = ann_search(&app, &emb, CANDIDATES, &f);
                if sliced.is_empty() {
                    ann_search(&app, &emb, WIDE, &none)
                        .into_iter()
                        .filter(|(r, _)| passes(&app, *r, &f))
                        .collect()
                } else {
                    sliced
                }
            }
        }
    } else {
        ann_base.clone()
    };
    let ann_ms = t.elapsed().as_secs_f64() * 1000.0;

    // ANN-only: the app supplies its own lexical half (FTS5 across seven
    // tables), so fusing searchd's BM25 in as well would count lexical evidence
    // twice. Used when the route swaps just its vector retrieval over.
    if p.mode.as_deref() == Some("ann") {
        return Json(Resp {
            query: q,
            embed_ms,
            ann_ms,
            bm25_ms: 0.0,
            total_ms: t0.elapsed().as_secs_f64() * 1000.0,
            lexical_only: false,
            hits: build_hits(&app, &[], &ann, &exact, k),
            facets: compute_facets(&app, &[], &ann_base),
            error: None,
        });
    }

    let t = Instant::now();
    let bm_base = lex_search(&app, &q, CANDIDATES, &none);
    let bm = if f.active() { lex_search(&app, &q, CANDIDATES, &f) } else { bm_base.clone() };
    let bm25_ms = t.elapsed().as_secs_f64() * 1000.0;

    let facets = compute_facets(&app, &bm_base, &ann_base);
    let hits = build_hits(&app, &bm, &ann, &exact, k);
    Json(Resp {
        query: q,
        embed_ms,
        ann_ms,
        bm25_ms,
        total_ms: t0.elapsed().as_secs_f64() * 1000.0,
        lexical_only: false,
        hits,
        facets,
        error: None,
    })
}

/// If the whole query reads as a section reference ("26AH", "s 26AH",
/// "section 160AAB"), return it normalised. Prose queries return None so they
/// are never hijacked by an incidental token match.
fn section_ref(q: &str) -> Option<String> {
    let t = q.trim().to_lowercase();
    let core = ["section ", "sec ", "s "]
        .iter()
        .find_map(|p| t.strip_prefix(p))
        .unwrap_or(&t)
        .trim()
        .to_string();
    let looks_like_ref = !core.is_empty()
        && core.len() <= 12
        && core.chars().any(|c| c.is_ascii_digit())
        && core.chars().all(|c| c.is_ascii_alphanumeric() || c == '-');
    looks_like_ref.then_some(core)
}

/// Reciprocal Rank Fusion of the BM25 and ANN lists, then materialise hits.
fn build_hits(app: &App, bm: &[(u64, f32)], ann: &[(u64, f32)], exact: &[u64], k: usize) -> Vec<Hit> {
    let mut fused: HashMap<u64, (f32, bool, bool, f32, bool)> = HashMap::new();
    for (rank, (row, cos)) in ann.iter().enumerate() {
        let e = fused.entry(*row).or_insert((0.0, false, false, f32::NAN, false));
        e.0 += 1.0 / (RRF_K + rank as f32 + 1.0);
        e.1 = true;
        e.3 = *cos;
    }
    for (rank, (row, _)) in bm.iter().enumerate() {
        let e = fused.entry(*row).or_insert((0.0, false, false, f32::NAN, false));
        e.0 += 1.0 / (RRF_K + rank as f32 + 1.0);
        e.2 = true;
    }
    // a literal section match is decisive — outrank every rank-based fusion score
    for row in exact {
        let e = fused.entry(*row).or_insert((0.0, false, false, f32::NAN, false));
        e.0 += 1.0;
        e.4 = true;
    }

    let mut rows: Vec<(u64, (f32, bool, bool, f32, bool))> = fused.into_iter().collect();
    rows.sort_by(|a, b| {
        b.1 .0
            .partial_cmp(&a.1 .0)
            .unwrap_or(std::cmp::Ordering::Equal)
            // rank ties are common at the top of two lists; prefer the lexical
            // match (high precision) and keep the order deterministic
            .then_with(|| b.1 .2.cmp(&a.1 .2))
            .then_with(|| a.0.cmp(&b.0))
    });

    // collapse chunk duplicates: one hit per (act, section)
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut out: Vec<Hit> = Vec::with_capacity(k);
    for (row, (rrf, is_ann, is_bm, cos, is_exact)) in rows {
        let i = row as usize;
        if i >= app.meta.t.len() {
            continue;
        }
        if !seen.insert((app.meta.a[i].clone(), app.meta.s[i].clone())) {
            continue;
        }
        out.push(Hit {
            row,
            embedding_id: app.meta.id[i],
            score: if is_ann && cos.is_finite() { Some(cos) } else { None },
            rrf,
            ann: is_ann,
            bm25: is_bm,
            exact: is_exact,
            kind: app.meta.t[i].clone(),
            act: app.meta.a[i].clone(),
            section: app.meta.s[i].clone(),
            title: app.meta.h[i].clone(),
            snippet: app.meta.x[i].clone(),
        });
        if out.len() >= k {
            break;
        }
    }
    out
}

async fn health(State(app): State<Arc<App>>) -> Json<serde_json::Value> {
    // cheap readiness probe for the app's feature detection — never touches the
    // network or the indexes beyond what is already resident
    Json(serde_json::json!({
        "ok": true,
        "rows": app.meta.t.len(),
        "ann": app.us.size(),
        "sections": app.sections.len(),
        "requests": app.requests.load(Ordering::Relaxed),
        "openai_key": !app.key.is_empty(),
        "expansion_search": app.us.expansion_search(),
    }))
}

async fn ui() -> impl axum::response::IntoResponse {
    // this page is rebuilt constantly — never let a browser cache a stale build
    (
        [(
            axum::http::header::CACHE_CONTROL,
            "no-store, no-cache, must-revalidate, max-age=0",
        )],
        Html(include_str!("../../index.html")),
    )
}

#[tokio::main]
async fn main() {
    let dir = std::env::args().nth(1).unwrap_or_else(|| ".".into());
    let port: u16 = std::env::args().nth(2).and_then(|p| p.parse().ok()).unwrap_or(8790);

    // Which ANN index to load. Set per host rather than baking the smaller
    // index into the default: f32 where there is RAM for best recall, i8 on a
    // small droplet (3.7x smaller, ~93% recall).
    let index_file = std::env::var("SEARCHD_INDEX").unwrap_or_else(|_| "idx_f32.usearch".into());

    let t = Instant::now();
    let us = UIndex::restore(&format!("{dir}/{index_file}")).expect("restore ANN index");
    // Recall knob, per deployment: both units run this binary, so unset must
    // mean "exactly as before" (the index's own value).
    if let Some(v) = std::env::var("SEARCHD_EXPANSION_SEARCH").ok().filter(|v| !v.trim().is_empty()) {
        let es: usize = v
            .trim()
            .parse()
            .ok()
            .filter(|n| *n > 0)
            .unwrap_or_else(|| panic!("SEARCHD_EXPANSION_SEARCH={v:?} is not a positive integer"));
        us.change_expansion_search(es);
    }
    println!(
        "ANN index loaded (mmap) in {:.0} ms | {} vectors | expansion_search {}",
        t.elapsed().as_secs_f64() * 1000.0,
        us.size(),
        us.expansion_search()
    );

    let t = Instant::now();
    let lex = tv::Index::open_in_dir(format!("{dir}/tantivy_idx")).expect("open tantivy index");
    let lex_reader = lex.reader().expect("tantivy reader");
    let schema = lex.schema();
    let f_row = schema.get_field("row").expect("row field");
    let f_sec = schema.get_field("section").expect("section field");
    let f_act = schema.get_field("act").expect("act field");
    let f_type = schema.get_field("type").expect("type field");
    let f_series = schema.get_field("series").expect("series field (rerun lexbuild)");
    let f_outcome = schema.get_field("outcome").ok();
    let f_year = schema.get_field("year").ok();
    let f_court = schema.get_field("court").ok();
    let lex_fields: Vec<tv::schema::Field> = ["section", "act", "title", "text"]
        .iter()
        .filter_map(|f| schema.get_field(f).ok())
        .collect();
    println!(
        "BM25 index opened in {:.0} ms | {} docs",
        t.elapsed().as_secs_f64() * 1000.0,
        lex_reader.searcher().num_docs()
    );

    let t = Instant::now();
    let meta: Meta =
        serde_json::from_slice(&std::fs::read(format!("{dir}/meta.json")).expect("meta.json"))
            .expect("parse meta");
    println!("meta loaded in {:.0} ms | {} rows", t.elapsed().as_secs_f64() * 1000.0, meta.t.len());
    for (name, col, field) in [
        ("outcome", &meta.outcome, f_outcome),
        ("year", &meta.year, f_year),
        ("court", &meta.court, f_court),
    ] {
        if col.is_empty() {
            println!("meta.json has no `{name}` column — {name} facet empty, {name} filter disabled");
            continue;
        }
        assert_eq!(col.len(), meta.t.len(), "meta.json `{name}` has {} rows, expected {}", col.len(), meta.t.len());
        assert!(field.is_some(), "meta.json has `{name}` but tantivy_idx does not — rerun lexbuild");
    }

    // exact section-reference lookup: "26AH" must find section 26AH, not prose about it
    let t = Instant::now();
    let mut sections: HashMap<String, Vec<u32>> = HashMap::new();
    for (i, s) in meta.s.iter().enumerate() {
        let key = s.trim().to_lowercase();
        if !key.is_empty() {
            sections.entry(key).or_default().push(i as u32);
        }
    }
    println!(
        "section map in {:.0} ms | {} distinct refs",
        t.elapsed().as_secs_f64() * 1000.0,
        sections.len()
    );

    // slicer indexes, which double as the facet universe
    let t = Instant::now();
    let mut type_rows: HashMap<String, Vec<u32>> = HashMap::new();
    let mut act_rows: HashMap<String, Vec<u32>> = HashMap::new();
    for i in 0..meta.t.len() {
        type_rows.entry(meta.t[i].to_lowercase()).or_default().push(i as u32);
        act_rows.entry(meta.a[i].to_lowercase()).or_default().push(i as u32);
    }
    let dim_rows = |col: &[String]| {
        let mut m: HashMap<String, Vec<u32>> = HashMap::new();
        for (i, v) in col.iter().enumerate().filter(|(_, v)| !v.is_empty()) {
            m.entry(v.to_lowercase()).or_default().push(i as u32);
        }
        m
    };
    let (outcome_rows, year_rows, court_rows) = (dim_rows(&meta.outcome), dim_rows(&meta.year), dim_rows(&meta.court));
    println!(
        "slicer indexes in {:.0} ms | {} types | {} sources",
        t.elapsed().as_secs_f64() * 1000.0,
        type_rows.len(),
        act_rows.len()
    );

    // raw vectors for exact scans of a slice; mmap, so no RAM cost up front
    let t = Instant::now();
    let vecs = unsafe {
        Mmap::map(&std::fs::File::open(format!("{dir}/matrix.f32")).expect("matrix.f32"))
            .expect("mmap matrix.f32")
    };
    let expected = meta.a.len() * DIM * 4;
    assert_eq!(vecs.len(), expected, "matrix.f32 is {} bytes, expected {expected}", vecs.len());
    println!(
        "matrix mmap in {:.0} ms | {} x {DIM} f32",
        t.elapsed().as_secs_f64() * 1000.0,
        meta.a.len()
    );

    // The ANN index and the metadata must describe the same corpus. If they
    // drift (a stale index left next to fresh data), every returned key is
    // silently off by the size difference — so fail loudly at startup instead.
    assert_eq!(
        us.size(),
        meta.a.len(),
        "ANN index holds {} vectors but meta.json has {} rows — rebuild the index (indexbuild)",
        us.size(),
        meta.a.len()
    );

    let key = env_key("OPENAI_API_KEY");
    if key.is_empty() {
        eprintln!("WARNING: OPENAI_API_KEY not set — semantic half will fail (BM25 still works)");
    }

    let app = Arc::new(App {
        us,
        lex,
        lex_reader,
        f_row,
        f_sec,
        f_act,
        f_type,
        f_series,
        f_outcome,
        f_year,
        f_court,
        lex_fields,
        meta,
        sections,
        type_rows,
        act_rows,
        outcome_rows,
        year_rows,
        court_rows,
        vecs,
        embed_cache: Mutex::new(EmbedCache::default()),
        requests: AtomicU64::new(0),
        http: reqwest::Client::new(),
        key,
    });

    let router = Router::new()
        .route("/", get(ui))
        .route("/health", get(health))
        .route("/api/search", get(search))
        .with_state(app);

    // Loopback by default. This service has no authentication of its own — the
    // Python app owns auth, sessions and metering and proxies to it — so
    // exposing it on the network would be an open, unmetered corpus endpoint.
    // SEARCHD_BIND exists only for deliberate debugging; never set it in a unit.
    let host = std::env::var("SEARCHD_BIND").unwrap_or_else(|_| "127.0.0.1".into());
    let addr = format!("{host}:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.expect("bind");
    println!("searchd listening on http://{addr}");
    axum::serve(listener, router).await.unwrap();
}
