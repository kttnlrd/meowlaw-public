"""Sign-in for MeowLaw.

Login methods: email magic link (clicked link or typed 6-digit code) and, when
configured, Google / Microsoft sign-in (backend/auth/providers/sso.py). All of
them end in the same place: a row in the `users` table (backend/auth/store.py,
keyed by email) and a signed JWT session cookie. First login auto-starts a trial.
(oauth_provider.py is a separate OAuth 2.1 *server* for MCP Connector auth, not
a login method.)
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from backend.auth import store
from authlib.integrations.starlette_client import OAuthError

from backend.auth.providers import magic, sso
from backend.config import ADMIN_EMAILS, IS_PRODUCTION, is_admin_email

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

# Signs every session JWT (and the consent/session state in main.py and
# oauth_provider.py) — fail closed: never fall back to a guessable key.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if len(SESSION_SECRET) < 32:
    if IS_PRODUCTION:
        raise RuntimeError(
            "SESSION_SECRET is missing or shorter than 32 characters; refusing to start "
            "with an https APP_BASE_URL. Set a random SESSION_SECRET of at least 32 characters."
        )
    SESSION_SECRET = secrets.token_urlsafe(48)
    logger.warning(
        "SESSION_SECRET missing or shorter than 32 characters (APP_BASE_URL is not a production "
        "https URL): using a random per-process secret — sessions will not survive a restart."
    )
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://lawkitty.app")

# Dev-only escape hatch: DEV_MODE=true on a LOCAL (non-production) instance makes
# the web data API browsable without a session. Fail-closed — never honored in
# production (https non-local APP_BASE_URL), so the gate cannot be left open.
AUTH_DISABLED = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes") and not IS_PRODUCTION


def _synthetic_user() -> dict[str, Any]:
    """Admin-level identity used when AUTH_DISABLED — no DB row, email matches ADMIN_EMAILS."""
    return {
        "id": None,
        "sub": "",
        "name": "dev",
        "email": min(ADMIN_EMAILS, default="dev@example.invalid"),
        "tier": "trial",
    }


# ── Session token helpers ────────────────────────────────────────────────────

SESSION_MAX_AGE = 28800  # 8 hours
REMEMBER_MAX_AGE = 2592000  # 30 days ("Keep me signed in")


def create_session_token(claims: dict[str, Any], remember: bool = False) -> str:
    """Create a signed JWT session token (expiry matches the cookie's max_age)."""
    max_age = REMEMBER_MAX_AGE if remember else SESSION_MAX_AGE
    payload = {
        **claims,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=max_age),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> dict[str, Any] | None:
    """Decode a signed JWT session token. Returns None if invalid/expired."""
    try:
        return jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except Exception:
        return None


# ── Shared login/callback helpers ───────────────────────────────────────────


def _session_claims(user: dict[str, Any]) -> dict[str, Any]:
    """The claims signed into every session JWT (same shape for both login paths)."""
    return {
        "id": user["id"],
        "sub": "",
        "name": user.get("name", ""),
        "email": user["email"],
        "tier": user.get("tier", "trial"),
    }


def _set_session_cookie(response: Any, session_token: str, remember: bool = False) -> None:
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=REMEMBER_MAX_AGE if remember else SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


_SAFE_NEXT_RE = re.compile(r"/(?![/\\])[^\x00-\x20\x7f\\]*")


def _safe_next(next_url: Any) -> str | None:
    """Only same-site relative paths ("/app", not "//evil.com" or "/\\evil.com").

    Control characters, spaces and backslashes are refused anywhere: browsers strip tab/CR/LF
    before parsing, so "/\\t/evil.com" would otherwise become "//evil.com" and leave the site.
    """
    if isinstance(next_url, str) and _SAFE_NEXT_RE.fullmatch(next_url):
        return next_url
    return None


async def _json_body(request: Request) -> dict[str, Any]:
    """The request's JSON object, or {} for anything else (bad JSON, a list, a string)."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _str_field(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


def _login_response(row: dict[str, Any], response: Any, provider: str = "magic",
                    name: str = "", subject: str | None = None) -> Any:
    """Shared tail of every login path: upsert the user, log the login, set the cookie.

    Raises on DB error (callers turn that into their own failure response).
    """
    user = store.upsert_user(email=row["email"], name=name, provider=provider, provider_subject=subject)
    from backend.services.login_log import log_login
    log_login(user["email"], user.get("name", ""))
    _set_session_cookie(response, create_session_token(_session_claims(user), row["remember"]), row["remember"])
    return response


async def request_magic(request: Request) -> JSONResponse:
    """POST {email, remember?, next?} -> always 202, to avoid account enumeration."""
    body = await _json_body(request)
    email = _str_field(body, "email").lower()
    sent = False
    if email:
        try:
            sent = magic.request_magic_link(
                email, remember=body.get("remember") is True, next_url=_safe_next(body.get("next")),
            )
        except Exception:
            logger.exception("Failed to send magic link to %s", email)
    return JSONResponse({"status": "ok", "sent": sent}, status_code=202)


async def callback_magic(request: Request) -> RedirectResponse:
    """GET ?token=... -> verify token, upsert_user, set session cookie, redirect."""
    token = request.query_params.get("token", "")
    row = magic.consume(token) if token else None
    if not row:
        return RedirectResponse(url="/?error=auth_failed", status_code=303)
    try:
        # next_url was validated by _safe_next when stored; re-check on the way out.
        return _login_response(row, RedirectResponse(url=_safe_next(row.get("next_url")) or "/app", status_code=303))
    except Exception:
        logger.exception("Magic callback upsert failed")
        return RedirectResponse(url="/?error=auth_failed", status_code=303)


async def verify_magic_code(request: Request) -> JSONResponse:
    """POST {email, code} -> verify a typed login code, set the session cookie in THIS browser."""
    body = await _json_body(request)
    email = _str_field(body, "email").lower()
    code = _str_field(body, "code")
    if not email or not code:
        return JSONResponse({"error": "Missing email or code"}, status_code=400)
    row = magic.consume_by_code(email, code)
    if not row:
        return JSONResponse({"error": "Invalid or expired code"}, status_code=400)
    try:
        return _login_response(row, JSONResponse({"status": "ok", "next": _safe_next(row.get("next_url")) or "/app"}))
    except Exception:
        logger.exception("Magic code upsert failed")
        return JSONResponse({"error": "Sign-in failed"}, status_code=500)


# ── Google / Microsoft sign-in ───────────────────────────────────────────────


async def sso_providers(request: Request) -> JSONResponse:
    """GET -> the sign-in providers configured on this instance (the login page draws its buttons from this)."""
    return JSONResponse({"providers": [{"id": k, "label": v} for k, v in sso.LABELS.items()]})


async def login_sso(request: Request) -> Any:
    """GET /auth/login/{provider}?next=&remember=1 -> redirect to the provider."""
    provider = request.path_params["provider"]
    client = sso.client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail="Unknown sign-in provider")
    # Carried in the signed starlette session cookie across the round trip.
    request.session["sso"] = {
        "next": _safe_next(request.query_params.get("next")),
        "remember": request.query_params.get("remember") == "1",
    }
    try:
        # Built from APP_BASE_URL, not the request: behind the tunnel the request's scheme/host can differ.
        return await client.authorize_redirect(request, f"{APP_BASE_URL}/auth/callback/{provider}")
    except Exception:
        logger.exception("SSO redirect to %s failed", provider)
        return RedirectResponse(url="/login.html?error=sso_unavailable", status_code=303)


async def callback_sso(request: Request) -> RedirectResponse:
    """GET /auth/callback/{provider} -> validate the ID token, upsert_user, set session cookie."""
    provider = request.path_params["provider"]
    client = sso.client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail="Unknown sign-in provider")
    ctx = request.session.pop("sso", None) or {}
    try:
        token = await client.authorize_access_token(request, **sso.token_kwargs(provider))
        identity = sso.verified_identity(provider, dict(token.get("userinfo") or {}))
    except (OAuthError, sso.SSOError) as e:
        # Includes a cancelled consent, a replayed/forged state and an unverified email.
        logger.warning("SSO sign-in via %s refused: %s", provider, e)
        return RedirectResponse(url="/login.html?error=sso_failed", status_code=303)
    except Exception:
        logger.exception("SSO callback from %s failed", provider)
        return RedirectResponse(url="/login.html?error=sso_failed", status_code=303)

    row = {"email": identity["email"], "remember": ctx.get("remember") is True}
    try:
        return _login_response(
            row, RedirectResponse(url=_safe_next(ctx.get("next")) or "/app", status_code=303),
            provider=provider, name=identity["name"], subject=identity["subject"],
        )
    except Exception:
        logger.exception("SSO upsert failed")
        return RedirectResponse(url="/login.html?error=sso_failed", status_code=303)


async def logout(request: Request) -> RedirectResponse:
    """Clear the session cookie and redirect home."""
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session", path="/")
    return response


async def me(request: Request) -> JSONResponse:
    """Return the current user's info if authenticated."""
    if AUTH_DISABLED:
        u = _synthetic_user()
        return JSONResponse({
            "id": None,
            "email": u["email"],
            "name": "dev",
            "tier": u["tier"],
            "trial_started_at": None,
            "authenticated": True,
            "is_admin": True,
        })
    session = getattr(request.state, "user", None)
    if not session:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    user = store.get_user_by_id(session.get("id")) if session.get("id") is not None else None
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    return JSONResponse({
        "id": user["id"],
        "email": user["email"],
        "name": user.get("name", ""),
        "tier": user.get("tier", "trial"),
        "trial_started_at": user.get("trial_started_at"),
        "authenticated": True,
        "is_admin": is_admin_email(user["email"]),
    })


# ── Middleware ────────────────────────────────────────────────────────────────


# Published ATO/ASIC documents, served by their download routes with no session.
_PUBLIC_DOWNLOAD_RE = re.compile(
    r"^/api/(ruling|private-ruling|regulatory-guide|afsa-guide|tax-cases/case)/[^/].*/download$"
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Populate `request.state.user` from the session cookie (None when absent/invalid).

    This never blocks a request: routes are gated by the `require_user` /
    `require_admin` dependencies, and anything not gated is public.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if AUTH_DISABLED:  # dev mode: everyone is an admin
            request.state.user = _synthetic_user()
        else:
            token = request.cookies.get("session")
            request.state.user = decode_session_token(token) if token else None
        return await call_next(request)


# ── Helper: get current user in route handlers ─────────────────────────────


def require_user(request: Request) -> dict[str, Any]:
    """Published documents may be downloaded without a session; everything else needs one."""
    if AUTH_DISABLED:
        return _synthetic_user()
    if request.method in ("GET", "HEAD") and _PUBLIC_DOWNLOAD_RE.match(request.url.path):
        return {"email": "public-download", "public": True}
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    """The one admin gate: a signed-in session whose email is in ADMIN_EMAILS."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not is_admin_email(user.get("email")):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
