//! Dense vectors: chunked embeddings, vector retrieval, and `similar` edges.
//!
//! Each node's text is split into chunks of at most [`CHUNK_CHARS`]; every
//! chunk gets a header (corpus, heading) and its own vector. A node's score for
//! a query is its best chunk. For the graph, each node gets one vector (the
//! normalised mean of its chunks) and keeps its top-k neighbours above a
//! threshold as weighted `similar` edges, plus a per-kind quota so a provision
//! always reaches its nearest cases and guidance even when other provisions
//! crowd them out of the top-k.

use anyhow::{Context, Result, bail};
use meowlaw_core::{CorpusKind, Edge, EdgeKind, Method};
use meowlaw_store::Store;
use rayon::prelude::*;
use std::collections::HashMap;

/// Upper bound on characters per embedded chunk.
pub const CHUNK_CHARS: usize = 1_500;

pub trait Embedder: Send + Sync {
    /// Identifier stored with each vector so models are never mixed.
    fn model(&self) -> &str;
    fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>>;
    /// Embed a search query. Models with instruction prefixes override this.
    fn embed_query(&self, query: &str) -> Result<Vec<f32>> {
        self.embed(&[query.to_string()])?
            .pop()
            .context("embedder returned no vector")
    }
}

pub fn normalise(v: &mut [f32]) {
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 0.0 {
        v.iter_mut().for_each(|x| *x /= n);
    }
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// Split text at paragraph, line or sentence boundaries into chunks of at most `max` chars.
pub fn chunk_text(text: &str, max: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let push = |cur: &mut String, out: &mut Vec<String>| {
        let t = cur.trim();
        if !t.is_empty() {
            out.push(t.to_string());
        }
        cur.clear();
    };
    for piece in text.split_inclusive(['\n', '.', ';', ':']) {
        if cur.chars().count() + piece.chars().count() > max && !cur.is_empty() {
            push(&mut cur, &mut out);
        }
        if piece.chars().count() > max {
            // A single run with no boundary: hard split on characters.
            let chars: Vec<char> = piece.chars().collect();
            for c in chars.chunks(max) {
                cur.extend(c);
                push(&mut cur, &mut out);
            }
            continue;
        }
        cur.push_str(piece);
    }
    push(&mut cur, &mut out);
    out
}

/// In-memory chunk vectors, scanned brute-force.
pub struct VectorIndex {
    ids: Vec<String>,
    vecs: Vec<Vec<f32>>,
}

impl VectorIndex {
    pub fn load(store: &Store, model: &str) -> Result<Self> {
        let mut ids = Vec::new();
        let mut vecs = Vec::new();
        for (id, _, mut v) in store.embeddings(model)? {
            normalise(&mut v);
            ids.push(id);
            vecs.push(v);
        }
        Ok(Self { ids, vecs })
    }

    pub fn is_empty(&self) -> bool {
        self.ids.is_empty()
    }

    pub fn len(&self) -> usize {
        self.ids.len()
    }

    /// Top `k` nodes by best chunk.
    pub fn search(&self, q: &[f32], k: usize) -> Vec<(String, f32)> {
        let mut q = q.to_vec();
        normalise(&mut q);
        let scores: Vec<f32> = self
            .vecs
            .par_iter()
            .map(|v| {
                if v.len() == q.len() {
                    dot(v, &q)
                } else {
                    f32::MIN
                }
            })
            .collect();
        let mut best: HashMap<&str, f32> = HashMap::new();
        for (i, s) in scores.into_iter().enumerate() {
            let e = best.entry(self.ids[i].as_str()).or_insert(f32::MIN);
            if s > *e {
                *e = s;
            }
        }
        let mut v: Vec<(String, f32)> = best
            .into_iter()
            .map(|(id, s)| (id.to_string(), s))
            .collect();
        v.sort_by(|a, b| b.1.total_cmp(&a.1));
        v.truncate(k);
        v
    }
}

/// Any OpenAI-compatible `/v1/embeddings` endpoint (OpenAI, Azure, vLLM,
/// llama.cpp server, LM Studio, Ollama's `/v1` API, ...).
pub struct HttpEmbedder {
    pub url: String,
    pub model: String,
    pub api_key: Option<String>,
    agent: ureq::Agent,
}

impl HttpEmbedder {
    pub fn new(url: impl Into<String>, model: impl Into<String>, api_key: Option<String>) -> Self {
        Self {
            url: url.into(),
            model: model.into(),
            api_key,
            agent: ureq::Agent::new_with_defaults(),
        }
    }
}

impl Embedder for HttpEmbedder {
    fn model(&self) -> &str {
        &self.model
    }

    fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        let body = serde_json::json!({ "model": self.model, "input": texts });
        let mut req = self
            .agent
            .post(&self.url)
            .header("Content-Type", "application/json");
        if let Some(k) = &self.api_key {
            req = req.header("Authorization", &format!("Bearer {k}"));
        }
        let mut resp = req
            .send(serde_json::to_string(&body)?.as_bytes())
            .with_context(|| format!("POST {}", self.url))?;
        let v: serde_json::Value = serde_json::from_reader(resp.body_mut().as_reader())?;
        let Some(data) = v.get("data").and_then(|d| d.as_array()) else {
            bail!("embedding response has no `data` array: {v}");
        };
        let mut out = Vec::with_capacity(data.len());
        for item in data {
            let e = item
                .get("embedding")
                .and_then(|e| e.as_array())
                .context("embedding item without `embedding`")?;
            out.push(
                e.iter()
                    .filter_map(|x| x.as_f64())
                    .map(|x| x as f32)
                    .collect(),
            );
        }
        Ok(out)
    }
}

/// Embed every node with text that has no vectors for `embedder.model()`.
/// Returns (nodes, chunks) embedded.
/// Documents with a summary node are embedded twice over:
/// - the corpus root gets the vector of the summary with citations removed by
///   `strip`; that vector stands for the whole document in the similarity graph;
/// - every paragraph gets its own vector, for pinpoint vector search.
///
/// The summary node itself is not embedded (the root carries it).
pub fn embed_all(
    store: &Store,
    embedder: &dyn Embedder,
    batch: usize,
    strip: &dyn Fn(&meowlaw_core::Corpus, &str) -> String,
    mut progress: impl FnMut(usize, usize),
) -> Result<(usize, usize)> {
    let have = store.embedded_nodes(embedder.model())?;
    // (node id, chunk texts)
    let mut pending: Vec<(String, Vec<String>)> = Vec::new();
    for d in store.documents()? {
        let headings: HashMap<&str, String> = d
            .nodes
            .iter()
            .map(|n| (n.id.as_str(), n.heading()))
            .collect();
        let summary = d
            .nodes
            .iter()
            .find(|n| n.kind == meowlaw_core::NodeKind::Summary)
            .map(|n| n.text.as_str());
        for n in &d.nodes {
            if have.contains(&n.id) || n.kind == meowlaw_core::NodeKind::Summary {
                continue;
            }
            if n.parent.is_none()
                && let Some(sum) = summary
            {
                let text = strip(&d.corpus, sum);
                if !text.is_empty() {
                    let header = format!("{} › Summary\n", d.corpus.title);
                    let chunks = chunk_text(&text, CHUNK_CHARS)
                        .into_iter()
                        .map(|c| format!("{header}{c}"))
                        .collect();
                    pending.push((n.id.clone(), chunks));
                }
                continue;
            }
            if n.text.trim().is_empty() {
                continue;
            }
            let parent = n
                .parent
                .as_deref()
                .and_then(|p| headings.get(p))
                .map(|h| format!(" › {h}"))
                .unwrap_or_default();
            let header = format!("{}{} › {}\n", d.corpus.title, parent, n.heading());
            let chunks = chunk_text(&n.text, CHUNK_CHARS)
                .into_iter()
                .map(|c| format!("{header}{c}"))
                .collect();
            pending.push((n.id.clone(), chunks));
        }
    }
    let total = pending.len();
    let mut nodes = 0;
    let mut chunks = 0;
    // Batch across nodes, keeping each node's chunks together.
    let mut i = 0;
    while i < pending.len() {
        let mut texts = Vec::new();
        let mut owners = Vec::new();
        while i < pending.len() && (texts.is_empty() || texts.len() + pending[i].1.len() <= batch) {
            owners.push((i, pending[i].1.len()));
            texts.extend(pending[i].1.iter().cloned());
            i += 1;
        }
        let vecs = embedder.embed(&texts)?;
        if vecs.len() != texts.len() {
            bail!(
                "embedder returned {} vectors for {} texts",
                vecs.len(),
                texts.len()
            );
        }
        let mut off = 0;
        for (idx, n) in owners {
            store.put_embeddings(&pending[idx].0, embedder.model(), &vecs[off..off + n])?;
            off += n;
            nodes += 1;
            chunks += n;
        }
        progress(nodes, total);
    }
    Ok((nodes, chunks))
}

/// Defaults are calibrated for BGE-small, whose cosines bunch high (unrelated
/// legal provisions score around 0.72). Recalibrate for other models.
#[derive(Debug, Clone, Copy, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct SimilarityConfig {
    /// Neighbours kept per node overall.
    pub top_k: usize,
    /// Minimum cosine for the overall top-k.
    pub threshold: f32,
    /// Extra neighbours per *other* corpus kind (cases, guidance, ...).
    pub per_kind: usize,
    /// Minimum cosine for the per-kind quota.
    pub per_kind_threshold: f32,
}

impl Default for SimilarityConfig {
    fn default() -> Self {
        Self {
            top_k: 8,
            threshold: 0.82,
            per_kind: 3,
            per_kind_threshold: 0.76,
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SimilarityReport {
    pub nodes: usize,
    pub edges: usize,
    pub cross_corpus: usize,
}

/// Build `similar` edges from stored vectors and replace the existing ones.
///
/// Excludes a node's own ancestors and descendants (the tree already links them).
/// Edges are stored once per direction found; each carries its cosine as weight.
pub fn build_similarity(
    store: &Store,
    model: &str,
    cfg: SimilarityConfig,
) -> Result<SimilarityReport> {
    // One vector per node: normalised mean of its chunks.
    let mut acc: Vec<(String, Vec<f32>, u32)> = Vec::new();
    for (id, _, v) in store.embeddings(model)? {
        match acc.last_mut() {
            Some((last, sum, n)) if *last == id => {
                sum.iter_mut().zip(&v).for_each(|(a, b)| *a += b);
                *n += 1;
            }
            _ => acc.push((id, v, 1)),
        }
    }
    // In corpora with a summary, the root (summary vector) represents the
    // document in the graph; its paragraphs stay out of the similarity graph.
    let summarised: std::collections::HashSet<String> = store.summarised_corpora()?;
    acc.retain(|(id, _, _)| {
        let c = meowlaw_store::corpus_of(id);
        !summarised.contains(c) || id == c
    });
    let ids: Vec<String> = acc.iter().map(|(id, _, _)| id.clone()).collect();
    let vecs: Vec<Vec<f32>> = acc
        .into_iter()
        .map(|(_, mut v, _)| {
            normalise(&mut v);
            v
        })
        .collect();
    let mut kind_of: HashMap<String, CorpusKind> = HashMap::new();
    for c in store.corpora()? {
        kind_of.insert(c.id, c.kind);
    }
    let corpus_of = |id: &str| meowlaw_store::corpus_of(id).to_string();
    let kinds: Vec<Option<CorpusKind>> = ids
        .iter()
        .map(|id| kind_of.get(&corpus_of(id)).copied())
        .collect();
    // Tree relation: node A is an ancestor of B if B's id path passes through A.
    let parents: HashMap<String, Option<String>> = store
        .documents()?
        .into_iter()
        .flat_map(|d| d.nodes.into_iter().map(|n| (n.id, n.parent)))
        .collect();
    let ancestors = |id: &str| -> Vec<String> {
        let mut out = Vec::new();
        let mut cur = parents.get(id).cloned().flatten();
        while let Some(p) = cur {
            cur = parents.get(&p).cloned().flatten();
            out.push(p);
            if out.len() > 64 {
                break;
            }
        }
        out
    };
    let anc: Vec<Vec<String>> = ids.iter().map(|id| ancestors(id)).collect();

    let edges: Vec<Vec<Edge>> = (0..ids.len())
        .into_par_iter()
        .map(|i| {
            let mut scored: Vec<(usize, f32)> = (0..ids.len())
                .filter(|&j| j != i)
                .map(|j| (j, dot(&vecs[i], &vecs[j])))
                .filter(|(_, s)| *s >= cfg.per_kind_threshold.min(cfg.threshold))
                .filter(|(j, _)| !anc[i].contains(&ids[*j]) && !anc[*j].contains(&ids[i]))
                .collect();
            scored.sort_by(|a, b| b.1.total_cmp(&a.1));
            let mut keep: Vec<(usize, f32)> = scored
                .iter()
                .filter(|(_, s)| *s >= cfg.threshold)
                .take(cfg.top_k)
                .copied()
                .collect();
            // Per-kind quota for kinds other than this node's own.
            let mut per: HashMap<Option<CorpusKind>, usize> = HashMap::new();
            for (j, s) in &scored {
                if kinds[*j] == kinds[i] || *s < cfg.per_kind_threshold {
                    continue;
                }
                let n = per.entry(kinds[*j]).or_insert(0);
                if *n < cfg.per_kind {
                    *n += 1;
                    if !keep.iter().any(|(k, _)| k == j) {
                        keep.push((*j, *s));
                    }
                }
            }
            keep.into_iter()
                .map(|(j, s)| Edge {
                    source: ids[i].clone(),
                    target: ids[j].clone(),
                    kind: EdgeKind::Similar,
                    method: Method::Model,
                    evidence: Some(format!("cosine {s:.3}")),
                    weight: s,
                })
                .collect()
        })
        .collect();
    let edges: Vec<Edge> = edges.into_iter().flatten().collect();
    let cross_corpus = edges
        .iter()
        .filter(|e| corpus_of(&e.source) != corpus_of(&e.target))
        .count();
    store.replace_similarity_edges(&edges)?;
    Ok(SimilarityReport {
        nodes: ids.len(),
        edges: edges.len(),
        cross_corpus,
    })
}

/// Local embeddings with no Python and no network after download: an ONNX
/// sentence-embedding model run by `tract` (pure Rust). Expects a directory
/// with `model.onnx` and `tokenizer.json` (e.g. BAAI/bge-small-en-v1.5).
/// Uses CLS pooling and L2 normalisation, as BGE models are trained for.
#[cfg(feature = "local-embed")]
pub mod local {
    use super::*;
    use std::path::Path;
    use tract_onnx::prelude::*;

    type Plan = std::sync::Arc<TypedSimplePlan>;

    /// Token window of the query plan.
    const QUERY_SEQ: usize = 64;

    /// How token vectors become one text vector.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Pooling {
        /// First token (BGE models).
        Cls,
        /// Attention-weighted mean of all tokens (E5, sentence-transformers).
        Mean,
    }

    impl Pooling {
        pub fn parse(s: &str) -> Result<Self> {
            match s {
                "cls" => Ok(Self::Cls),
                "mean" => Ok(Self::Mean),
                o => bail!("pooling must be `cls` or `mean`, not `{o}`"),
            }
        }
    }

    /// Model-specific settings; `models::PRESETS` has known-good values.
    #[derive(Debug, Clone)]
    pub struct LocalOptions {
        pub batch: usize,
        pub seq: usize,
        pub pooling: Pooling,
        /// Prepended to search queries ("query: " for E5).
        pub query_prefix: String,
        /// Prepended to indexed text ("passage: " for E5).
        pub passage_prefix: String,
    }

    /// Local embeddings with no Python and no network after download: an ONNX
    /// sentence-embedding model run by `tract` (pure Rust). The directory holds
    /// `model.onnx` and `tokenizer.json`.
    pub struct LocalEmbedder {
        name: String,
        tokenizer: tokenizers::Tokenizer,
        plan: Plan,
        /// [1, QUERY_SEQ] plan for short single texts (queries).
        single: Plan,
        /// 3 for BERT-style models (ids, mask, token types), 2 without token types.
        inputs: usize,
        opts: LocalOptions,
    }

    fn compile(dir: &Path, batch: usize, seq: usize) -> Result<(Plan, usize)> {
        let shape = [batch, seq];
        let mut m = tract_onnx::onnx().model_for_path(dir.join("model.onnx"))?;
        let inputs = m.inputs.len().min(3);
        for i in 0..inputs {
            m = m.with_input_fact(i, InferenceFact::dt_shape(i64::datum_type(), shape))?;
        }
        Ok((m.into_optimized()?.into_runnable()?, inputs))
    }

    impl LocalEmbedder {
        pub fn load(dir: &Path, name: &str, opts: LocalOptions) -> Result<Self> {
            let mut tokenizer = tokenizers::Tokenizer::from_file(dir.join("tokenizer.json"))
                .map_err(|e| anyhow::anyhow!("loading tokenizer: {e}"))?;
            tokenizer
                .with_truncation(Some(tokenizers::TruncationParams {
                    max_length: opts.seq,
                    ..Default::default()
                }))
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            tokenizer.with_padding(Some(tokenizers::PaddingParams {
                strategy: tokenizers::PaddingStrategy::Fixed(opts.seq),
                ..Default::default()
            }));
            let (plan, inputs) = compile(dir, opts.batch, opts.seq)?;
            let (single, _) = compile(dir, 1, opts.seq.min(QUERY_SEQ))?;
            Ok(Self {
                name: name.to_string(),
                tokenizer,
                plan,
                single,
                inputs,
                opts,
            })
        }

        fn run_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
            let enc = self
                .tokenizer
                .encode_batch(texts.to_vec(), true)
                .map_err(|e| anyhow::anyhow!("tokenising: {e}"))?;
            let real = |e: &tokenizers::Encoding| {
                e.get_attention_mask().iter().filter(|m| **m == 1).count()
            };
            let short = self.opts.seq.min(QUERY_SEQ);
            let (plan, b, l) = if texts.len() == 1 && real(&enc[0]) <= short {
                (&self.single, 1, short)
            } else {
                (&self.plan, self.opts.batch, self.opts.seq)
            };
            let mut ids = vec![0i64; b * l];
            let mut mask = vec![0i64; b * l];
            let types = vec![0i64; b * l];
            for (r, e) in enc.iter().enumerate() {
                for (c, (&t, &m)) in e
                    .get_ids()
                    .iter()
                    .zip(e.get_attention_mask())
                    .enumerate()
                    .take(l)
                {
                    ids[r * l + c] = t as i64;
                    mask[r * l + c] = m as i64;
                }
            }
            let t = |v: &[i64]| -> Result<TValue> {
                Ok(tract_ndarray::Array2::from_shape_vec((b, l), v.to_vec())?
                    .into_tensor()
                    .into())
            };
            let mut input = tvec!(t(&ids)?, t(&mask)?);
            if self.inputs == 3 {
                input.push(t(&types)?);
            }
            let out = plan.run(input)?;
            let h = out[0].to_plain_array_view::<f32>()?; // [batch, seq, dim]
            let mut res = Vec::with_capacity(texts.len());
            for r in 0..texts.len() {
                let mut v: Vec<f32> = match self.opts.pooling {
                    Pooling::Cls => h.slice(tract_ndarray::s![r, 0, ..]).iter().copied().collect(),
                    Pooling::Mean => {
                        let dim = h.shape()[2];
                        let mut acc = vec![0f32; dim];
                        let mut n = 0f32;
                        for c in 0..l {
                            if mask[r * l + c] == 1 {
                                for (a, x) in acc.iter_mut().zip(h.slice(tract_ndarray::s![r, c, ..])) {
                                    *a += *x;
                                }
                                n += 1.0;
                            }
                        }
                        acc.iter_mut().for_each(|a| *a /= n.max(1.0));
                        acc
                    }
                };
                normalise(&mut v);
                res.push(v);
            }
            Ok(res)
        }

        fn run_all(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
            // tract runs one batch per thread; spread batches over the cores.
            let parts: Vec<Vec<Vec<f32>>> = texts
                .par_chunks(self.opts.batch)
                .map(|c| self.run_batch(c))
                .collect::<Result<_>>()?;
            Ok(parts.into_iter().flatten().collect())
        }
    }

    impl Embedder for LocalEmbedder {
        fn model(&self) -> &str {
            &self.name
        }

        fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
            if self.opts.passage_prefix.is_empty() {
                return self.run_all(texts);
            }
            let prefixed: Vec<String> = texts
                .iter()
                .map(|t| format!("{}{t}", self.opts.passage_prefix))
                .collect();
            self.run_all(&prefixed)
        }

        fn embed_query(&self, query: &str) -> Result<Vec<f32>> {
            self.run_batch(&[format!("{}{query}", self.opts.query_prefix)])?
                .pop()
                .context("no vector")
        }
    }
}

/// Embedding models `meowlaw models pull` can fetch. Every entry was checked
/// against its model card: licence, ONNX export present, pooling and prefixes.
pub mod models {
    use anyhow::{Context, Result, bail};
    use std::path::{Path, PathBuf};

    pub struct Preset {
        pub name: &'static str,
        pub repo: &'static str,
        pub onnx: &'static str,
        pub licence: &'static str,
        pub languages: &'static str,
        pub pooling: &'static str,
        pub query_prefix: &'static str,
        pub passage_prefix: &'static str,
        pub note: &'static str,
    }

    pub const PRESETS: &[Preset] = &[
        Preset {
            name: "bge-small-en-v1.5",
            repo: "BAAI/bge-small-en-v1.5",
            onnx: "onnx/model.onnx",
            licence: "MIT",
            languages: "English",
            pooling: "cls",
            query_prefix: "",
            passage_prefix: "",
            note: "Default. 384 dimensions, about 130 MB.",
        },
        Preset {
            name: "multilingual-e5-small",
            repo: "intfloat/multilingual-e5-small",
            onnx: "onnx/model.onnx",
            licence: "MIT",
            languages: "about 100 languages",
            pooling: "mean",
            query_prefix: "query: ",
            passage_prefix: "passage: ",
            note: "Multilingual, about 470 MB. Needs the query/passage prefixes (set by `pull`).",
        },
        Preset {
            name: "paraphrase-multilingual-minilm-l12-v2",
            repo: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            onnx: "onnx/model.onnx",
            licence: "Apache-2.0",
            languages: "50+ languages",
            pooling: "mean",
            query_prefix: "",
            passage_prefix: "",
            note: "Multilingual, no prefixes.",
        },
    ];

    pub fn preset(name: &str) -> Option<&'static Preset> {
        PRESETS.iter().find(|p| p.name.eq_ignore_ascii_case(name))
    }

    /// Download a model's ONNX file and tokenizer into `<dir>/<name>/`.
    /// Skips files already present. Returns the model directory.
    pub fn pull(dir: &Path, p: &Preset, mut progress: impl FnMut(&str, u64)) -> Result<PathBuf> {
        let target = dir.join(p.name);
        std::fs::create_dir_all(&target)?;
        let agent = ureq::Agent::new_with_defaults();
        for (remote, local) in [(p.onnx, "model.onnx"), ("tokenizer.json", "tokenizer.json")] {
            let dest = target.join(local);
            if dest.exists() && std::fs::metadata(&dest)?.len() > 0 {
                progress(local, 0);
                continue;
            }
            let url = format!("https://huggingface.co/{}/resolve/main/{remote}", p.repo);
            let mut resp = agent.get(&url).call().with_context(|| format!("GET {url}"))?;
            if resp.status() != 200 {
                bail!("GET {url}: HTTP {}", resp.status());
            }
            let tmp = target.join(format!("{local}.part"));
            let mut f = std::fs::File::create(&tmp)?;
            let n = std::io::copy(&mut resp.body_mut().as_reader(), &mut f)?;
            std::fs::rename(&tmp, &dest)?;
            progress(local, n);
        }
        Ok(target)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_respect_limit() {
        let text = "One. Two three.\nFour; five: six.".repeat(200);
        let c = chunk_text(&text, 100);
        assert!(c.iter().all(|x| x.chars().count() <= 100));
        let squash = |s: &str| s.chars().filter(|c| !c.is_whitespace()).collect::<String>();
        assert_eq!(squash(&c.concat()), squash(&text));
    }
}
