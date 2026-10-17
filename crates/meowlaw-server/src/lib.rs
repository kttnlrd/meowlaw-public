//! HTTP server: REST API, MCP (Streamable HTTP, JSON responses), branding and static UI.

pub mod admin;
pub mod config;

use axum::Router;
use axum::body::Body;
use axum::extract::DefaultBodyLimit;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use meowlaw_mcp::McpServer;
use meowlaw_mcp::access::Scope;
use meowlaw_search::Engine;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Arc;
use tower_http::services::ServeDir;

pub use config::Config;

#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<Engine>,
    pub mcp: McpServer,
    pub config: Arc<Config>,
    pub token: Option<String>,
}

pub fn router(state: AppState) -> Router {
    let ui_dir = state.config.resolve(&state.config.server.ui_dir);
    let api = Router::new()
        .route("/api/info", get(|s, h, q| tool(s, h, q, "get_info")))
        .route("/api/corpora", get(|s, h, q| tool(s, h, q, "list_corpora")))
        .route("/api/search", get(|s, h, q| tool(s, h, q, "search")))
        .route("/api/node", get(|s, h, q| tool(s, h, q, "fetch")))
        .route("/api/outline", get(|s, h, q| tool(s, h, q, "outline")))
        .route("/api/tree", get(tree))
        .route("/api/related", get(|s, h, q| tool(s, h, q, "related")))
        .route(
            "/api/definitions",
            get(|s, h, q| tool(s, h, q, "get_definition")),
        )
        .route("/api/resolve", get(|s, h, q| tool(s, h, q, "resolve")))
        .route(
            "/api/graph/neighbourhood",
            get(|s, h, q| tool(s, h, q, "graph_neighbourhood")),
        )
        .route(
            "/api/graph/path",
            get(|s, h, q| tool(s, h, q, "graph_path")),
        )
        .route(
            "/api/unresolved",
            get(|s, h, q| tool(s, h, q, "unresolved_references")),
        )
        .route("/api/branding", get(branding))
        .route("/theme.css", get(theme_css))
        .route("/brand/logo", get(|s| brand_file(s, true)))
        .route("/brand/favicon", get(|s| brand_file(s, false)))
        .route("/api/admin/status", get(admin::status))
        .route("/api/admin/preview", post(admin::preview))
        .route("/api/admin/ingest", post(admin::ingest))
        .route("/api/admin/remove", post(admin::remove))
        .layer(DefaultBodyLimit::max(
            state.config.server.max_upload_mb.max(1) * 1024 * 1024,
        ))
        .route("/healthz", get(|| async { "ok" }))
        .route("/mcp", get(mcp_get).post(mcp_post).delete(mcp_get));
    api.fallback_service(ServeDir::new(ui_dir).append_index_html_on_directories(true))
        .with_state(state)
}

pub(crate) fn authorised(state: &AppState, headers: &HeaderMap) -> bool {
    let Some(token) = &state.token else {
        return true;
    };
    let given = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    constant_time_eq(given.as_bytes(), token.as_bytes())
}

fn bearer(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|t| !t.is_empty())
}

/// What this caller may read. `Err` is the response to send instead: an
/// unknown token, or no token where one is required (`required`).
fn caller_scope(
    state: &AppState,
    headers: &HeaderMap,
    required: bool,
) -> Result<Scope, Box<Response>> {
    let given = bearer(headers);
    if let (Some(g), Some(t)) = (given, &state.token)
        && constant_time_eq(g.as_bytes(), t.as_bytes())
    {
        return Ok(Scope::all());
    }
    let Some(access) = &state.config.access else {
        // No access control: the main token, when set and required, is the only key.
        return if required && state.token.is_some() {
            Err(Box::new(unauthorised()))
        } else {
            Ok(Scope::all())
        };
    };
    let tags = match given {
        Some(g) => access
            .tags_for_token(g)
            .ok_or_else(|| Box::new(unauthorised()))?,
        None if required => return Err(Box::new(unauthorised())),
        None => access.anonymous.clone(),
    };
    let corpora = state.engine.store.corpora().map_err(|e| {
        Box::new(
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": format!("{e:#}") })),
            )
                .into_response(),
        )
    })?;
    let hidden = corpora
        .iter()
        .filter(|c| !tags.contains(&access.tag_of(c)))
        .map(|c| c.id.clone())
        .collect();
    Ok(Scope::hiding(hidden))
}

pub(crate) fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub(crate) fn unauthorised() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        [(header::WWW_AUTHENTICATE, "Bearer")],
        Json(json!({ "error": "missing or invalid bearer token" })),
    )
        .into_response()
}

/// Query-string values arrive as strings; turn numeric ones into numbers and
/// comma lists for `edge_kinds` into arrays so the tool schemas line up.
const NUMERIC: &[&str] = &["limit", "depth", "max_hops", "max_chars"];

fn args_from_query(q: HashMap<String, String>) -> Value {
    let mut m = serde_json::Map::new();
    for (k, v) in q {
        let key = if k == "q" { "query".to_string() } else { k };
        let val = if key == "edge_kinds" {
            Value::Array(
                v.split(',')
                    .map(|s| Value::String(s.trim().into()))
                    .collect(),
            )
        } else if NUMERIC.contains(&key.as_str())
            && let Ok(n) = v.parse::<u64>()
        {
            // Only numeric parameters become numbers: a search for "1997" is text.
            Value::from(n)
        } else {
            Value::String(v)
        };
        m.insert(key, val);
    }
    Value::Object(m)
}

async fn tool(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
    name: &'static str,
) -> Response {
    if !host_allowed(&state, &headers) || !origin_allowed(&state, &headers) {
        return forbidden_origin();
    }
    let scope = match caller_scope(&state, &headers, state.config.server.protect_api) {
        Ok(s) => s,
        Err(r) => return *r,
    };
    let args = args_from_query(q);
    let engine = state.engine.clone();
    let res = tokio::task::spawn_blocking(move || {
        meowlaw_mcp::access::call_scoped(&engine, name, &args, &scope)
    })
    .await;
    match res {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => (
            StatusCode::BAD_REQUEST,
            Json(json!({ "error": format!("{e:#}") })),
        )
            .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": e.to_string() })),
        )
            .into_response(),
    }
}

/// Whole navigation tree of one corpus, for the UI drawer. Cached by the
/// browser per store generation (the ETag), so large Acts load once.
async fn tree(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !host_allowed(&state, &headers) || !origin_allowed(&state, &headers) {
        return forbidden_origin();
    }
    let scope = match caller_scope(&state, &headers, state.config.server.protect_api) {
        Ok(s) => s,
        Err(r) => return *r,
    };
    let Some(corpus) = q.get("corpus").cloned() else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({ "error": "`corpus` is required" })),
        )
            .into_response();
    };
    if !scope.corpus_visible(&corpus) {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "no such corpus" })),
        )
            .into_response();
    }
    let engine = state.engine.clone();
    let res = tokio::task::spawn_blocking(move || {
        let generation = engine.store.generation()?;
        Ok::<_, anyhow::Error>((generation, engine.store.corpus_tree(&corpus)?))
    })
    .await;
    match res {
        Ok(Ok((generation, Some(t)))) => {
            let etag = format!("\"{}-{generation}\"", t["corpus"].as_str().unwrap_or(""));
            if headers
                .get(header::IF_NONE_MATCH)
                .and_then(|v| v.to_str().ok())
                == Some(etag.as_str())
            {
                return StatusCode::NOT_MODIFIED.into_response();
            }
            (
                [
                    (header::ETAG, etag),
                    (header::CACHE_CONTROL, "no-cache".to_string()),
                ],
                Json(t),
            )
                .into_response()
        }
        Ok(Ok((_, None))) => (
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "no such corpus" })),
        )
            .into_response(),
        Ok(Err(e)) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": format!("{e:#}") })),
        )
            .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": e.to_string() })),
        )
            .into_response(),
    }
}

async fn branding(State(state): State<AppState>) -> Json<Value> {
    let b = &state.config.branding;
    Json(json!({
        "name": b.name,
        "tagline": b.tagline,
        "footer": b.footer,
        "search_hint": b.search_hint,
        "locale": b.locale,
        "logo": b.logo.as_ref().map(|_| "/brand/logo"),
        "favicon": b.favicon.as_ref().or(b.logo.as_ref()).map(|_| "/brand/favicon"),
        "source_groups": state.config.source_groups,
        "access_control": state.config.access.is_some(),
    }))
}

async fn theme_css(State(state): State<AppState>) -> Response {
    (
        [
            (header::CONTENT_TYPE, "text/css; charset=utf-8"),
            (header::CACHE_CONTROL, "no-cache"),
        ],
        state.config.theme_css(),
    )
        .into_response()
}

async fn brand_file(State(state): State<AppState>, logo: bool) -> Response {
    let b = &state.config.branding;
    let p = if logo {
        b.logo.as_ref()
    } else {
        b.favicon.as_ref().or(b.logo.as_ref())
    };
    let Some(p) = p else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let path = state.config.resolve(p);
    let mime = match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("webp") => "image/webp",
        Some("ico") => "image/x-icon",
        _ => "application/octet-stream",
    };
    match tokio::fs::read(&path).await {
        Ok(bytes) => ([(header::CONTENT_TYPE, mime)], Body::from(bytes)).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

/// Browser requests from other sites must be listed (DNS-rebinding and CSRF
/// protection). Clients that send no Origin (CLI, desktop MCP clients) pass.
pub(crate) fn origin_allowed(state: &AppState, headers: &HeaderMap) -> bool {
    match headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
        None => true,
        Some(o) => state.config.server.allowed_origins.iter().any(|a| a == o),
    }
}

/// When bound to loopback, only accept loopback Host headers, so a page on a
/// rebinding domain cannot read the API through the user's browser.
pub(crate) fn host_allowed(state: &AppState, headers: &HeaderMap) -> bool {
    let bind = &state.config.server.bind;
    let loopback =
        bind.starts_with("127.") || bind.starts_with("localhost") || bind.starts_with("[::1]");
    if !loopback {
        return true;
    }
    let host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let name = host.rsplit_once(':').map_or(host, |(h, _)| h);
    matches!(name, "127.0.0.1" | "localhost" | "[::1]" | "")
}

pub(crate) fn forbidden_origin() -> Response {
    (
        StatusCode::FORBIDDEN,
        Json(json!({ "error": "origin or host not allowed" })),
    )
        .into_response()
}

async fn mcp_get() -> Response {
    // Stateless server: no server-initiated stream, no sessions to delete.
    (StatusCode::METHOD_NOT_ALLOWED, [(header::ALLOW, "POST")]).into_response()
}

async fn mcp_post(State(state): State<AppState>, headers: HeaderMap, body: String) -> Response {
    if !origin_allowed(&state, &headers) || !host_allowed(&state, &headers) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({ "error": "origin not allowed" })),
        )
            .into_response();
    }
    // /mcp needs a token whenever one is configured.
    let required = state.token.is_some()
        || state
            .config
            .access
            .as_ref()
            .is_some_and(|a| a.anonymous.is_empty());
    let scope = match caller_scope(&state, &headers, required) {
        Ok(s) => s,
        Err(r) => return *r,
    };
    let msg: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({ "jsonrpc": "2.0", "id": null, "error": { "code": -32700, "message": format!("Parse error: {e}") } })),
            )
                .into_response();
        }
    };
    let mcp = state.mcp.clone();
    match tokio::task::spawn_blocking(move || mcp.handle_body_scoped(&msg, &scope)).await {
        Ok(Some(reply)) => Json(reply).into_response(),
        // Notifications and responses only: 202 Accepted, no body.
        Ok(None) => StatusCode::ACCEPTED.into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

pub async fn serve(mut state: AppState) -> anyhow::Result<()> {
    let bind = state.config.server.bind.clone();
    // The server's own origin is always allowed, whatever port it binds.
    {
        let cfg = Arc::make_mut(&mut state.config);
        if let Some((_, port)) = bind.rsplit_once(':') {
            for h in ["localhost", "127.0.0.1"] {
                let o = format!("http://{h}:{port}");
                if !cfg.server.allowed_origins.contains(&o) {
                    cfg.server.allowed_origins.push(o);
                }
            }
        }
    }
    if state.token.is_none() && !bind.starts_with("127.") && !bind.starts_with("localhost") {
        tracing::warn!(
            "no {} set and binding to {bind}: /mcp is open to the network",
            state.config.server.token_env
        );
    }
    let app = router(state);
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!("listening on http://{bind}");
    axum::serve(listener, app).await?;
    Ok(())
}
