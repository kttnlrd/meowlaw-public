"""Data version registry — tracks when data sources were last updated.

Single SQLite DB, no snapshots (daily backup.sh handles that), no revert
(that's what git + backups are for).

Just answers: "what changed last month?"
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from backend.config import BASE

DB_PATH = BASE / "data_version.db"

DATA_SOURCES = [
    "legislation", "cases_hca", "cases_fca",
    "cases_fcafc", "cases_aata", "rulings",
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY,
            version TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            summary TEXT,
            changes_json TEXT
        );
        CREATE TABLE IF NOT EXISTS source_updates (
            id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            added INTEGER DEFAULT 0,
            modified INTEGER DEFAULT 0,
            FOREIGN KEY (version_id) REFERENCES versions(id)
        );
        CREATE TABLE IF NOT EXISTS last_checked (
            source TEXT PRIMARY KEY,
            last_run_at REAL,
            last_result TEXT
        );
    """)
    conn.close()


def current_version() -> dict[str, Any] | None:
    """Return the most recent version."""
    _init()
    conn = _connect()
    row = conn.execute("SELECT * FROM versions ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return None
    v = dict(row)
    if v.get("changes_json"):
        try:
            v["changes"] = json.loads(v["changes_json"])
        except Exception:
            v["changes"] = []
    v.pop("changes_json", None)
    return v


def version_history(limit: int = 20) -> list[dict]:
    _init()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, version, created_at, summary FROM versions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def source_status() -> dict:
    _init()
    conn = _connect()
    rows = conn.execute("SELECT * FROM last_checked").fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = dict(r)
        if d.get("last_result"):
            try:
                d["last_result"] = json.loads(d["last_result"])
            except Exception:
                pass
        result[d["source"]] = d
    return result


def create_version(
    summary: str,
    changes: list[dict],
    source_updates: list[dict],
) -> str:
    """Record a data update.

    Args:
        summary: Human-readable summary.
        changes: List of {source, action, item, detail} dicts — stored as JSON.
        source_updates: List of {source, added, modified}.

    Returns:
        Version string like "v2026-07-28-001".
    """
    _init()
    now = time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")

    conn = _connect()
    prefix = f"v{date_str}"
    existing = conn.execute(
        "SELECT version FROM versions WHERE version LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = (int(existing["version"].rsplit("-", 1)[-1]) + 1) if existing else 1
    version = f"{prefix}-{seq:03d}"

    conn.execute(
        "INSERT INTO versions (version, created_at, summary, changes_json) VALUES (?, ?, ?, ?)",
        (version, now, summary, json.dumps(changes[:200])),
    )
    v_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for su in source_updates:
        conn.execute(
            "INSERT INTO source_updates (version_id, source, added, modified) VALUES (?, ?, ?, ?)",
            (v_id, su["source"], su.get("added", 0), su.get("modified", 0)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO last_checked (source, last_run_at, last_result) VALUES (?, ?, ?)",
            (su["source"], now, json.dumps({"added": su.get("added", 0), "modified": su.get("modified", 0)})),
        )
    conn.commit()
    conn.close()
    logger.info("Created version %s", version)
    return version
