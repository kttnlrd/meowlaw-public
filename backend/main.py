"""backend/main.py — FastAPI app for Legislation Explorer.

Serves:
  - React SPA static files
  - JSON API for tree, sections, definitions, search
  - MCP over Streamable HTTP
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.config import ALLOWED_ORIGINS, FRONTEND_DIST, IS_PRODUCTION, SEARCH_DB, BASE
from backend.doc_ids import STATE_TAX_ACTS
from backend import db
from backend.logging_config import setup_logging
from backend.middleware.metrics import MetricsMiddleware
from backend.middleware.ratelimit import RateLimitMiddleware
from backend.auth import require_user
from backend.auth.providers import sso
from backend.routes.api import router as api_router
from backend.routes.social import router as social_router
from backend.routes.mcp import router as mcp_router
from backend.routes.ato import router as ato_router
from backend.routes.maps import router as maps_router
from backend.routes.account import router as account_router
from backend.routes.playground import router as playground_router
from backend.routes.commentary import router as commentary_router
from backend.routes.insolvency import router as insolvency_router
from backend.routes.nz_mtg import router as nz_mtg_router
from backend.fastmcp_server import mcp as fastmcp, MCPAuthMiddleware
from backend.services.search_service import init_search_index
from backend.services import vector_search_service

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build search index in background on startup if missing or stale."""
    loop = asyncio.get_running_loop()
    if not SEARCH_DB.exists() or SEARCH_DB.stat().st_size == 0:
        logger.info("Search index missing, building in background...")
        await loop.run_in_executor(None, init_search_index)
    await loop.run_in_executor(None, vector_search_service.load)

    # Preload lazy caches so the first request doesn't pay cold-start cost.
    from backend.routes.search import _load_private_rulings_index
    from backend.services.graph_alias import _load_alias_map, _graph_key_set
    from backend.services.data_loader import load_tree, load_acts_meta

    def _preload_trees():
        for a in load_acts_meta():
            load_tree(a["id"])

    for _name, _fn in (
        ("private_rulings_index", _load_private_rulings_index),
        ("alias_map", _load_alias_map),
        ("graph_keys", _graph_key_set),
        ("act_trees", _preload_trees),
    ):
        try:
            await loop.run_in_executor(None, _fn)
        except Exception:
            logger.exception("Startup preload failed: %s", _name)

    # Run FastMCP session manager (handles Streamable HTTP connections)
    async with fastmcp._session_manager.run():
        yield


app = FastAPI(title="MeowLaw", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# GZip compression for large JSON payloads
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Metrics
app.add_middleware(MetricsMiddleware)

# Rate limiting (on by default, disable with RATE_LIMIT_ENABLED=false)
app.add_middleware(RateLimitMiddleware, enabled=os.environ.get("RATE_LIMIT_ENABLED", "true").lower() == "true")

# Force HTML to revalidate so marketing-site copy/layout edits show up immediately
@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Force HTTPS: requests that arrive over plain HTTP (through the tunnel) are
# redirected to HTTPS so Secure cookies get stored/sent. cloudflared sets
# X-Forwarded-Proto to the real client scheme.
@app.middleware("http")
async def force_https(request: Request, call_next):
    if request.headers.get("x-forwarded-proto", "https") == "http":
        return RedirectResponse(url=str(request.url.replace(scheme="https")), status_code=308)
    return await call_next(request)


# Baseline security headers on every response.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Enforced. Inline scripts live in files (site/js/, frontend/public/theme-init.js);
    # static.cloudflareinsights.com is the beacon Cloudflare injects into every page.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    # CORSMiddleware adds Allow-Credentials even when it refused the origin; keep the pair together.
    if "access-control-allow-origin" not in response.headers:
        del response.headers["access-control-allow-credentials"]
    return response


# Defence in depth on top of SameSite=Lax: a cookie-carrying write that names a
# foreign Origin is refused. No Origin (curl, MCP, server-to-server) passes, as
# does Sec-Fetch-Site: same-origin (browser-set, cannot be forged by a page).
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_base = urlsplit(os.environ.get("APP_BASE_URL", ""))
_TRUSTED_ORIGINS = set(ALLOWED_ORIGINS) | ({f"{_base.scheme}://{_base.netloc}"} if _base.netloc else set())


@app.middleware("http")
async def origin_check(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method in _STATE_CHANGING
        and origin is not None
        and "session" in request.cookies
        and origin not in _TRUSTED_ORIGINS
        and request.headers.get("sec-fetch-site") != "same-origin"
    ):
        return JSONResponse({"error": "Cross-origin request refused"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth: email magic-link sign-in + signed session cookie.
# Users sign in via magic link, then create streamable-HTTP MCP tokens.
# (Google/Microsoft OAuth SSO removed — access is UI + MCP only.)
# ---------------------------------------------------------------------------

from starlette.middleware.sessions import SessionMiddleware
from backend.auth import (
    SESSION_SECRET,
    AuthMiddleware,
    request_magic,
    verify_magic_code,
    callback_magic,
    sso_providers,
    login_sso,
    callback_sso,
    logout,
    me,
)
from backend.oauth_provider import (
    handle_well_known,
    handle_authorize,
    handle_authorize_consent,
    handle_token,
    handle_register,
    handle_revoke,
)

# Carries the SSO state/nonce/PKCE verifier across the provider round trip (authlib).
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="starlette_session",
                   same_site="lax", https_only=IS_PRODUCTION, max_age=3600)
app.add_middleware(AuthMiddleware)

app.add_api_route("/auth/login/magic", request_magic, methods=["POST"])
app.add_api_route("/auth/login/magic/verify", verify_magic_code, methods=["POST"])
app.add_api_route("/auth/callback/magic", callback_magic, methods=["GET"])
# Google / Microsoft sign-in — registered after the magic routes so "magic" is never a provider.
app.add_api_route("/auth/providers", sso_providers, methods=["GET"])
app.add_api_route("/auth/login/{provider}", login_sso, methods=["GET"])
app.add_api_route("/auth/callback/{provider}", callback_sso, methods=["GET"])
app.add_api_route("/auth/logout", logout, methods=["GET"])
app.add_api_route("/auth/me", me, methods=["GET"])

# OAuth 2.1 endpoints for MCP Connector auth
app.add_api_route(
    "/.well-known/oauth-authorization-server",
    handle_well_known,
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route("/oauth/authorize", handle_authorize, methods=["GET"])
app.add_api_route("/oauth/authorize/consent", handle_authorize_consent, methods=["POST"])
app.add_api_route("/oauth/token", handle_token, methods=["POST"])
app.add_api_route("/oauth/register", handle_register, methods=["POST"])
app.add_api_route("/oauth/revoke", handle_revoke, methods=["POST"])
logger.info("Magic-link auth enabled; SSO providers: %s", ", ".join(sso.LABELS) or "none configured")
logger.info("OAuth 2.1 MCP endpoints enabled")


# Health check
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Detailed health check for monitoring
# ---------------------------------------------------------------------------

@app.get("/health/check")
def health_check():
    """Detailed health check: DB, FTS5 search, memory, PG connectivity.

    Returns structured pass/fail. Unauthenticated so the monitor cron can hit it
    without a token, so failure detail goes to the log, not the response.
    """
    import psutil

    checks = {}

    # 1. Search DB exists and FTS5 queryable
    try:
        conn = sqlite3.connect(str(SEARCH_DB))
        conn.execute("SELECT 1 FROM sections_fts LIMIT 1")
        conn.close()
        checks["search_db"] = {"status": "pass"}
    except Exception:
        logger.exception("health check: search index not queryable")
        checks["search_db"] = {"status": "fail"}

    # 2. PostgreSQL connectivity (the app's own user-data database)
    if db.DATABASE_URL:
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
            checks["postgres"] = {"status": "pass"}
        except Exception:
            logger.exception("health check: postgres unreachable")
            checks["postgres"] = {"status": "fail"}

    # 3. Memory usage
    mem = psutil.virtual_memory()
    checks["memory"] = {
        "status": "pass" if mem.percent < 90 else "warn",
        "percent": mem.percent,
        "available_mb": mem.available // (1024 * 1024),
    }

    # 4. Process uptime
    try:
        p = psutil.Process(os.getpid())
        created = datetime.fromtimestamp(p.create_time())
        checks["uptime"] = {
            "status": "pass",
            "seconds": int((datetime.now() - created).total_seconds()),
        }
    except Exception:
        checks["uptime"] = {"status": "error"}

    overall = all(c.get("status") == "pass" for c in checks.values())
    return {
        "status": "ok" if overall else "degraded",
        "checks": checks,
    }


# API routes
app.include_router(social_router, dependencies=[Depends(require_user)])
app.include_router(api_router, dependencies=[Depends(require_user)])
app.include_router(playground_router, dependencies=[Depends(require_user)])
app.include_router(mcp_router)
app.include_router(ato_router, dependencies=[Depends(require_user)])
app.include_router(maps_router, dependencies=[Depends(require_user)])
# Admin-gated professional content, proxied from legislation-explorer.
for _r in (commentary_router, insolvency_router, nz_mtg_router):
    app.include_router(_r, dependencies=[Depends(require_user)])
app.include_router(account_router)


# MCP Streamable HTTP via FastMCP
# We add the route directly (not mount) so the FastMCP session manager
# can be run inside the app lifespan below.
from starlette.routing import Route as StarletteRoute

fastmcp_app = fastmcp.streamable_http_app()

# Extract the ASGI handler from the first route and wrap with auth middleware
_mcp_raw_handler = fastmcp_app.routes[0].app  # StreamableHTTPASGIApp.__call__
_mcp_handler = MCPAuthMiddleware(_mcp_raw_handler)
app.router.routes.insert(
    0,
    StarletteRoute("/mcp", endpoint=_mcp_handler, methods=["GET", "POST", "DELETE"]),
)
app.router.routes.insert(
    1,
    StarletteRoute("/mcp/{path:path}", endpoint=_mcp_handler, methods=["GET", "POST", "DELETE"]),
)


# Static files — serve ONLY the case-summary JSON the frontend fetches, not the
# whole scripts/ dir (which contains backup/probe/source files).
SUMMARIES_DIR = Path(__file__).parent.parent / "scripts" / "cleaned" / "summaries"
if SUMMARIES_DIR.exists():
    app.mount("/static/cleaned/summaries", StaticFiles(directory=str(SUMMARIES_DIR)), name="static_summaries")

# React app under /app
if FRONTEND_DIST.exists():
    app.mount("/app/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="app_assets")

    _DIST_ROOT = FRONTEND_DIST.resolve()
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

    @app.get("/app/{full_path:path}")
    def app_fallback(full_path: str, request: Request):
        # PWA manifest + favicons are public — the browser/OS fetches them before sign-in.
        # Top-level files only: "favicon/../x" must not count as a favicon.
        is_public_asset = "/" not in full_path and (
            full_path in ("manifest.webmanifest", "theme-init.js") or full_path.startswith("favicon"))
        if not is_public_asset and not getattr(request.state, "user", None):
            return RedirectResponse(url="/login.html", status_code=307)
        if full_path.startswith("register"):
            return JSONResponse({"error": "Not found"}, status_code=404)
        # full_path is attacker-controlled ("../..", "%2fetc%2fpasswd" arrive decoded):
        # resolve it and serve only what is still inside the build directory.
        # A NUL byte makes resolve()/stat() raise ValueError: treat it as "no such file".
        file_path = None if "\x00" in full_path else (FRONTEND_DIST / full_path).resolve()
        if file_path is not None and file_path.is_relative_to(_DIST_ROOT) and file_path.is_file():
            return FileResponse(file_path, headers={"Cache-Control": "public, max-age=3600"})
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index, headers=_NO_CACHE)
        return HTMLResponse("<h1>lawkitty</h1><p>Frontend not built yet.</p>")

    @app.get("/app")
    async def _app_root():
        return RedirectResponse(url="/app/", status_code=307)

# Legacy deep links from before the app moved to /app (old bookmarks, MCP results).
_LEGACY_EXACT = ("search", "settings", "maps", "definitions", "private-rulings")
_LEGACY_PREFIXES = (
    "tax-cases", "treaties", "rulings", "ato", "private-rulings",
    "itaa-1997", "itaa-1936", "gst-1999", "taa-1953", "nz-it-2007",
    "corporations-act-2001", "regulatory-guides",
    "bankruptcy-act-1966", "afsa-guides",
    # State tax Acts: the same root-level shortcut as every other Act, so a typed or
    # pasted "/qld-duties-2001/10" reaches the app instead of a 404.
    *STATE_TAX_ACTS,
)


def _legacy_redirect(request: Request):
    url = "/app" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return RedirectResponse(url=url, status_code=308)


for _p in _LEGACY_EXACT:
    app.add_api_route(f"/{_p}", _legacy_redirect, methods=["GET", "HEAD"], include_in_schema=False)
for _p in _LEGACY_PREFIXES:
    app.add_api_route(f"/{_p}/{{rest:path}}", _legacy_redirect, methods=["GET", "HEAD"], include_in_schema=False)

@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_redirect():
    """Admin now lives in the app at /app/admin — the old served HTML dashboard is closed."""
    return RedirectResponse(url="/app/admin", status_code=307)


# Marketing site at the root — registered LAST so it only catches paths the above and the API routes don't
SITE_DIR = BASE / "site"
if SITE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    # Custom 404 for the marketing site only; API/app 404s stay JSON / SPA.
    if exc.status_code == 404:
        p = request.url.path
        if not p.startswith(("/app", "/api", "/auth", "/mcp", "/static")):
            f = SITE_DIR / "404.html"
            if f.exists():
                return FileResponse(f, status_code=404)
    return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
