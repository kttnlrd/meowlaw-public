"""User preferences service — Postgres-backed per-user settings (`user_prefs` table)."""
from __future__ import annotations

import time
from typing import Any

from backend.db import cursor, safe

# Default preferences
DEFAULT_PREFS: dict[str, Any] = {
    "display_name": "",
    "default_act": "itaa-1997",
    "theme": "dark",
    "accent_color": "#c6f432",
    "text_color": "#aebec2",
    "bg_color": "#0a1214",
    "heading_font": "Montserrat",
    "body_font": "Lora",
}

_UPSERT = """
INSERT INTO user_prefs (email, display_name, default_act, theme,
    accent_color, text_color, bg_color, heading_font, body_font, updated_at)
VALUES (%(email)s, %(display_name)s, %(default_act)s, %(theme)s, %(accent_color)s,
    %(text_color)s, %(bg_color)s, %(heading_font)s, %(body_font)s, %(updated_at)s)
ON CONFLICT (email) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    default_act = EXCLUDED.default_act,
    theme = EXCLUDED.theme,
    accent_color = EXCLUDED.accent_color,
    text_color = EXCLUDED.text_color,
    bg_color = EXCLUDED.bg_color,
    heading_font = EXCLUDED.heading_font,
    body_font = EXCLUDED.body_font,
    updated_at = EXCLUDED.updated_at
"""


def get_prefs(email: str) -> dict[str, Any]:
    """Get a user's preferences, merged with defaults for any missing fields.

    DB outage degrades to defaults.
    """
    row = _fetch(email)
    if not row:
        return {**DEFAULT_PREFS, "email": email}
    return {"email": row["email"], **{k: row[k] or v for k, v in DEFAULT_PREFS.items()}}


@safe(None)
def _fetch(email: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM user_prefs WHERE email = %s", (email,))
        return cur.fetchone()


def _save(prefs: dict[str, Any]) -> None:
    """Raises on DB error — a failed save must not look like success."""
    with cursor() as cur:
        cur.execute(_UPSERT, prefs)


def update_prefs(email: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update a user's preferences (partial merge) and return the full prefs."""
    current = get_prefs(email)
    current.update(updates)
    current["updated_at"] = time.time()

    # Any non-empty string is accepted — the frontend theme registry is the
    # source of truth for valid themes; the backend just stores what it's given.
    if not current.get("theme"):
        current["theme"] = DEFAULT_PREFS["theme"]

    _save(current)
    return get_prefs(email)


def reset_prefs(email: str) -> dict[str, Any]:
    """Reset a user's preferences to defaults."""
    _save({**DEFAULT_PREFS, "email": email, "updated_at": time.time()})
    return get_prefs(email)


@safe(list)
def get_all_prefs() -> list[dict[str, Any]]:
    """Get all users' preferences (admin only)."""
    with cursor() as cur:
        cur.execute("SELECT * FROM user_prefs ORDER BY updated_at DESC")
        return [dict(r) for r in cur.fetchall()]
