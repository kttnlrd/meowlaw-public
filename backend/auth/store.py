"""User + magic-link persistence for unified SSO auth.

Postgres (`lawkitty` DB, see backend/db.py) — tables `users`, `magic_tokens`.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.db import cursor, safe

MAGIC_TOKEN_TTL_MINUTES = 15


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Users ────────────────────────────────────────────────────────────────────


def upsert_user(email: str, name: str, provider: str, provider_subject: str | None) -> dict[str, Any]:
    """Insert-or-update a user by email (always lowercased).

    First insert: tier='trial', trial_started_at=now. Always updates
    last_login_at. A second provider login with an already-known email
    LINKS to the existing row rather than duplicating it.

    Raises on DB error — there is no sensible fake user to return.
    """
    email = email.strip().lower()
    now = now_iso()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO users
               (email, name, provider, provider_subject, tier, trial_started_at, last_login_at, created_at)
               VALUES (%s, %s, %s, %s, 'trial', %s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET
                   name = COALESCE(NULLIF(EXCLUDED.name, ''), users.name),
                   provider = EXCLUDED.provider,
                   provider_subject = COALESCE(NULLIF(EXCLUDED.provider_subject, ''), users.provider_subject),
                   last_login_at = EXCLUDED.last_login_at
               RETURNING *""",
            (email, name, provider, provider_subject, now, now, now),
        )
        return dict(cur.fetchone())


@safe(None)
def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── Magic tokens ─────────────────────────────────────────────────────────────


MAX_LIVE_TOKENS = 5    # outstanding sign-in emails per address
MAX_CODE_ATTEMPTS = 5  # wrong codes before an address's outstanding codes stop working
MAX_TOKENS_PER_DAY = 20  # sign-in emails per address per rolling 24h (caps code guessing at ~100/day)


def create_magic_token(email: str, remember: bool = False, next_url: str | None = None) -> tuple[str, str] | None:
    """Create a magic token + a short 6-digit code. Returns (token, code).

    Returns None when the address already has MAX_LIVE_TOKENS unexpired tokens: that
    bounds both how many emails a stranger can make us send to one inbox and how many
    6-digit codes are live to guess at once.
    """
    email = email.strip().lower()
    token = secrets.token_urlsafe(48)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    with cursor() as cur:
        # expires_at is always a UTC isoformat() string, so text comparison orders correctly.
        cur.execute("DELETE FROM magic_tokens WHERE expires_at < %s", ((now - timedelta(days=1)).isoformat(),))
        cur.execute(
            "SELECT COUNT(*) AS n FROM magic_tokens WHERE email = %s AND used = 0 AND expires_at > %s",
            (email, now.isoformat()),
        )
        if cur.fetchone()["n"] >= MAX_LIVE_TOKENS:
            return None
        # Rows are kept until a day after expiry (DELETE above), so every token created in
        # the last 24h is still here, used or not; created_at = expires_at - TTL.
        day_ago_expiry = now - timedelta(days=1) + timedelta(minutes=MAGIC_TOKEN_TTL_MINUTES)
        cur.execute(
            "SELECT COUNT(*) AS n FROM magic_tokens WHERE email = %s AND expires_at > %s",
            (email, day_ago_expiry.isoformat()),
        )
        if cur.fetchone()["n"] >= MAX_TOKENS_PER_DAY:
            return None
        cur.execute(
            "INSERT INTO magic_tokens (token, code, email, expires_at, used, remember, next_url) VALUES (%s, %s, %s, %s, 0, %s, %s)",
            (token, code, email, (now + timedelta(minutes=MAGIC_TOKEN_TTL_MINUTES)).isoformat(), int(remember), next_url),
        )
    return token, code


_CONSUME = ("UPDATE magic_tokens SET used = 1 WHERE {match} AND used = 0 AND expires_at > %s "
            "RETURNING email, remember, next_url")


def _consumed(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"email": row["email"], "remember": bool(row["remember"]), "next_url": row["next_url"]} if row else None


@safe(None)
def consume_magic_token_row(token: str) -> dict[str, Any] | None:
    """Single-use, 15-min TTL. Returns {email, remember, next_url} if valid, else None."""
    with cursor() as cur:
        # Atomic mark-used: two concurrent clicks can't both succeed.
        cur.execute(_CONSUME.format(match="token = %s"), (token, now_iso()))
        return _consumed(cur.fetchone())


@safe(None)
def consume_magic_token_by_code(email: str, code: str) -> dict[str, Any] | None:
    """Verify a 6-digit login code typed by the user (single-use, 15-min TTL).

    Every try, right or wrong, counts against all of the address's outstanding codes;
    past MAX_CODE_ATTEMPTS they stop working (the emailed link still does). Without
    this a 6-digit code could simply be guessed.
    """
    email = email.strip().lower()
    with cursor() as cur:
        cur.execute("UPDATE magic_tokens SET attempts = attempts + 1 WHERE email = %s AND used = 0", (email,))
        cur.execute(
            _CONSUME.format(match="email = %s AND code = %s AND attempts <= %s"),
            (email, code, MAX_CODE_ATTEMPTS, now_iso()),
        )
        return _consumed(cur.fetchone())


# ── Account management ───────────────────────────────────────────────────────

TRIAL_DAYS = 14


@safe(None)
def update_user_name(user_id: int, name: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("UPDATE users SET name = %s WHERE id = %s RETURNING *", (name.strip(), user_id))
        row = cur.fetchone()
        return dict(row) if row else None


@safe(False)
def delete_user(user_id: int) -> bool:
    """Delete the user row, their magic tokens, prefs, and revoke their MCP + OAuth tokens."""
    user = get_user_by_id(user_id)
    if not user:
        return False
    email = user["email"]

    from backend.mcp_token_manager import token_manager
    for t in token_manager.list_tokens(created_by=email):
        token_manager.revoke_token(str(t["id"]))
    # OAuth connector tokens too, so a later account with the same email does not inherit them.
    from backend.oauth_provider import provider as oauth_provider
    oauth_provider.revoke_all_for_user(email)

    with cursor() as cur:
        cur.execute("DELETE FROM user_prefs WHERE email = %s", (email,))
        cur.execute("DELETE FROM magic_tokens WHERE email = %s", (email,))
        cur.execute("DELETE FROM user_content_grants WHERE email = %s", (email.lower(),))
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return True


def trial_status(user: dict[str, Any]) -> dict[str, Any]:
    started = user.get("trial_started_at")
    if not started:
        return {"started_at": None, "days_left": 0, "expired": True}
    start = datetime.fromisoformat(started)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - start).days
    return {
        "started_at": started,
        "days_left": max(0, TRIAL_DAYS - elapsed),
        "expired": elapsed >= TRIAL_DAYS,
    }


# ── Admin ────────────────────────────────────────────────────────────────────

TIERS = {"trial", "solo", "power"}


@safe(list)
def list_users() -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]


def email_exists(email: str) -> bool:
    with cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE lower(email) = %s", (email.lower(),))
        return cur.fetchone() is not None


def update_user_tier(user_id: int, tier: str) -> dict[str, Any] | None:
    """Set a user's tier. Raises ValueError on an unknown tier; None if no such user."""
    if tier not in TIERS:
        raise ValueError(f"Invalid tier: {tier}")
    with cursor() as cur:
        cur.execute("UPDATE users SET tier = %s WHERE id = %s RETURNING *", (tier, user_id))
        row = cur.fetchone()
        return dict(row) if row else None
