"""OAuth 2.1 authorization server provider for MCP.

Implements the MCP OAuthAuthorizationServerProvider protocol so that
Claude Desktop Connectors can authenticate via OAuth 2.1.

Flow:
  1. /.well-known/oauth-authorization-server — discovery
  2. /oauth/authorize — signed-in user (magic-link session) approves on a consent page
  3. /oauth/token — exchange auth code for access/refresh tokens
  4. /oauth/register — dynamic client registration (RFC 7591)

Tokens issued here are validated by MCPAuthMiddleware alongside static tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get(
    "OAUTH_BASE_URL",
    "https://lawkitty.app",
)
from backend.config import BASE

DB_DIR = os.environ.get("OAUTH_DB_DIR", str(BASE))
DB_PATH = os.path.join(DB_DIR, "oauth_state.db")
from backend.auth import SESSION_SECRET  # validated there: fails closed, never "change-me"

# OAuth token lifetimes
AUTHORIZATION_CODE_TTL = 600       # 10 minutes
ACCESS_TOKEN_TTL = 3600            # 1 hour
REFRESH_TOKEN_TTL = 2592000         # 30 days
MAX_REDIRECT_URIS = 10

# ── DB helpers ────────────────────────────────────────────────────────────────


@contextmanager
def _db():
    """Connection in a transaction: commits on success, rolls back on error, always closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret TEXT,
    client_name TEXT DEFAULT '',
    redirect_uris TEXT DEFAULT '[]',
    grant_types TEXT DEFAULT '["authorization_code","refresh_token"]',
    response_types TEXT DEFAULT '["code"]',
    token_endpoint_auth_method TEXT DEFAULT 'none',
    scope TEXT DEFAULT 'mcp',
    created_at REAL NOT NULL,
    client_id_issued_at INTEGER,
    client_secret_expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT,
    code_challenge_method TEXT,
    user_email TEXT NOT NULL,
    user_name TEXT DEFAULT '',
    scopes TEXT DEFAULT 'mcp',
    expires_at REAL NOT NULL,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token_hash TEXT PRIMARY KEY,
    token_prefix TEXT NOT NULL,
    client_id TEXT NOT NULL,
    user_email TEXT NOT NULL,
    user_name TEXT DEFAULT '',
    scopes TEXT DEFAULT 'mcp',
    expires_at REAL NOT NULL,
    refresh_token_hash TEXT,
    revoked INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    token_prefix TEXT NOT NULL,
    client_id TEXT NOT NULL,
    user_email TEXT NOT NULL,
    user_name TEXT DEFAULT '',
    scopes TEXT DEFAULT 'mcp',
    expires_at REAL NOT NULL,
    access_token_hash TEXT,
    revoked INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_client_id ON oauth_authorization_codes(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_token_client ON oauth_access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_token_user ON oauth_access_tokens(user_email);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_token ON oauth_refresh_tokens(token_hash);
"""


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(_SQL_SCHEMA)


def _pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ── OAuth provider implementation ────────────────────────────────────────────


class OAuthProvider:
    """Implements MCP's OAuthAuthorizationServerProvider protocol.

    Each method maps to a protocol method from the MCP SDK.
    We use our own class (not subclassing the protocol) for simpler
    dependency handling — the protocol is duck-typed.
    """

    def __init__(self) -> None:
        _init_db()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _hash(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _gen_token(self, prefix: str = "mcp_") -> str:
        return prefix + secrets.token_urlsafe(32)

    def _gen_code(self) -> str:
        return secrets.token_urlsafe(32)

    def _gen_client_id(self) -> str:
        return "lk_" + secrets.token_urlsafe(16)

    # ── Client management ──────────────────────────────────────────────────

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        """Retrieve a registered client by client_id."""
        with _db() as conn:
            row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return dict(row) if row else None

    def register_client(self, client_metadata: dict[str, Any]) -> dict[str, Any]:
        """Register a new OAuth client (RFC 7591 dynamic registration).

        This endpoint is unauthenticated, so everything in `client_metadata` is
        untrusted: only redirect_uris and client_name are taken from it. Clients are
        public (token_endpoint_auth_method "none") and must use PKCE.
        """
        if not isinstance(client_metadata, dict):
            raise ValueError("client metadata must be a JSON object")
        redirect_uris = client_metadata.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ValueError("redirect_uris is required")
        if len(redirect_uris) > MAX_REDIRECT_URIS:
            raise ValueError(f"at most {MAX_REDIRECT_URIS} redirect_uris")
        for uri in redirect_uris:
            if not isinstance(uri, str) or len(uri) > 2000 or not _is_allowed_redirect_uri(uri):
                raise ValueError("each redirect_uri must be an https:// or loopback http:// URL")
        client_name = client_metadata.get("client_name")
        client_name = client_name[:200] if isinstance(client_name, str) else ""

        client_id = self._gen_client_id()
        now = int(time.time())
        with _db() as conn:
            # Cheap global cap on this unauthenticated dynamic-registration endpoint.
            recent = conn.execute(
                "SELECT COUNT(*) FROM oauth_clients WHERE created_at > ?", (time.time() - 86400,)
            ).fetchone()[0]
            if recent >= 500:
                raise ValueError("Registration temporarily unavailable")
            conn.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at, client_id_issued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (client_id, client_name, json.dumps(redirect_uris), time.time(), now),
            )
        return {
            "client_id": client_id,
            "client_id_issued_at": now,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp",
        }

    # ── Authorization code flow ────────────────────────────────────────────

    def create_authorization_code(
        self,
        client_id: str,
        redirect_uri: str,
        user_email: str,
        code_challenge: str,
        user_name: str = "",
        scopes: str = "mcp",
    ) -> str:
        """Generate and store an authorization code (always PKCE S256-bound)."""
        code = self._gen_code()
        with _db() as conn:
            conn.execute(
                """INSERT INTO oauth_authorization_codes
                   (code, client_id, redirect_uri, code_challenge, code_challenge_method,
                    user_email, user_name, scopes, expires_at)
                   VALUES (?, ?, ?, ?, 'S256', ?, ?, ?, ?)""",
                (code, client_id, redirect_uri, code_challenge, user_email, user_name, scopes,
                 time.time() + AUTHORIZATION_CODE_TTL),
            )
        return code

    def validate_authorization_code(
        self,
        client_id: str,
        code: str,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> dict[str, Any] | None:
        """Validate and consume an authorization code. Returns code data or None.

        The code is consumed atomically before anything else is checked, so it can be
        redeemed once at most and a failed attempt burns it.
        """
        with _db() as conn:
            row = conn.execute(
                "UPDATE oauth_authorization_codes SET used = 1 "
                "WHERE code = ? AND client_id = ? AND used = 0 AND expires_at > ? RETURNING *",
                (code, client_id, time.time()),
            ).fetchone()
        if not row:
            return None
        code_data = dict(row)
        # redirect_uri is optional at the token endpoint (OAuth 2.1), but if sent it must match.
        if redirect_uri and redirect_uri != code_data["redirect_uri"]:
            return None
        # PKCE is mandatory; a code stored without a challenge (pre-upgrade) is refused.
        challenge = code_data["code_challenge"]
        if not challenge or not isinstance(code_verifier, str) or not code_verifier:
            return None
        if not hmac.compare_digest(_pkce_s256(code_verifier), challenge):
            return None
        return code_data

    # ── Token management ───────────────────────────────────────────────────

    def _store_token(self, table: str, link_col: str, raw: str, ttl: int, client_id: str,
                     user_email: str, user_name: str, scopes: str, linked: str | None) -> None:
        with _db() as conn:
            conn.execute(
                f"INSERT INTO {table} (token_hash, token_prefix, client_id, user_email, user_name, "
                f"scopes, expires_at, {link_col}, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._hash(raw), raw[:12], client_id, user_email, user_name, scopes,
                 time.time() + ttl, self._hash(linked) if linked else None, time.time()),
            )

    def create_access_token(
        self,
        client_id: str,
        user_email: str,
        user_name: str = "",
        scopes: str = "mcp",
        refresh_token_value: str | None = None,
    ) -> str:
        """Generate and store an access token. Returns the raw token."""
        raw = self._gen_token()
        self._store_token("oauth_access_tokens", "refresh_token_hash", raw, ACCESS_TOKEN_TTL,
                          client_id, user_email, user_name, scopes, refresh_token_value)
        return raw

    def create_refresh_token(
        self,
        client_id: str,
        user_email: str,
        user_name: str = "",
        scopes: str = "mcp",
        access_token_value: str | None = None,
    ) -> str:
        """Generate and store a refresh token. Returns the raw token."""
        raw = self._gen_token("mcp_ref_")
        self._store_token("oauth_refresh_tokens", "access_token_hash", raw, REFRESH_TOKEN_TTL,
                          client_id, user_email, user_name, scopes, access_token_value)
        return raw

    def issue_token_pair(self, client_id: str, user_email: str, user_name: str, scopes: str) -> dict[str, Any]:
        """A fresh access + refresh token pair, as the token-endpoint response body."""
        access = self.create_access_token(client_id, user_email, user_name, scopes)
        refresh = self.create_refresh_token(client_id, user_email, user_name, scopes, access_token_value=access)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": refresh,
            "scope": scopes,
        }

    def load_access_token(self, token: str) -> dict[str, Any] | None:
        """Validate an access token and return its data."""
        if not isinstance(token, str) or not token:
            return None
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_access_tokens WHERE token_hash = ? AND revoked = 0 AND expires_at > ?",
                (self._hash(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def exchange_refresh_token(self, refresh_token: str, client_id: str = "") -> dict[str, Any] | None:
        """Validate and rotate a refresh token. Returns new token pair data.

        Rotation is atomic (a refresh token redeems once), and a token presented by a
        different client than the one it was issued to is refused.
        """
        with _db() as conn:
            row = conn.execute(
                "UPDATE oauth_refresh_tokens SET revoked = 1 "
                "WHERE token_hash = ? AND revoked = 0 AND expires_at > ? RETURNING *",
                (self._hash(refresh_token), time.time()),
            ).fetchone()
        if not row or (client_id and client_id != row["client_id"]):
            return None
        return self.issue_token_pair(row["client_id"], row["user_email"], row["user_name"], row["scopes"])

    def revoke_token(self, token: str) -> None:
        """Revoke an access or refresh token."""
        token_hash = self._hash(token)
        with _db() as conn:
            conn.execute("UPDATE oauth_access_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
            conn.execute("UPDATE oauth_refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))

    def revoke_all_for_user(self, user_email: str) -> None:
        """Revoke all tokens for a given user (called when the account is deleted)."""
        with _db() as conn:
            conn.execute("UPDATE oauth_access_tokens SET revoked = 1 WHERE user_email = ?", (user_email,))
            conn.execute("UPDATE oauth_refresh_tokens SET revoked = 1 WHERE user_email = ?", (user_email,))


# ── Singletons ────────────────────────────────────────────────────────────────

provider = OAuthProvider()


# ── Route helpers ─────────────────────────────────────────────────────────────


def get_oauth_metadata() -> dict[str, Any]:
    """Return OAuth 2.1 authorization server metadata (RFC 8414)."""
    return {
        "issuer": f"{BASE_URL}",
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "revocation_endpoint": f"{BASE_URL}/oauth/revoke",
    }


async def handle_well_known(request: Request) -> JSONResponse:
    """GET /.well-known/oauth-authorization-server"""
    return JSONResponse(get_oauth_metadata())


RESOURCE_METADATA_URL = f"{BASE_URL}/.well-known/oauth-protected-resource/mcp"


async def handle_protected_resource(request: Request) -> JSONResponse:
    """GET /.well-known/oauth-protected-resource[/mcp] (RFC 9728): where an MCP client that was
    given only the connector link (https://<host>/mcp) finds this server's sign-in."""
    return JSONResponse({
        "resource": f"{BASE_URL}/mcp",
        "authorization_servers": [BASE_URL],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
        "resource_name": "lawkitty",
    })


async def handle_register(request: Request) -> JSONResponse:
    """POST /oauth/register — dynamic client registration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    try:
        client_info = provider.register_client(body)
    except ValueError as e:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": str(e)}, status_code=400)

    return JSONResponse(client_info, status_code=201)


def _is_allowed_redirect_uri(uri: str) -> bool:
    """Dynamic registration may only target https:// or loopback http:// URIs."""
    try:
        parts = urlsplit(uri)
    except Exception:
        return False
    if parts.scheme == "https" and parts.netloc:
        return True
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    return False


_AUTHORIZE_PARAMS = ("client_id", "redirect_uri", "response_type", "state",
                     "code_challenge", "code_challenge_method", "scope")


def _with_params(redirect_uri: str, params: dict[str, Any]) -> str:
    """Append query params to a registered redirect_uri that may already carry a query."""
    return f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(params)}"


def _session_user(request: Request) -> dict[str, Any] | None:
    """The signed-in user (session cookie) with a non-empty email, else None."""
    from backend.auth import decode_session_token
    token = request.cookies.get("session")
    user = decode_session_token(token) if token else None
    return user if user and user.get("email") else None


def _sign_state(payload: dict[str, Any]) -> str:
    """HMAC-sign a consent payload so the POST-back cannot be forged (CSRF)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=") + "." + sig


def _verify_state(token: str) -> dict[str, Any] | None:
    """Verify a consent token's signature and expiry; return its payload or None."""
    try:
        b64, sig = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        expected = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def _consent_page(token: str, client_name: str) -> str:
    """One-button consent page; POSTs the signed token back to the consent endpoint."""
    name = html.escape(client_name or "An application")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Authorise access</title></head><body>"
        "<main style='max-width:32rem;margin:4rem auto;font-family:system-ui,sans-serif'>"
        "<h1>Authorise access</h1>"
        f"<p><strong>{name}</strong> is requesting access to your LawKitty MCP tools.</p>"
        "<form method='post' action='/oauth/authorize/consent'>"
        f"<input type='hidden' name='consent_token' value=\"{html.escape(token)}\">"
        "<button type='submit' style='padding:.6rem 1.2rem;font-size:1rem'>Authorise</button>"
        "</form></main></body></html>"
    )


async def handle_authorize(request: Request) -> Response:
    """GET /oauth/authorize — authorization endpoint.

    1. Validate client/redirect_uri/PKCE. Pre-validation failures return JSON
       (never redirect to an unverified redirect_uri — RFC 6749 §4.1.2.1).
    2. Require a signed-in session, then render a one-button consent page.
    3. The code is issued only by POST /oauth/authorize/consent.
    """
    # Parse required params
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    response_type = request.query_params.get("response_type")
    state = request.query_params.get("state")
    code_challenge = request.query_params.get("code_challenge")
    code_challenge_method = request.query_params.get("code_challenge_method", "S256")
    scope = request.query_params.get("scope", "mcp")

    # Validate
    errors = []
    if not client_id:
        errors.append("client_id is required")
    if not redirect_uri:
        errors.append("redirect_uri is required")
    if response_type != "code":
        errors.append("response_type must be 'code'")

    if errors:
        # Do NOT redirect to the unverified redirect_uri.
        return JSONResponse(
            {"error": "invalid_request", "error_description": " ".join(errors)},
            status_code=400,
        )

    # Validate client exists
    client = provider.get_client(client_id)
    if not client:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "Unknown client"},
            status_code=400,
        )

    # Validate redirect URI is registered for this client
    registered_uris = json.loads(client.get("redirect_uris", "[]"))
    if redirect_uri not in registered_uris:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri not registered"},
            status_code=400,
        )

    # PKCE is mandatory and S256 only (OAuth 2.1; clients here are public, so the
    # code_verifier is the only thing binding a code to the client that asked for it).
    # Answer with a 400, not a redirect: /oauth/register is open, so a "registered"
    # redirect_uri can be anyone's site, and an automatic error redirect would turn
    # /oauth/authorize (a same-site `next` target) into an open redirect. Real MCP
    # clients always send PKCE; the only redirect left needs the user's consent click.
    if not code_challenge or code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": "PKCE is required: send code_challenge with code_challenge_method=S256"},
            status_code=400,
        )

    # Require a signed-in session
    user = _session_user(request)
    if not user:
        # Back here after sign-in. `next` must be a same-site relative path (auth._safe_next).
        next_params = {k: v for k, v in request.query_params.items() if k in _AUTHORIZE_PARAMS}
        next_url = f"/oauth/authorize?{urlencode(next_params)}"
        return RedirectResponse(url=f"/login.html?{urlencode({'next': next_url})}", status_code=303)
    user_email = user["email"]

    # Signed-in: render the consent page (code is issued only on the POST).
    consent = _sign_state({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
        "code_challenge": code_challenge,
        "user": user_email,
        "exp": time.time() + AUTHORIZATION_CODE_TTL,
    })
    return HTMLResponse(_consent_page(consent, client.get("client_name", "")))


async def handle_token(request: Request) -> JSONResponse:
    """POST /oauth/token — exchange authorization code or refresh token."""
    try:
        body = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    # Form fields can be file uploads; anything that is not a string is treated as absent.
    body = {k: v for k, v in body.items() if isinstance(v, str)}
    grant_type = body.get("grant_type")
    client_id = body.get("client_id", "")

    if grant_type == "authorization_code":
        code = body.get("code")
        code_verifier = body.get("code_verifier")
        redirect_uri = body.get("redirect_uri")

        if not code:
            return JSONResponse({"error": "invalid_request", "error_description": "code is required"}, status_code=400)
        if not client_id:
            return JSONResponse({"error": "invalid_request", "error_description": "client_id is required"}, status_code=400)

        # Validate the authorization code
        code_data = provider.validate_authorization_code(
            client_id=client_id,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        if not code_data:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}, status_code=400)

        return JSONResponse(provider.issue_token_pair(
            client_id, code_data["user_email"], code_data["user_name"], code_data["scopes"]))

    elif grant_type == "refresh_token":
        refresh_token_value = body.get("refresh_token")
        if not refresh_token_value:
            return JSONResponse({"error": "invalid_request", "error_description": "refresh_token is required"}, status_code=400)

        result = provider.exchange_refresh_token(refresh_token_value, client_id)
        if not result:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired refresh token"}, status_code=400)

        return JSONResponse(result)

    else:
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": f"Grant type '{grant_type}' not supported"},
            status_code=400,
        )


async def handle_revoke(request: Request) -> JSONResponse:
    """POST /oauth/revoke — revoke an access or refresh token."""
    try:
        body = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    token = body.get("token")
    if not isinstance(token, str) or not token:
        return JSONResponse({"error": "invalid_request", "error_description": "token is required"}, status_code=400)

    provider.revoke_token(token)
    return JSONResponse({}, status_code=200)


async def handle_authorize_consent(request: Request) -> Response:
    """POST /oauth/authorize/consent — user approved; issue the authorization code.

    The HMAC-signed ``consent_token`` (bound to the signed-in user) is the CSRF
    check: a token minted for another user's session is rejected.
    """
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    consent_token = form.get("consent_token")
    payload = _verify_state(consent_token) if isinstance(consent_token, str) else None
    if not payload:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Invalid or expired consent token"},
            status_code=400,
        )

    # The session must still be signed in as the same user that consented.
    user = _session_user(request)
    if not user:
        return JSONResponse(
            {"error": "access_denied", "error_description": "Not authenticated"},
            status_code=401,
        )
    user_email = user["email"]
    if user_email != payload.get("user"):
        return JSONResponse(
            {"error": "access_denied", "error_description": "Consent does not match session"},
            status_code=403,
        )

    client_id = payload.get("client_id")
    redirect_uri = payload.get("redirect_uri")
    scope = payload.get("scope") or "mcp"
    client_state = payload.get("state")
    code_challenge = payload.get("code_challenge")
    if not code_challenge:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "PKCE code_challenge is required"},
            status_code=400,
        )

    # Re-validate client + redirect_uri — never trust the token blindly.
    client = provider.get_client(client_id) if client_id else None
    if not client:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "Unknown client"},
            status_code=400,
        )
    registered_uris = json.loads(client.get("redirect_uris", "[]"))
    if redirect_uri not in registered_uris:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri not registered"},
            status_code=400,
        )

    code = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        user_email=user_email,
        code_challenge=code_challenge,
        user_name=user.get("name", ""),
        scopes=scope,
    )

    result_params = {"code": code}
    if client_state:
        result_params["state"] = client_state
    if scope:
        result_params["scope"] = scope
    return RedirectResponse(url=_with_params(redirect_uri, result_params), status_code=303)
