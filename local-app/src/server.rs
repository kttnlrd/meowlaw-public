//! The HTTP side: the browse API the React app calls, and the app's static files.

use crate::{catalog, pyjson, search, section, state, App, INFO_JSON};
use axum::body::{Body, Bytes};
use axum::extract::{Path as UrlPath, Query, Request, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use regex::Regex;
use serde::ser::{SerializeMap, Serializer};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, LazyLock};

type Shared = State<Arc<App>>;
type Params = Query<HashMap<String, String>>;

pub fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/health", get(|| async { Json(json!({"status": "ok"})) }))
        // Browse API: real.
        .route("/api/acts", get(acts))
        .route("/api/tree/{act}", get(tree_full))
        .route("/api/tree/{act}/part", get(tree_part))
        .route("/api/section/{act}/{*section}", get(section_route))
        .route("/api/info", get(|| async { json_response(INFO_JSON) }))
        // Search: the legislation lane of the FTS index, when the index is there.
        .route("/api/search", get(search_route))
        .route("/api/search/hybrid", get(search_hybrid))
        .route("/api/search/suggest", get(search_suggest))
        .route("/api/search/flat", get(search_flat))
        .route("/api/search/facets", get(search_facets))
        .route("/api/search/warm", get(|| async { Json(json!({"ok": true, "warmed": false, "skipped": true, "ms": 0})) }))
        // One fixed local user; nothing to sign in to.
        .route("/auth/me", get(auth_me))
        .route("/auth/logout", get(|| async { redirect(StatusCode::TEMPORARY_REDIRECT, "/app/") }))
        .route("/api/account", get(account).put(account))
        .route("/api/account/pinned-sources", get(pinned_get).put(pinned_put))
        .route("/api/user/prefs", get(prefs_get).put(prefs_put))
        .route("/api/user/prefs/reset", post(prefs_reset))
        .route("/api/content-types", get(content_types))
        // Features this build does not carry: the empty answer each screen already handles.
        .route("/api/mcp-tokens", get(|| async { Json(json!({"tokens": []})) }))
        .route("/api/maps", get(|| async { Json(json!([])) }))
        .route("/api/tax/health", get(|| async { Json(json!({"ok": false, "detail": "chains disabled"})) }))
        .route("/api/cases/{act}/{section}", get(|p: UrlPath<(String, String)>| async move { empty_list(p, "cases") }))
        .route("/api/rulings/{act}/{section}", get(|p: UrlPath<(String, String)>| async move { empty_list(p, "rulings") }))
        .route("/api/comments/{act}/{section}", get(|p: UrlPath<(String, String)>| async move { empty_list(p, "comments") }))
        .route("/api/section-refs/{act}/{section}", get(section_refs))
        // The web app, at the paths the Python app serves it.
        .route("/", get(|| async { redirect(StatusCode::TEMPORARY_REDIRECT, "/app/") }))
        .route("/app", get(|| async { redirect(StatusCode::TEMPORARY_REDIRECT, "/app/") }))
        .route("/app/", get(app_index))
        .route("/app/{*path}", get(app_file))
        .fallback(fallback)
        .layer(axum::middleware::from_fn(local_only))
        .with_state(app)
}

/// Refuses a request addressed to any name but this machine's loopback. The listener is on
/// 127.0.0.1 already; this stops a web page that re-points its own hostname at 127.0.0.1
/// (DNS rebinding) from reading the data folder through the user's browser.
async fn local_only(req: Request, next: axum::middleware::Next) -> Response {
    let host = req.headers().get(header::HOST).and_then(|h| h.to_str().ok()).unwrap_or("");
    let name = match host.rsplit_once(':') {
        Some((name, port)) if port.bytes().all(|b| b.is_ascii_digit()) => name,
        _ => host,
    };
    if !matches!(name, "" | "127.0.0.1" | "localhost" | "[::1]") {
        return detail(403, "This server only answers on 127.0.0.1");
    }
    next.run(req).await
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f).await.expect("request task panicked")
}

fn json_response(body: impl Into<Body>) -> Response {
    ([(header::CONTENT_TYPE, "application/json")], body.into()).into_response()
}

/// FastAPI's error body: `{"detail": "..."}`.
fn detail(status: u16, message: &str) -> Response {
    let code = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (code, Json(json!({ "detail": message }))).into_response()
}

fn redirect(status: StatusCode, to: &str) -> Response {
    match HeaderValue::from_str(to) {
        Ok(location) => (status, [(header::LOCATION, location)]).into_response(),
        Err(_) => detail(404, "Not Found"),
    }
}

/// FastAPI's reading of a boolean query value.
fn flag(q: &HashMap<String, String>, key: &str) -> bool {
    q.get(key).is_some_and(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "on" | "yes"))
}

fn text<'a>(q: &'a HashMap<String, String>, key: &str) -> Option<&'a str> {
    q.get(key).map(String::as_str).filter(|v| !v.is_empty())
}

fn number(q: &HashMap<String, String>, key: &str, default: i64) -> i64 {
    q.get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

// ---------------------------------------------------------------------------
// Browse API
// ---------------------------------------------------------------------------

async fn acts(State(app): Shared, Query(q): Params) -> Response {
    json_response(blocking(move || app.catalog.acts_json(text(&q, "jurisdiction"))).await)
}

async fn tree_full(State(app): Shared, UrlPath(act): UrlPath<String>, Query(q): Params) -> Response {
    let result = blocking(move || {
        let tree = app.trees.get(&act)?;
        Ok::<Bytes, (u16, String)>(tree.render(app.catalog.licence(&act), flag(&q, "shallow"), text(&q, "open")))
    })
    .await;
    match result {
        // X-Uncompressed-Length lets the app show a real progress bar for large trees.
        Ok(body) => (
            [
                (header::CONTENT_TYPE, HeaderValue::from_static("application/json")),
                (header::HeaderName::from_static("x-uncompressed-length"), HeaderValue::from(body.len())),
            ],
            body,
        )
            .into_response(),
        Err((status, message)) => detail(status, &message),
    }
}

async fn tree_part(State(app): Shared, UrlPath(act): UrlPath<String>, Query(q): Params) -> Response {
    let result = blocking(move || {
        let tree = app.trees.get(&act)?;
        // An empty `section=` still selects by section, as in the Python route.
        let section = q.get("section").map(String::as_str);
        tree.part(q.get("id").map(String::as_str), section).ok_or((404, "Part not found".to_string()))
    })
    .await;
    match result {
        Ok(body) => json_response(body),
        Err((status, message)) => detail(status, &message),
    }
}

async fn section_route(State(app): Shared, UrlPath((act, section)): UrlPath<(String, String)>) -> Response {
    let result = blocking(move || {
        // 's8-1' -> '8-1'. The digit guard keeps genuine ids such as 'schedule-2'.
        let bytes = section.as_bytes();
        let section = if bytes.len() > 1 && bytes[0] == b's' && bytes[1].is_ascii_digit() { &section[1..] } else { &section[..] };
        let rendered = app.sections.render(&app.trees, &act, section)?;
        let mut out = Vec::with_capacity(rendered.body.len() + rendered.body.len() / 8 + 1024);
        let mut ser = serde_json::Serializer::new(&mut out);
        let write = (|| {
            let mut m = ser.serialize_map(None)?;
            m.serialize_entry("frontmatter", &rendered.frontmatter)?;
            m.serialize_entry("body", &rendered.body)?;
            if let Some(licence) = app.catalog.licence(&act) {
                m.serialize_entry("licence", licence)?;
            }
            if let Some(hint) = section::dictionary_hint(&act, section) {
                m.serialize_entry("hint", &hint)?;
            }
            m.end()
        })();
        write.map_err(|e| (500, e.to_string()))?;
        Ok::<Vec<u8>, (u16, String)>(out)
    })
    .await;
    match result {
        Ok(body) => json_response(body),
        Err((status, message)) => detail(status, &message),
    }
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

static SECTION_NUMBER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]+(-[0-9]+)?$").expect("static pattern"));

fn with_note(app: &App, mut body: Value) -> Json<Value> {
    if app.search.path().is_none() {
        body["note"] = json!(search::NO_INDEX_NOTE);
    }
    Json(body)
}

/// `/api/search`: keyword search, usually scoped to one act.
async fn search_route(State(app): Shared, Query(q): Params) -> Json<Value> {
    blocking(move || {
        let limit = number(&q, "limit", 50).clamp(0, 100) as usize;
        let offset = number(&q, "offset", 0).max(0) as usize;
        let query = catalog::py_strip(q.get("q").map_or("", String::as_str));
        let act = text(&q, "act");
        let mut results: Vec<Value> = Vec::new();
        let exact = act.filter(|_| SECTION_NUMBER.is_match(query)).and_then(|a| app.search.exact(a, query));
        results.extend(exact.clone());
        let acts: Vec<String> = act.map(String::from).into_iter().collect();
        let (rows, fts_total) = app.search.sections(query, &acts, (offset + limit).min(100), "AND");
        let same = |r: &Value, e: &Value| r["act"] == e["act"] && r["section"] == e["section"];
        results.extend(rows.into_iter().filter(|r| !exact.as_ref().is_some_and(|e| same(r, e))));
        // Nothing indexed: fall back to ids and titles in the act's tree.
        if results.is_empty() && !query.is_empty() {
            if let Some(tree) = act.and_then(|a| app.trees.get(a).ok()) {
                let needle = query.to_lowercase();
                for s in tree.sections() {
                    let id = s.get("id").and_then(pyjson::V::as_str).unwrap_or("");
                    let title = s.get("title").and_then(pyjson::V::as_str).unwrap_or("");
                    if id.to_lowercase().contains(&needle) || title.to_lowercase().contains(&needle) {
                        results.push(json!({"act": act, "section": id, "title": title}));
                    }
                }
            }
        }
        let total = if fts_total > 0 { fts_total as usize + exact.is_some() as usize } else { results.len() };
        let page: Vec<Value> = results.into_iter().skip(offset).take(limit).collect();
        let engine = if app.search.path().is_some() { "fts5" } else { "fallback" };
        with_note(&app, json!({"results": page, "total": total, "offset": offset, "limit": limit, "engine": engine}))
    })
    .await
}

/// `/api/search/hybrid`, legislation lane only: BM25 order, no embeddings and no fusion.
async fn search_hybrid(State(app): Shared, Query(q): Params) -> Json<Value> {
    blocking(move || {
        let limit = number(&q, "limit", 20).clamp(0, 50) as usize;
        let offset = number(&q, "offset", 0).max(0) as usize;
        let query = catalog::py_strip(q.get("q").map_or("", String::as_str));
        let acts: Vec<String> = text(&q, "act").unwrap_or("").split(',').filter(|a| !a.is_empty()).map(String::from).collect();
        let wants_sections = text(&q, "type").is_none_or(|t| t.to_lowercase().split(',').any(|v| v == "section"));
        let (rows, total) = if wants_sections {
            app.search.sections(query, &acts, offset + limit, text(&q, "operator").unwrap_or("AND"))
        } else {
            (Vec::new(), 0)
        };
        let page: Vec<Value> = rows
            .into_iter()
            .enumerate()
            .skip(offset)
            .take(limit)
            .map(|(rank, mut r)| {
                r["source_type"] = json!("section");
                r["fusion_score"] = json!(1.0 / (60.0 + rank as f64 + 1.0));
                r["cross_references"] = json!([]);
                r
            })
            .collect();
        let scope = text(&q, "scope").unwrap_or("au-tax");
        with_note(&app, json!({"results": page, "total": total, "offset": offset, "limit": limit,
                               "meta": {"reranked": false, "scope": scope}}))
    })
    .await
}

async fn search_suggest(State(app): Shared, Query(q): Params) -> Json<Value> {
    blocking(move || {
        let query = catalog::py_strip(q.get("q").map_or("", String::as_str));
        let limit = number(&q, "limit", 12).clamp(0, 50) as usize;
        let suggestions = if query.chars().count() < 2 { Vec::new() } else { app.search.suggest(query, limit) };
        Json(json!({"query": query, "suggestions": suggestions}))
    })
    .await
}

async fn search_flat(State(app): Shared, Query(q): Params) -> Json<Value> {
    blocking(move || {
        let query = catalog::py_strip(q.get("q").map_or("", String::as_str));
        let limit = number(&q, "limit", 50).clamp(0, 100) as usize;
        let rows: Vec<Value> = app
            .search
            .sections(query, &[], limit, "AND")
            .0
            .into_iter()
            .map(|r| json!({"type": "section", "act": r["act"], "section": r["section"], "title": r["title"], "snippet": r["snippet"]}))
            .collect();
        Json(json!({"query": query, "results": rows}))
    })
    .await
}

async fn search_facets(Query(q): Params) -> Json<Value> {
    Json(json!({"facets": {}, "scope": text(&q, "scope").unwrap_or("au-tax"), "library": null}))
}

// ---------------------------------------------------------------------------
// Local user and stubs
// ---------------------------------------------------------------------------

async fn auth_me() -> Json<Value> {
    Json(json!({"id": null, "email": state::LOCAL_EMAIL, "name": state::LOCAL_NAME, "tier": "local",
                "trial_started_at": null, "authenticated": true, "is_admin": false}))
}

async fn account() -> Json<Value> {
    Json(json!({"email": state::LOCAL_EMAIL, "name": state::LOCAL_NAME, "provider": "local"}))
}

async fn pinned_get(State(app): Shared) -> Json<Value> {
    Json(blocking(move || app.local.pinned()).await)
}

async fn pinned_put(State(app): Shared, Json(body): Json<Value>) -> Json<Value> {
    Json(blocking(move || app.local.set_pinned(body["sources"].as_array().map_or(&[][..], Vec::as_slice))).await)
}

async fn prefs_get(State(app): Shared) -> Json<Value> {
    Json(blocking(move || app.local.prefs()).await)
}

async fn prefs_put(State(app): Shared, Json(body): Json<Value>) -> Json<Value> {
    Json(blocking(move || app.local.update_prefs(&body)).await)
}

async fn prefs_reset(State(app): Shared) -> Json<Value> {
    Json(blocking(move || app.local.reset_prefs()).await)
}

/// The app hides a nav entry whose content type is not available. Legislation and every
/// collection are; the browsers this build leaves out are listed as unavailable.
async fn content_types(State(app): Shared) -> Json<Value> {
    const NOT_SERVED: [(&str, &str); 12] = [
        ("afsa-guide", "AFSA practice guidance"),
        ("ato-private-ruling", "Private rulings"),
        ("case", "Tax cases"),
        ("chains", "Interpretive chains"),
        ("commentary", "Commentary"),
        ("insolvency", "Insolvency"),
        ("maps", "Procedural maps"),
        ("nz-mtg", "NZ Master Tax Guide"),
        ("nz-ruling", "NZ rulings"),
        ("regulatory-guide", "ASIC regulatory guides"),
        ("ruling", "Public rulings"),
        ("treaty", "Tax treaties"),
    ];
    let mut rows = vec![json!({"key": "legislation", "label": "Legislation", "auth": "user", "enabled": true, "available": true})];
    rows.extend(NOT_SERVED.iter().map(|(key, label)| json!({"key": key, "label": label, "auth": "user", "enabled": false, "available": false})));
    let mut collections: Vec<_> = app.catalog.collections.iter().collect();
    collections.sort_by_key(|(id, _)| id.as_str());
    rows.extend(collections.into_iter().map(|(id, c)| {
        json!({"key": id, "label": c.title, "auth": "public", "enabled": true, "available": true,
               "collection": true, "access": c.access, "group": c.group})
    }));
    Json(Value::Array(rows))
}

fn empty_list(UrlPath((act, section)): UrlPath<(String, String)>, key: &str) -> Json<Value> {
    let mut body = json!({"act": act, "section": section, "count": 0});
    body[key] = json!([]);
    Json(body)
}

async fn section_refs(UrlPath((act, section)): UrlPath<(String, String)>) -> Json<Value> {
    Json(json!({"act": act, "section": section, "sections": [], "definitions": []}))
}

// ---------------------------------------------------------------------------
// The web app
// ---------------------------------------------------------------------------

const NO_CACHE: [(header::HeaderName, &str); 3] = [
    (header::CACHE_CONTROL, "no-cache, no-store, must-revalidate"),
    (header::PRAGMA, "no-cache"),
    (header::EXPIRES, "0"),
];

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase().as_str() {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json",
        "webmanifest" => "application/manifest+json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "txt" => "text/plain; charset=utf-8",
        "wasm" => "application/wasm",
        _ => "application/octet-stream",
    }
}

fn index(app: &App) -> Response {
    match std::fs::read(app.dist.join("index.html")) {
        Ok(body) => (NO_CACHE, [(header::CONTENT_TYPE, "text/html; charset=utf-8")], body).into_response(),
        Err(_) => (
            NO_CACHE,
            [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
            format!(
                "<h1>lawkitty</h1><p>Frontend not built yet. Run <code>local-app/build-frontend.sh</code>, or pass \
                 <code>--dist</code>. Looked in {}.</p>",
                app.dist.display()
            ),
        )
            .into_response(),
    }
}

async fn app_index(State(app): Shared) -> Response {
    blocking(move || index(&app)).await
}

async fn app_file(State(app): Shared, UrlPath(path): UrlPath<String>) -> Response {
    blocking(move || {
        if path.starts_with("register") {
            return (StatusCode::NOT_FOUND, Json(json!({"error": "Not found"}))).into_response();
        }
        // The path comes from the URL: resolve it and serve only what is still inside dist.
        let root = app.dist.canonicalize().ok();
        let file = if path.contains('\0') { None } else { app.dist.join(&path).canonicalize().ok() };
        let inside = file.filter(|f| root.as_ref().is_some_and(|r| f.starts_with(r)) && f.is_file());
        if let Some(body) = inside.as_ref().and_then(|f| std::fs::read(f).ok()) {
            let kind = content_type(inside.as_ref().expect("checked above"));
            return ([(header::CONTENT_TYPE, kind), (header::CACHE_CONTROL, "public, max-age=3600")], body).into_response();
        }
        if path.starts_with("assets/") {
            return detail(404, "Not Found");
        }
        index(&app)
    })
    .await
}

/// Old deep links (`/itaa-1997/8-1`, `/search?q=`) move to `/app/...`; anything else is a 404.
async fn fallback(State(app): Shared, req: Request) -> Response {
    const LEGACY: [&str; 12] = [
        "search", "settings", "maps", "definitions", "private-rulings", "tax-cases", "treaties", "rulings", "ato",
        "regulatory-guides", "afsa-guides", "admin",
    ];
    let path = req.uri().path();
    let first = path.trim_start_matches('/').split('/').next().unwrap_or("");
    if req.method() == axum::http::Method::GET && (LEGACY.contains(&first) || app.catalog.has(first)) {
        let query = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
        return redirect(StatusCode::PERMANENT_REDIRECT, &format!("/app{path}{query}"));
    }
    detail(404, "Not Found")
}
