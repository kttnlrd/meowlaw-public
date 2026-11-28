// Build the BM25 lexical index (tantivy) over the corpus, for hybrid retrieval.
//
//   cargo run --release --bin lexbuild -- <dir>
//
// Reads meta.json (same columnar dump the ANN side uses) and writes tantivy_idx/.
// The `row` field ties a BM25 hit back to its row index in the ANN arrays.
// `type` and `act` are raw, lowercased single tokens so the slicers can filter
// on them exactly.
#[allow(dead_code)]
#[derive(serde::Deserialize)]
struct Meta {
    t: Vec<String>,
    a: Vec<String>,
    s: Vec<String>,
    h: Vec<String>,
    x: Vec<String>,
    series: Vec<String>,
    // slicer dimensions; absent from an older meta.json -> indexed as ""
    #[serde(default)]
    outcome: Vec<String>,
    #[serde(default)]
    year: Vec<String>,
    #[serde(default)]
    court: Vec<String>,
}

fn cell(v: &[String], i: usize) -> String {
    v.get(i).map(|s| s.to_lowercase()).unwrap_or_default()
}

use tantivy::schema::{IndexRecordOption, Schema, TextFieldIndexing, TextOptions, INDEXED, STORED, TEXT};
use tantivy::{doc, Index};

fn main() {
    let dir = std::env::args().nth(1).unwrap_or_else(|| ".".into());
    let meta: Meta = serde_json::from_slice(&std::fs::read(format!("{dir}/meta.json")).expect("meta.json")).expect("parse meta");
    let idx_path = format!("{dir}/tantivy_idx");
    if std::path::Path::new(&idx_path).exists() {
        std::fs::remove_dir_all(&idx_path).unwrap();
    }
    // tantivy's create_in_dir requires the directory to already exist
    std::fs::create_dir_all(&idx_path).unwrap();

    let mut b = Schema::builder();
    let f_row = b.add_u64_field("row", INDEXED | STORED | tantivy::schema::FAST);
    // identifiers and slicer keys are exact values, not prose: one raw token each
    let raw_opts = TextOptions::default()
        .set_indexing_options(
            TextFieldIndexing::default().set_tokenizer("raw").set_index_option(IndexRecordOption::Basic),
        )
        .set_stored();
    let f_sec = b.add_text_field("section", raw_opts.clone());
    let f_act = b.add_text_field("act", raw_opts.clone());
    let f_type = b.add_text_field("type", raw_opts.clone());
    // public-ruling series ("tr", "ps la"), empty for every other row
    let f_series = b.add_text_field("series", raw_opts.clone());
    let f_outcome = b.add_text_field("outcome", raw_opts.clone());
    let f_year = b.add_text_field("year", raw_opts.clone());
    let f_court = b.add_text_field("court", raw_opts);
    let f_title = b.add_text_field("title", TEXT | STORED);
    let f_text = b.add_text_field("text", TEXT);
    let schema = b.build();

    let index = Index::create_in_dir(&idx_path, schema).expect("create index");
    let mut w = index.writer(512_000_000).expect("writer");

    let n = meta.a.len();
    for i in 0..n {
        w.add_document(doc!(
            f_row => i as u64,
            f_sec => meta.s[i].clone(),
            // lowercased so slicer terms match whatever the raw tokenizer does with case
            f_act => meta.a[i].to_lowercase(),
            f_type => meta.t[i].to_lowercase(),
            f_series => meta.series[i].to_lowercase(),
            f_outcome => cell(&meta.outcome, i),
            f_year => cell(&meta.year, i),
            f_court => cell(&meta.court, i),
            f_title => meta.h[i].clone(),
            f_text => meta.x[i].clone(),
        ))
        .expect("add");
        if i > 0 && i % 100_000 == 0 {
            println!("  {i}/{n}");
        }
    }
    w.commit().expect("commit");
    println!("lexbuild done: {n} docs -> {idx_path}");
}
