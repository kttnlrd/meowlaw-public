//! Contract test: every response must equal, byte for byte, what the Python app serves.
//!
//! The expected bodies in tests/golden were written by tests/gen_golden.py from the real
//! Python route functions. They are cut from the corpus, so the test needs the data folder:
//! LAWKITTY_DATA, or ../data beside this crate. Without it the test is skipped.
//! LAWKITTY_GOLDEN points the test at another set (see gen_golden.py --sweep).

use axum::body::Body;
use axum::http::Request;
use http_body_util::BodyExt;
use lawkitty_local::{router, App, Config};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;
use tower::ServiceExt;

fn first_difference(got: &[u8], want: &[u8]) -> String {
    let at = got.iter().zip(want).position(|(a, b)| a != b).unwrap_or(got.len().min(want.len()));
    let around = |b: &[u8]| String::from_utf8_lossy(&b[at.saturating_sub(60)..(at + 60).min(b.len())]).into_owned();
    format!("differs at byte {at} (got {} bytes, want {})\n   got: {:?}\n  want: {:?}", got.len(), want.len(), around(got), around(want))
}

#[tokio::test]
async fn responses_match_the_python_app() {
    let crate_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let data = std::env::var_os("LAWKITTY_DATA").map(PathBuf::from).unwrap_or_else(|| crate_dir.join("../data"));
    if !data.join("itaa-1997/tree.json").is_file() {
        eprintln!("skipped: no corpus at {} (set LAWKITTY_DATA)", data.display());
        return;
    }
    let golden = std::env::var_os("LAWKITTY_GOLDEN").map(PathBuf::from).unwrap_or_else(|| crate_dir.join("tests/golden"));
    let manifest: Vec<Value> = serde_json::from_slice(&std::fs::read(golden.join("manifest.json")).expect("manifest.json")).expect("manifest");
    let app = router(Arc::new(App::new(Config {
        data,
        dist: crate_dir.join("../frontend/dist"),
        state: std::env::temp_dir().join("lawkitty-local-contract-state.json"),
        search_db: None,
    })));

    let mut failures = Vec::new();
    for case in &manifest {
        let path = case["path"].as_str().expect("path");
        let want = std::fs::read(golden.join(case["file"].as_str().expect("file"))).expect("golden file");
        let response = app.clone().oneshot(Request::builder().uri(path).body(Body::empty()).unwrap()).await.unwrap();
        let status = response.status().as_u16();
        let length = response.headers().get("x-uncompressed-length").and_then(|v| v.to_str().ok()).map(String::from);
        let got = response.into_body().collect().await.unwrap().to_bytes();
        if u64::from(status) != case["status"].as_u64().unwrap_or(200) {
            failures.push(format!("{path}: status {status}, want {}", case["status"]));
        } else if got[..] != want[..] {
            failures.push(format!("{path}: {}", first_difference(&got, &want)));
        } else if case["tree"] == true && length != Some(want.len().to_string()) {
            failures.push(format!("{path}: X-Uncompressed-Length {length:?}, want {}", want.len()));
        }
    }
    assert!(failures.is_empty(), "{} of {} responses differ:\n{}", failures.len(), manifest.len(), failures.join("\n"));
    eprintln!("{} responses identical to the Python app", manifest.len());
}
