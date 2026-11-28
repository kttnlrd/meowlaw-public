// Spike 001: does a Rust ANN index (usearch) beat the current Python brute-force
// vector search (numpy matmul + full argsort over 306k x 1536)?
//
// Modes:
//   ./usearch-bench <dir>          build both indexes; report build/size/latency/recall
//   ./usearch-bench <dir> eval     load existing indexes; sweep expansion_search -> recall/latency
//
// Inputs (dumped from meowlaw/data/*.npy by prepare.py):
//   matrix.f32  - N x 1536 float32, row-major
//   ids.i64     - N int64 embedding ids (parallel to rows)
// N is derived from matrix.f32's size, as indexbuild does — never hardcoded (it
// was 306_452 here and the assert panicked once the corpus grew to 384,869).
use std::collections::HashSet;
use std::fs;
use std::time::Instant;
use usearch::{new_index, IndexOptions, MetricKind, ScalarKind};

const DIMS: usize = 1536;
const K: usize = 50;
const NQ: usize = 200; // latency queries (build mode)
const NQ_BF: usize = 20; // brute-force / recall queries (build mode)
const NQ_EVAL: usize = 60; // recall queries (eval mode)
const RERANK: usize = 4; // eval: oversample factor for the exact re-rank column

fn read_f32(path: &str, n: usize) -> Vec<f32> {
    let bytes = fs::read(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    assert_eq!(bytes.len(), n * 4, "size mismatch for {path}");
    bytes
        .chunks_exact(4)
        .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .collect()
}

fn read_i64(path: &str, n: usize) -> Vec<i64> {
    let bytes = fs::read(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    assert_eq!(bytes.len(), n * 8, "size mismatch for {path}");
    bytes
        .chunks_exact(8)
        .map(|b| i64::from_le_bytes(b.try_into().unwrap()))
        .collect()
}

/// Exact top-K by inner product. Partial selection — no full sort.
fn topk_dot(matrix: &[f32], q: &[f32], k: usize) -> HashSet<u64> {
    let n = matrix.len() / DIMS;
    let mut scores: Vec<f32> = Vec::with_capacity(n);
    for i in 0..n {
        let v = &matrix[i * DIMS..(i + 1) * DIMS];
        let mut s = 0f32;
        for j in 0..DIMS {
            s += v[j] * q[j];
        }
        scores.push(s);
    }
    let mut idx: Vec<usize> = (0..n).collect();
    idx.select_nth_unstable_by(k, |&a, &b| scores[b].partial_cmp(&scores[a]).unwrap());
    idx[..k].iter().map(|&i| i as u64).collect()
}

/// Exact re-rank of ANN candidates: the same dot product as topk_dot and as
/// serve.rs slice_search, over the candidate rows only.
fn rerank(matrix: &[f32], q: &[f32], keys: &[u64], k: usize) -> HashSet<u64> {
    let mut scored: Vec<(f32, u64)> = keys
        .iter()
        .map(|&key| {
            let v = &matrix[key as usize * DIMS..(key as usize + 1) * DIMS];
            (v.iter().zip(q).map(|(a, b)| a * b).sum::<f32>(), key)
        })
        .collect();
    scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
    scored.into_iter().take(k).map(|(_, key)| key).collect()
}

fn opts_for(quant: ScalarKind) -> IndexOptions {
    IndexOptions {
        dimensions: DIMS,
        metric: MetricKind::Cos,
        quantization: quant,
        connectivity: 0,     // auto
        expansion_add: 0,    // auto
        expansion_search: 0, // auto
        multi: false,
    }
}

/// Row count from matrix.f32's size (same derivation as indexbuild).
fn rows(dir: &str) -> usize {
    let path = format!("{dir}/matrix.f32");
    let len = fs::metadata(&path).unwrap_or_else(|e| panic!("stat {path}: {e}")).len() as usize;
    assert_eq!(len % (DIMS * 4), 0, "{path} is not a whole number of {DIMS}-dim f32 rows ({len} bytes)");
    len / (DIMS * 4)
}

fn load_matrix(dir: &str) -> (Vec<f32>, usize) {
    let t = Instant::now();
    let n = rows(dir);
    let matrix = read_f32(&format!("{dir}/matrix.f32"), n * DIMS);
    let _ids = read_i64(&format!("{dir}/ids.i64"), n);
    println!(
        "loaded matrix + ids in {:.2}s ({:.2} GB)",
        t.elapsed().as_secs_f64(),
        (matrix.len() * 4) as f64 / 1e9
    );
    (matrix, n)
}

fn build_bench(dir: &str) {
    let (matrix, n) = load_matrix(dir);
    let qidx_lat: Vec<usize> = (0..NQ).map(|i| (i * 1373 + 7) % n).collect();
    let qidx_bf: Vec<usize> = (0..NQ_BF).map(|i| (i * 1373 + 7) % n).collect();

    for (label, quant) in [("f32", ScalarKind::F32), ("i8", ScalarKind::I8)] {
        let index = new_index(&opts_for(quant)).unwrap();
        index.reserve(n).unwrap();

        let t = Instant::now();
        for i in 0..n {
            index
                .add(i as u64, &matrix[i * DIMS..(i + 1) * DIMS])
                .unwrap();
        }
        let build = t.elapsed();
        let path = format!("{dir}/idx_{label}.usearch");
        index.save(&path).unwrap();
        let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        println!(
            "\n[{label}] build {:.1}s | index {:.0} MB | connectivity {} | size {}",
            build.as_secs_f64(),
            size as f64 / 1e6,
            index.connectivity(),
            index.size()
        );

        let _ = index.search(&matrix[0..DIMS], K).unwrap();
        let t = Instant::now();
        for &qi in &qidx_lat {
            let _ = index
                .search(&matrix[qi * DIMS..(qi + 1) * DIMS], K)
                .unwrap();
        }
        let per = t.elapsed().as_secs_f64() * 1000.0 / NQ as f64;
        println!(
            "[{label}] ANN top-{K}: {per:.3} ms/query warm ({NQ} queries, {:.0} qps)",
            1000.0 / per
        );

        let mut recall = 0f64;
        let mut bf_total = 0f64;
        for &qi in &qidx_bf {
            let q = &matrix[qi * DIMS..(qi + 1) * DIMS];
            let ann: HashSet<u64> = index.search(q, K).unwrap().keys.into_iter().collect();
            let t = Instant::now();
            let exact = topk_dot(&matrix, q, K);
            bf_total += t.elapsed().as_secs_f64();
            recall += ann.intersection(&exact).count() as f64 / K as f64;
        }
        println!("[{label}] recall@{K} = {:.3}", recall / NQ_BF as f64);
        if label == "f32" {
            println!(
                "[brute-force rust] top-{K}: {:.1} ms/query (numpy baseline: 95ms matmul + 34ms argsort)",
                bf_total * 1000.0 / NQ_BF as f64
            );
        }

        let v = new_index(&opts_for(quant)).unwrap();
        let t = Instant::now();
        v.view(&path).unwrap();
        let _ = v.search(&matrix[0..DIMS], K).unwrap();
        println!(
            "[{label}] mmap view + first search: {:.1} ms",
            t.elapsed().as_secs_f64() * 1000.0
        );
    }
    println!("\n(timings exclude the OpenAI query-embed round trip, identical for both stacks)");
}

fn eval(dir: &str) {
    let (matrix, n) = load_matrix(dir);
    let qidx: Vec<usize> = (0..NQ_EVAL).map(|i| (i * 7919 + 13) % n).collect();

    println!("\ncomputing exact top-{K} ground truth for {NQ_EVAL} queries ...");
    let t = Instant::now();
    let truth: Vec<HashSet<u64>> = qidx
        .iter()
        .map(|&qi| topk_dot(&matrix, &matrix[qi * DIMS..(qi + 1) * DIMS], K))
        .collect();
    println!("ground truth in {:.1}s", t.elapsed().as_secs_f64());

    for (label, quant) in [("f32", ScalarKind::F32), ("i8", ScalarKind::I8)] {
        let path = format!("{dir}/idx_{label}.usearch");
        if !std::path::Path::new(&path).exists() {
            println!("\n[{label}] missing {path}, skipping");
            continue;
        }
        let index = new_index(&opts_for(quant)).unwrap();
        index.load(&path).unwrap();
        println!(
            "\n[{label}] loaded (auto expansion_search = {})",
            index.expansion_search()
        );
        println!("  es | recall@{K} | ms/query | rerank {RERANK}x recall@{K} | ms/query");
        for es in [16usize, 32, 64, 128, 256, 512] {
            index.change_expansion_search(es);
            let t = Instant::now();
            let mut recall = 0f64;
            for (qi, tr) in qidx.iter().zip(truth.iter()) {
                let ann: HashSet<u64> = index
                    .search(&matrix[qi * DIMS..(qi + 1) * DIMS], K)
                    .unwrap()
                    .keys
                    .into_iter()
                    .collect();
                recall += ann.intersection(tr).count() as f64 / K as f64;
            }
            let per = t.elapsed().as_secs_f64() * 1000.0 / NQ_EVAL as f64;
            // oversample RERANK*K from the graph, re-score exactly, keep K
            let t = Instant::now();
            let mut recall_rr = 0f64;
            for (qi, tr) in qidx.iter().zip(truth.iter()) {
                let q = &matrix[qi * DIMS..(qi + 1) * DIMS];
                let keys = index.search(q, RERANK * K).unwrap().keys;
                recall_rr += rerank(&matrix, q, &keys, K).intersection(tr).count() as f64 / K as f64;
            }
            let per_rr = t.elapsed().as_secs_f64() * 1000.0 / NQ_EVAL as f64;
            println!(
                "{es:>4} |     {:.3} |    {per:.3} |              {:.3} |    {per_rr:.3}",
                recall / NQ_EVAL as f64,
                recall_rr / NQ_EVAL as f64
            );
        }
    }
}

fn main() {
    let dir = std::env::args().nth(1).unwrap_or_else(|| ".".into());
    let mode = std::env::args().nth(2).unwrap_or_else(|| "build".into());
    if mode == "eval" {
        eval(&dir);
    } else {
        build_bench(&dir);
    }
}
