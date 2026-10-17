//! Ingest page endpoints (`/api/admin/*`), off unless `server.allow_ingest`.
//!
//! Uploads are kept in `<data_dir>/uploads/` with a `<file>.meta.json`
//! sidecar holding the form's metadata, so the directory is an ordinary
//! source: `meowlaw ingest <data_dir>/uploads` rebuilds it from scratch.
//! Uploading a file with the same name again is an update: the sync report
//! lists what changed and which citations elsewhere stop resolving.

use crate::{AppState, authorised, forbidden_origin, host_allowed, origin_allowed, unauthorised};
use axum::extract::{Multipart, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use meowlaw_ingest::Manifest;
use meowlaw_search::sync::{SourceEntry, SyncOptions};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// One write at a time: ingestion rebuilds the index and extraction.
static WRITE: Mutex<()> = Mutex::new(());

fn loopback(state: &AppState) -> bool {
    let b = &state.config.server.bind;
    b.starts_with("127.") || b.starts_with("localhost") || b.starts_with("[::1]")
}

/// Why admin access is refused, if it is.
fn guard(state: &AppState, headers: &HeaderMap) -> Option<Response> {
    if !host_allowed(state, headers) || !origin_allowed(state, headers) {
        return Some(forbidden_origin());
    }
    if !state.config.server.allow_ingest {
        return Some(error(
            StatusCode::NOT_FOUND,
            "ingest is disabled: set server.allow_ingest = true",
        ));
    }
    if state.token.is_some() {
        if !authorised(state, headers) {
            return Some(unauthorised());
        }
    } else if !loopback(state) {
        return Some(error(
            StatusCode::FORBIDDEN,
            "ingest without a token is only allowed when bound to localhost",
        ));
    }
    None
}

fn error(code: StatusCode, msg: impl std::fmt::Display) -> Response {
    (code, Json(json!({ "error": msg.to_string() }))).into_response()
}

fn uploads_dir(state: &AppState) -> PathBuf {
    state
        .config
        .resolve(&state.config.storage.data_dir)
        .join("uploads")
}

/// File name safe to write: no directories, no hidden files, and never
/// mistaken for a sidecar.
fn safe_name(raw: &str) -> String {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");
    let mut s: String = base
        .chars()
        .map(|c| if c.is_alphanumeric() || matches!(c, '.' | '-' | '_') { c } else { '-' })
        .collect();
    s = s.trim_start_matches('.').replace(".meta.", "-meta.");
    if s.chars().count() > 120 {
        let ext = Path::new(&s).extension().and_then(|e| e.to_str()).unwrap_or("").to_string();
        s = s.chars().take(100).collect::<String>() + "." + &ext;
    }
    if s.is_empty() || !s.contains('.') { "upload.txt".into() } else { s }
}

fn sidecar(file: &Path) -> PathBuf {
    PathBuf::from(format!("{}.meta.json", file.with_extension("").display()))
}

struct Upload {
    name: String,
    bytes: Vec<u8>,
    manifest: Manifest,
    dry_run: bool,
}

async fn read_upload(mut mp: Multipart) -> Result<Upload, Response> {
    let bad = |e: &dyn std::fmt::Display| error(StatusCode::BAD_REQUEST, e);
    let mut up = Upload {
        name: String::new(),
        bytes: Vec::new(),
        manifest: Manifest::default(),
        dry_run: false,
    };
    while let Some(field) = mp.next_field().await.map_err(|e| bad(&e))? {
        match field.name().unwrap_or("") {
            "file" => {
                up.name = safe_name(field.file_name().unwrap_or("upload.txt"));
                up.bytes = field.bytes().await.map_err(|e| bad(&e))?.to_vec();
            }
            "manifest" => {
                let t = field.text().await.map_err(|e| bad(&e))?;
                if !t.trim().is_empty() {
                    up.manifest = serde_json::from_str(&t)
                        .map_err(|e| bad(&format!("manifest: {e}")))?;
                }
            }
            "dry_run" => up.dry_run = field.text().await.map_err(|e| bad(&e))? == "true",
            _ => {}
        }
    }
    if up.bytes.is_empty() {
        return Err(bad(&"no file uploaded"));
    }
    Ok(up)
}

/// Write the upload and its sidecar into `dir`.
fn stage(dir: &Path, up: &Upload) -> anyhow::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let path = dir.join(&up.name);
    std::fs::write(&path, &up.bytes)?;
    std::fs::write(sidecar(&path), serde_json::to_vec_pretty(&up.manifest)?)?;
    Ok(path)
}

/// A scratch directory for previews and dry runs, removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new(state: &AppState) -> anyhow::Result<Self> {
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let p = state
            .config
            .resolve(&state.config.storage.data_dir)
            .join("tmp")
            .join(format!("{n}"));
        std::fs::create_dir_all(&p)?;
        Ok(Self(p))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn blocking<F>(f: F) -> Response
where
    F: FnOnce() -> anyhow::Result<Value> + Send + 'static,
{
    match tokio::task::spawn_blocking(f).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => error(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, e),
    }
}

/// `GET /api/admin/status`: what the page needs to build its form.
pub async fn status(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Some(r) = guard(&state, &headers) {
        return r;
    }
    let dir = uploads_dir(&state);
    blocking(move || {
        let registry = state.engine.registry();
        let profiles: Vec<Value> = registry
            .ids()
            .into_iter()
            .map(|id| {
                let name = registry.spec(&id).and_then(|s| s.name.clone());
                json!({ "id": id, "name": name })
            })
            .collect();
        let mut uploads = Vec::new();
        if dir.is_dir() {
            let mut entries: Vec<_> = std::fs::read_dir(&dir)?.filter_map(|e| e.ok()).collect();
            entries.sort_by_key(|e| e.file_name());
            for e in entries {
                let name = e.file_name().to_string_lossy().to_string();
                if name.contains(".meta.") || name.starts_with('.') {
                    continue;
                }
                let m: Manifest = std::fs::read(sidecar(&e.path()))
                    .ok()
                    .and_then(|b| serde_json::from_slice(&b).ok())
                    .unwrap_or_default();
                uploads.push(json!({ "file": name, "id": m.id, "title": m.title, "kind": m.kind }));
            }
        }
        Ok(json!({
            "profiles": profiles,
            "kinds": ["legislation", "instrument", "rule_set", "case", "treaty", "guidance", "secondary"],
            "uploads": uploads,
            "uploads_dir": dir.display().to_string(),
            "embeddings": state.engine.embedder.is_some(),
            "max_upload_mb": state.config.server.max_upload_mb,
        }))
    })
    .await
}

/// `POST /api/admin/preview`: run the profile over the upload without
/// storing anything.
pub async fn preview(State(state): State<AppState>, headers: HeaderMap, mp: Multipart) -> Response {
    if let Some(r) = guard(&state, &headers) {
        return r;
    }
    let up = match read_upload(mp).await {
        Ok(u) => u,
        Err(r) => return r,
    };
    blocking(move || {
        let scratch = Scratch::new(&state)?;
        let path = stage(&scratch.0, &up)?;
        let report = meowlaw_search::diagnose::profile_test(
            &path,
            &Manifest::default(),
            state.engine.registry(),
            Some(&state.engine.store),
        )?;
        let lint = meowlaw_lint::lint_file(
            &path,
            &meowlaw_lint::LintOptions {
                profile: up.manifest.profile.as_deref().and_then(|p| state.engine.registry().get(p).ok()),
                kind: up.manifest.kind.as_deref().and_then(meowlaw_core::CorpusKind::parse),
            },
        )?;
        Ok(json!({ "profile": report, "text": report.render(), "lint": lint }))
    })
    .await
}

/// `POST /api/admin/ingest`: store the upload and sync it. With
/// `dry_run=true`, report what would change and keep nothing.
pub async fn ingest(State(state): State<AppState>, headers: HeaderMap, mp: Multipart) -> Response {
    if let Some(r) = guard(&state, &headers) {
        return r;
    }
    let up = match read_upload(mp).await {
        Ok(u) => u,
        Err(r) => return r,
    };
    blocking(move || {
        let _lock = WRITE
            .try_lock()
            .map_err(|_| anyhow::anyhow!("another ingest is running; try again when it finishes"))?;
        let scratch;
        let path = if up.dry_run {
            scratch = Scratch::new(&state)?;
            stage(&scratch.0, &up)?
        } else {
            stage(&uploads_dir(&state), &up)?
        };
        let entry = SourceEntry {
            path: path.clone(),
            manifest: Manifest::default(),
        };
        let report = meowlaw_search::sync::sync(
            &state.engine,
            state.engine.registry(),
            std::slice::from_ref(&entry),
            Path::new(""),
            SyncOptions {
                dry_run: up.dry_run,
                ..Default::default()
            },
        )?;
        let markdown = report.to_markdown();
        if let Some(err) = report.sources.iter().find_map(|s| s.error.clone()) {
            if !up.dry_run {
                // Keep nothing that failed to ingest.
                let _ = std::fs::remove_file(&path);
                let _ = std::fs::remove_file(sidecar(&path));
            }
            anyhow::bail!(err);
        }
        let mut embedded = Value::Null;
        let mut summaries = Value::Null;
        if !up.dry_run {
            // Record the corpus id so the upload can be found and removed.
            if up.manifest.id.is_none()
                && let Some(id) = report.sources.iter().flat_map(|s| &s.diffs).map(|d| d.corpus.clone()).next()
            {
                let mut m = up.manifest.clone();
                m.id = Some(id);
                std::fs::write(sidecar(&path), serde_json::to_vec_pretty(&m)?)?;
            }
            let dir = state.config.resolve(&state.config.storage.data_dir).join("reports");
            std::fs::create_dir_all(&dir)?;
            std::fs::write(dir.join(format!("sync-{}.md", report.started_unix)), &markdown)?;
            if report.changed()
                && let Some(sc) = state.config.summaries.as_ref().filter(|s| s.auto)
            {
                let corpus = report.sources.iter().flat_map(|s| &s.diffs).map(|d| d.corpus.clone()).next();
                let r = meowlaw_search::summarise::summarise(
                    &state.engine.store,
                    sc,
                    meowlaw_search::summarise::SummariseOptions { corpus: corpus.as_deref(), dry_run: false },
                )?;
                if r.changed() {
                    meowlaw_search::rebuild(&state.engine)?;
                }
                summaries = serde_json::to_value(&r)?;
            }
            if report.changed()
                && let Some(emb) = &state.engine.embedder
            {
                let batch = state.config.embeddings.as_ref().map_or(16, |e| e.batch);
                let engine = &state.engine;
                let strip = |c: &meowlaw_core::Corpus, t: &str| {
                    engine.strip_citations(c, t).unwrap_or_else(|_| t.to_string())
                };
                let (nodes, chunks) = meowlaw_search::vector::embed_all(
                    &engine.store,
                    emb.as_ref(),
                    batch * 4,
                    &strip,
                    |_, _| {},
                )?;
                meowlaw_search::vector::build_similarity(&engine.store, emb.model(), state.config.similarity)?;
                engine.store.bump_generation()?;
                embedded = json!({ "nodes": nodes, "chunks": chunks });
            }
        }
        Ok(json!({ "report": report, "markdown": markdown, "embedded": embedded, "summaries": summaries }))
    })
    .await
}

#[derive(Deserialize)]
pub struct RemoveReq {
    corpus: String,
}

/// `POST /api/admin/remove`: delete a corpus, and its upload if it has one.
pub async fn remove(State(state): State<AppState>, headers: HeaderMap, Json(req): Json<RemoveReq>) -> Response {
    if let Some(r) = guard(&state, &headers) {
        return r;
    }
    let dir = uploads_dir(&state);
    blocking(move || {
        let _lock = WRITE
            .try_lock()
            .map_err(|_| anyhow::anyhow!("an ingest is running; try again when it finishes"))?;
        let removed = state.engine.store.delete_corpus(&req.corpus)?;
        let mut files = Vec::new();
        if dir.is_dir() {
            for e in std::fs::read_dir(&dir)?.filter_map(|e| e.ok()) {
                let p = e.path();
                let name = e.file_name().to_string_lossy().to_string();
                if name.ends_with(".meta.json") {
                    continue;
                }
                let m: Option<Manifest> = std::fs::read(sidecar(&p))
                    .ok()
                    .and_then(|b| serde_json::from_slice(&b).ok());
                if m.and_then(|m| m.id).as_deref() == Some(req.corpus.as_str()) {
                    std::fs::remove_file(&p)?;
                    let _ = std::fs::remove_file(sidecar(&p));
                    let _ = state.engine.store.set_kv(&format!("source_hash:{}", p.display()), "");
                    files.push(name);
                }
            }
        }
        let rebuild = if removed { Some(meowlaw_search::rebuild(&state.engine)?) } else { None };
        Ok(json!({ "removed": removed, "files": files, "rebuild": rebuild }))
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::safe_name;

    #[test]
    fn names() {
        assert_eq!(safe_name("../../etc/passwd"), "upload.txt");
        assert_eq!(safe_name("C:\\x\\TR 2018-5.pdf"), "TR-2018-5.pdf");
        assert_eq!(safe_name(".hidden.md"), "hidden.md");
        assert_eq!(safe_name("a.meta.json"), "a-meta.json");
    }
}
