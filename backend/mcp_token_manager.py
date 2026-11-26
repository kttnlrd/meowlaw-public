"""MCP token storage and rate limiting."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from backend.db import cursor, safe
from backend.services.usage_log import REJECT_KINDS

# Rate limits
TOKEN_RPM = 300
GLOBAL_RPM = 3000

# Tier -> MCP calls per UTC day per owner (None = unlimited). Tiers not listed get no MCP.
# "trial" is the default new-user tier; MCP is free during beta, so new sign-ups can use it.
DAILY_CAPS: dict[str, int | None] = {"trial": 5000, "solo": 5000, "power": None}

_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Bucket:
    tokens: float
    last_update: float


_token_buckets: dict[str, Bucket] = {}
_global_bucket = Bucket(GLOBAL_RPM, time.time())


class TokenManager:
    def create_token(self, name: str = "", created_by: str = "") -> str:
        """Create a new named token associated with a user. Returns the raw token (shown once).

        Raises on DB error — never hand out a token that wasn't stored.
        """
        raw = secrets.token_hex(32)
        token_hash = _hash(raw)
        with cursor() as cur:
            cur.execute(
                "INSERT INTO mcp_tokens (token_hash, name, created_by, created_at) VALUES (%s, %s, %s, %s)",
                (token_hash, name.strip(), created_by.strip(), time.time()),
            )
        return raw

    @safe(False)  # fail closed
    def validate_token(self, token: str) -> bool:
        """Check if token is valid (exists and not revoked)."""
        with cursor() as cur:
            cur.execute("SELECT revoked_at FROM mcp_tokens WHERE token_hash = %s", (_hash(token),))
            row = cur.fetchone()
        return row is not None and row["revoked_at"] is None

    @safe(None)
    def owner_of(self, token: str) -> str | None:
        """created_by email of a live token, or None."""
        with cursor() as cur:
            cur.execute("SELECT created_by FROM mcp_tokens WHERE token_hash = %s AND revoked_at IS NULL", (_hash(token),))
            row = cur.fetchone()
        return row["created_by"] if row else None

    @safe(None)
    def owner_by_id_or_hash(self, token_hash_id: str) -> str | None:
        """created_by of a live token matched by hash or numeric id, or None."""
        with cursor() as cur:
            cur.execute(
                "SELECT created_by FROM mcp_tokens WHERE token_hash = %s AND revoked_at IS NULL",
                (_hash(token_hash_id),),
            )
            row = cur.fetchone()
            if row is None and token_hash_id.isdigit():
                cur.execute(
                    "SELECT created_by FROM mcp_tokens WHERE id = %s AND revoked_at IS NULL",
                    (int(token_hash_id),),
                )
                row = cur.fetchone()
        return row["created_by"] if row else None

    @safe("anonymous")
    def tier_for_email(self, email: str) -> str:
        """users.tier for an email; 'anonymous' if there's no such user."""
        with cursor() as cur:
            cur.execute("SELECT tier FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
        return (row["tier"] or "trial") if row else "anonymous"

    @safe(None)
    def record_use(self, token: str) -> None:
        """Increment request count and update last_used."""
        with cursor() as cur:
            cur.execute(
                "UPDATE mcp_tokens SET request_count = request_count + 1, last_used = %s WHERE token_hash = %s",
                (time.time(), _hash(token)),
            )

    def _update_by_hash_or_id(self, set_sql: str, value, hash_value: str, id_value: str) -> bool:
        """UPDATE a live token matched by hash, falling back to numeric id."""
        with cursor() as cur:
            cur.execute(
                f"UPDATE mcp_tokens SET {set_sql} = %s WHERE token_hash = %s AND revoked_at IS NULL",
                (value, hash_value),
            )
            if cur.rowcount == 0 and id_value.isdigit():
                cur.execute(
                    f"UPDATE mcp_tokens SET {set_sql} = %s WHERE id = %s AND revoked_at IS NULL",
                    (value, int(id_value)),
                )
            return cur.rowcount > 0

    @safe(False)
    def rename_token(self, token_hash_id: str, new_name: str) -> bool:
        """Rename a token by its hash string or id. Returns True if it existed."""
        return self._update_by_hash_or_id("name", new_name.strip(), token_hash_id, token_hash_id)

    @safe(False)
    def revoke_token(self, token: str) -> bool:
        """Revoke a token by its hash or numeric id. Returns True if it existed."""
        return self._update_by_hash_or_id("revoked_at", time.time(), _hash(token), token)

    @safe(list)
    def list_tokens(self, created_by: str = "") -> list[dict]:
        """List all non-revoked tokens (without hashes). Includes created_by.
        If created_by is provided, only returns tokens created by that user.
        """
        with cursor() as cur:
            cur.execute(
                """
                SELECT id, name, created_by, created_at, last_used, request_count
                FROM mcp_tokens
                WHERE revoked_at IS NULL AND (%(by)s = '' OR created_by = %(by)s)
                ORDER BY created_at DESC
                """,
                {"by": created_by},
            )
            rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "created_by": r["created_by"] or "",
                "created_at": r["created_at"],
                "last_used": r["last_used"],
                "request_count": r["request_count"],
            }
            for r in rows
        ]

    @safe(lambda: {"all_time": [], "monthly": [], "weekly": [], "daily": []})
    def hall_of_fame(self) -> dict:
        """Leaderboard of call counts by token name, over all-time/monthly/daily windows."""
        now = time.time()

        def top(since: float) -> list[dict]:
            with cursor() as cur:
                cur.execute(
                    """
                    SELECT token_id, MAX(token_name) AS token_name, COUNT(*) AS count FROM mcp_call_log
                    WHERE created_at >= %s AND token_id IS NOT NULL
                      AND (error_kind IS NULL OR error_kind <> ALL(%s)) GROUP BY token_id ORDER BY count DESC LIMIT 10
                    """,
                    (since, list(REJECT_KINDS)),
                )
                rows = cur.fetchall()
            return [
                {
                    "name": r["token_name"] or f"Token #{r['token_id']}",
                    "count": r["count"],
                    "token_id": r["token_id"],
                }
                for r in rows
            ]

        return {
            "all_time": top(0),
            "monthly": top(now - 30 * 86400),
            "weekly": top(now - 7 * 86400),
            "daily": top(now - 86400),
        }

    @safe({"used": 0, "cap": None, "reset_in": 0})
    def daily_usage(self, owner: str, tier: str = "trial") -> dict:
        """Calls used today (per owner), the daily cap, and seconds to UTC-midnight reset.

        Backed by mcp_call_log so the count survives restarts (persistent daily cap).
        Counted by the caller's email, not by token: OAuth-connector calls have no
        mcp_tokens row, so a join on the token left them outside the cap entirely.
        """
        now = time.time()
        day_start = now - (now % 86400)
        with cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM mcp_call_log l "
                "WHERE l.user_email = %s AND l.transport = 'mcp' AND l.created_at >= %s "
                "AND (l.error_kind IS NULL OR l.error_kind <> ALL(%s))",
                (owner, day_start, list(REJECT_KINDS)),
            )
            row = cur.fetchone()
            used = row["n"] if row else 0
        return {"used": used, "cap": DAILY_CAPS.get(tier), "reset_in": int(86400 - (now % 86400))}

    def check_rate_limit(self, token: str, owner: str = "", tier: str = "power") -> tuple[bool, str]:
        """
        Check tier access, daily cap, and rate limit for a token.
        Returns (allowed, reason). Reason "upgrade_required" means the tier has no MCP.
        """
        global _global_bucket
        now = time.time()
        if tier not in DAILY_CAPS:
            return False, "upgrade_required"
        cap = DAILY_CAPS[tier]

        if cap is not None and self.daily_usage(owner or token, tier)["used"] >= cap:
            return False, f"Daily MCP limit reached ({cap} calls/day on this plan)."

        with _lock:
            # Per-token bucket
            bucket = _token_buckets.get(token)
            if bucket is None:
                bucket = Bucket(TOKEN_RPM, now)
                _token_buckets[token] = bucket

            elapsed = now - bucket.last_update
            bucket.tokens = min(TOKEN_RPM, bucket.tokens + elapsed * (TOKEN_RPM / 60.0))
            bucket.last_update = now

            if bucket.tokens < 1:
                return False, f"Token rate limit exceeded ({TOKEN_RPM} req/min)"

            bucket.tokens -= 1

            # Global bucket
            elapsed_g = now - _global_bucket.last_update
            _global_bucket.tokens = min(GLOBAL_RPM, _global_bucket.tokens + elapsed_g * (GLOBAL_RPM / 60.0))
            _global_bucket.last_update = now

            if _global_bucket.tokens < 1:
                return False, f"Global rate limit exceeded ({GLOBAL_RPM} req/min)"

            _global_bucket.tokens -= 1

        self.record_use(token)
        # The call's mcp_call_log row is written by MCPAuthMiddleware (usage_log.record).
        return True, ""


token_manager = TokenManager()
