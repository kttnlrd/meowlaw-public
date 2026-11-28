// Build the usearch ANN index for searchd from matrix.f32.
//
//   indexbuild <dir> [f32|i8|both]
//
// matrix.f32 is row-major float32, unit-normalised, so cosine == inner product.
// N is derived from the file size and never hardcoded, so this stays correct as
// the corpus grows (the spike's bench had 306,452 baked in and silently broke
// the moment the corpus grew to 384,869).
use std::time::Instant;
use usearch::{new_index, IndexOptions, MetricKind, ScalarKind};

const DIMS: usize = 1536;

fn main() {
    let dir = std::env::args().nth(1).unwrap_or_else(|| ".".into());
    let which = std::env::args().nth(2).unwrap_or_else(|| "both".into());

    let path = format!("{dir}/matrix.f32");
    let file = std::fs::File::open(&path).unwrap_or_else(|e| panic!("open {path}: {e}"));
    let mmap = unsafe { memmap2::Mmap::map(&file).expect("mmap matrix.f32") };
    assert_eq!(
        mmap.len() % (DIMS * 4),
        0,
        "matrix.f32 is not a whole number of {DIMS}-dim f32 rows ({} bytes)",
        mmap.len()
    );
    let n = mmap.len() / (DIMS * 4);
    println!("matrix: {n} rows x {DIMS} f32 ({:.2} GB)", mmap.len() as f64 / 1e9);

    let mut quants: Vec<(&str, ScalarKind)> = Vec::new();
    if which == "f32" || which == "both" {
        quants.push(("f32", ScalarKind::F32));
    }
    if which == "i8" || which == "both" {
        quants.push(("i8", ScalarKind::I8));
    }
    assert!(!quants.is_empty(), "usage: indexbuild <dir> [f32|i8|both]");

    for (label, quant) in quants {
        let opts = IndexOptions {
            dimensions: DIMS,
            metric: MetricKind::Cos,
            quantization: quant,
            connectivity: 0,     // auto
            expansion_add: 0,    // auto
            expansion_search: 0, // auto
            multi: false,
        };
        let index = new_index(&opts).unwrap();
        index.reserve(n).unwrap();

        let t = Instant::now();
        for i in 0..n {
            let off = i * DIMS * 4;
            // mmap base is page-aligned and off is a multiple of 4, so the f32
            // view is aligned; matrix.f32 is a fixed read-only dump.
            let v = unsafe { std::slice::from_raw_parts(mmap.as_ptr().add(off) as *const f32, DIMS) };
            index.add(i as u64, v).unwrap();
            if i > 0 && i % 50_000 == 0 {
                println!("  [{label}] {i}/{n} ({:.0}s)", t.elapsed().as_secs_f64());
            }
        }
        let out = format!("{dir}/idx_{label}.usearch");
        index.save(&out).unwrap();
        let size = std::fs::metadata(&out).map(|m| m.len()).unwrap_or(0);
        println!(
            "[{label}] built in {:.0}s | {:.0} MB | connectivity {} | saved {out}",
            t.elapsed().as_secs_f64(),
            size as f64 / 1e6,
            index.connectivity()
        );
    }
}
