"""Shared Postgres connection for user data (users, magic tokens, prefs, MCP tokens).

DB: `lawkitty` on the local Postgres, URL from LAWKITTY_DATABASE_URL.
"""
from __future__ import annotations

import functools
import logging
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("LAWKITTY_DATABASE_URL", "")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    provider TEXT,
    provider_subject TEXT,
    tier TEXT DEFAULT 'trial',
    trial_started_at TEXT,
    last_login_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS magic_tokens (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    remember INTEGER DEFAULT 0,
    next_url TEXT
);
ALTER TABLE magic_tokens ADD COLUMN IF NOT EXISTS next_url TEXT;
-- Typed login code, consumed by POST /auth/login/magic/verify. Without this a fresh
-- database has no `code` column and sign-in by code fails.
ALTER TABLE magic_tokens ADD COLUMN IF NOT EXISTS code TEXT;
-- Wrong-code counter (backend/auth/store.py: MAX_CODE_ATTEMPTS).
ALTER TABLE magic_tokens ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS user_prefs (
    email TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    default_act TEXT DEFAULT 'itaa-1997',
    theme TEXT DEFAULT 'dark',
    accent_color TEXT DEFAULT '#c6f432',
    text_color TEXT DEFAULT '#aebec2',
    bg_color TEXT DEFAULT '#0a1214',
    heading_font TEXT DEFAULT 'Montserrat',
    body_font TEXT DEFAULT 'Lora',
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_tokens (
    id SERIAL PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    name TEXT,
    created_by TEXT DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    last_used DOUBLE PRECISION,
    request_count INTEGER DEFAULT 0,
    revoked_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS mcp_call_log (
    id SERIAL PRIMARY KEY,
    token_id INTEGER NOT NULL,
    token_name TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_created ON mcp_call_log(created_at);
-- Usage analytics (backend/services/usage_log.py). Rows from before these columns
-- existed keep tool IS NULL ("legacy"); nothing is backfilled.
-- token_id is NULL for operator/OAuth calls and for requests rejected before auth.
ALTER TABLE mcp_call_log ALTER COLUMN token_id DROP NOT NULL;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS user_email TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS tool TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS transport TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS params TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS query TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS content_type TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS content_ref TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS act TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS result_count INTEGER;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS top_score DOUBLE PRECISION;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS exact_match BOOLEAN;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS citation_shaped BOOLEAN;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS latency_ms DOUBLE PRECISION;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS error_kind TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS error_detail TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS client_ip TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS response_bytes INTEGER;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS feedback TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS feedback_by TEXT;
ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS feedback_at DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_created_desc ON mcp_call_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_user_created ON mcp_call_log(user_email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_tool ON mcp_call_log(tool);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_status ON mcp_call_log(status);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_content_type ON mcp_call_log(content_type);

CREATE TABLE IF NOT EXISTS issues (
    id SERIAL PRIMARY KEY,
    ticket TEXT,
    category TEXT NOT NULL,
    tool TEXT,
    params TEXT,
    param_hash TEXT,
    expected TEXT,
    actual TEXT,
    note TEXT,
    server_ver TEXT,
    compilation TEXT,
    created TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'open',
    known_note TEXT,
    hits INTEGER DEFAULT 1,
    fixed BOOLEAN
);

-- Runtime content-type gating (backend/services/content_access.py). No row = enabled.
CREATE TABLE IF NOT EXISTS content_type_settings (
    key TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS user_content_grants (
    email TEXT NOT NULL,
    content_key TEXT NOT NULL,
    PRIMARY KEY (email, content_key)
);
CREATE TABLE IF NOT EXISTS user_pinned_sources (
    email TEXT NOT NULL,
    source_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, source_id)
);
"""


def get_conn():
    """New connection (autocommit off, RealDictCursor rows). Caller closes it."""
    if not DATABASE_URL:
        # Refuse to fall back to PG* env defaults — they point at other databases.
        raise RuntimeError("LAWKITTY_DATABASE_URL is not set")
    return psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=5
    )


@contextmanager
def cursor():
    """Cursor in a transaction: commits on success, rolls back on error, always closes."""
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def safe(default):
    """On any DB error, log and return `default` (called if callable) instead of raising."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                logger.exception("DB error in %s", fn.__qualname__)
                return default() if callable(default) else default
        return wrapper
    return deco


def init_db() -> None:
    with cursor() as cur:
        cur.execute(_SCHEMA)


if not DATABASE_URL:
    logger.warning("LAWKITTY_DATABASE_URL not set — user data unavailable")
else:
    try:
        init_db()
    except Exception:
        logger.exception("lawkitty init_db failed — user data unavailable until Postgres is reachable")
