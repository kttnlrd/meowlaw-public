use lawkitty_local::{router, App, Config};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

const USAGE: &str = "lawkitty-local: local, read-only LawKitty server

Usage: lawkitty-local [--data <dir>] [--port <n>] [--dist <dir>] [--state <file>] [--search-db <file>]

  --data <dir>        Data folder (or LAWKITTY_DATA). Default: ./lawkitty-data
  --port <n>          Port on 127.0.0.1 (or LAWKITTY_PORT). Default: 8790
  --dist <dir>        Built web app, frontend/dist (or LAWKITTY_DIST)
  --state <file>      Pinned sources and display preferences (or LAWKITTY_STATE)
  --search-db <file>  SQLite FTS index. Default: search_index.db in or beside the data folder";

fn fail(message: &str) -> ! {
    eprintln!("lawkitty-local: {message}\n\n{USAGE}");
    std::process::exit(2);
}

/// frontend/dist beside the working directory, or beside the checkout the binary was built in
/// (local-app/target/release/lawkitty-local), or a `dist` folder shipped next to the binary.
fn default_dist() -> PathBuf {
    let exe = std::env::current_exe().ok();
    let exe_dir = exe.as_deref().and_then(|p| p.parent());
    let candidates = [
        Some(PathBuf::from("frontend/dist")),
        exe_dir.and_then(|d| d.ancestors().nth(3)).map(|repo| repo.join("frontend/dist")),
        exe_dir.map(|d| d.join("dist")),
    ];
    candidates.into_iter().flatten().find(|p| p.join("index.html").is_file()).unwrap_or_else(|| PathBuf::from("frontend/dist"))
}

fn default_state() -> PathBuf {
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    let config = env("XDG_CONFIG_HOME").or_else(|| env("APPDATA")).or_else(|| env("HOME").map(|h| h.join(".config")));
    match config {
        Some(dir) => dir.join("lawkitty-local").join("state.json"),
        None => PathBuf::from("lawkitty-local-state.json"),
    }
}

fn main() {
    let started = Instant::now();
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    let mut data = env("LAWKITTY_DATA");
    let mut dist = env("LAWKITTY_DIST");
    let mut state = env("LAWKITTY_STATE");
    let mut search_db = None;
    let mut port: u16 = match std::env::var("LAWKITTY_PORT") {
        Ok(p) => p.parse().unwrap_or_else(|_| fail("LAWKITTY_PORT is not a port number")),
        Err(_) => 8790,
    };
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = |name: &str| args.next().unwrap_or_else(|| fail(&format!("{name} needs a value")));
        match arg.as_str() {
            "--data" => data = Some(value("--data").into()),
            "--dist" => dist = Some(value("--dist").into()),
            "--state" => state = Some(value("--state").into()),
            "--search-db" => search_db = Some(value("--search-db").into()),
            "--port" => port = value("--port").parse().unwrap_or_else(|_| fail("--port is not a port number")),
            "-h" | "--help" => {
                println!("{USAGE}");
                return;
            }
            other => fail(&format!("unknown argument {other}")),
        }
    }
    let data = data.unwrap_or_else(|| PathBuf::from("lawkitty-data"));
    if !data.is_dir() {
        fail(&format!("data folder {} not found (set LAWKITTY_DATA or pass --data)", data.display()));
    }

    let app = Arc::new(App::new(Config {
        data: data.clone(),
        dist: dist.unwrap_or_else(default_dist),
        state: state.unwrap_or_else(default_state),
        search_db,
    }));
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_io()
        .build()
        .unwrap_or_else(|e| fail(&format!("cannot start: {e}")));
    runtime.block_on(async {
        // Loopback only: this server has no accounts and must never be reachable off the machine.
        let addr = SocketAddr::from(([127, 0, 0, 1], port));
        let listener = tokio::net::TcpListener::bind(addr).await.unwrap_or_else(|e| fail(&format!("cannot bind {addr}: {e}")));
        println!(
            "lawkitty-local: http://{addr}/app/  ({} sources from {}, ready in {:.1} ms)",
            app.catalog.sources.len(),
            data.display(),
            started.elapsed().as_secs_f64() * 1000.0
        );
        match app.search.path() {
            Some(p) => println!("lawkitty-local: search index {}", p.display()),
            None => println!("lawkitty-local: no search_index.db found; full-text search is off"),
        }
        if !app.dist.join("index.html").is_file() {
            println!("lawkitty-local: no web app at {}; run local-app/build-frontend.sh", app.dist.display());
        }
        if let Err(e) = axum::serve(listener, router(app)).await {
            eprintln!("lawkitty-local: server stopped: {e}");
        }
    });
}
