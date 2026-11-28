"""Simple SQLite-backed comment service for sections."""
from __future__ import annotations

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.config import BASE

logger = logging.getLogger(__name__)

COMMENTS_DB = BASE / "comments.db"


@contextmanager
def comments_conn():
    """Yield a fresh SQLite connection."""
    conn = sqlite3.connect(str(COMMENTS_DB))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_comments_db() -> None:
    """Create the comments table if it doesn't exist."""
    with comments_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                act TEXT NOT NULL,
                section TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Anonymous',
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_act_section ON comments(act, section)
        """)
        conn.commit()
    logger.info(f"Comments DB ready: {COMMENTS_DB}")


def get_comments(act: str, section: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch comments for a given act/section, newest first."""
    init_comments_db()
    with comments_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, author, text, created_at, resolved
            FROM comments
            WHERE act = ? AND section = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (act, section, limit, offset),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "author": r["author"],
            "text": r["text"],
            "created_at": r["created_at"],
            "resolved": bool(r["resolved"]),
        }
        for r in rows
    ]


def add_comment(act: str, section: str, author: str, text: str) -> dict:
    """Add a new comment. Returns the created comment."""
    init_comments_db()
    author = (author or "Anonymous").strip() or "Anonymous"
    text = text.strip()
    if not text:
        raise ValueError("Comment text is required")
    now = datetime.now(timezone.utc).isoformat()
    with comments_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO comments (act, section, author, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (act, section, author, text, now),
        )
        conn.commit()
        comment_id = cur.lastrowid
    return {
        "id": comment_id,
        "act": act,
        "section": section,
        "author": author,
        "text": text,
        "created_at": now,
        "resolved": False,
    }


def resolve_comment(comment_id: int) -> None:
    """Mark a comment as resolved."""
    init_comments_db()
    with comments_conn() as conn:
        conn.execute(
            "UPDATE comments SET resolved = 1 WHERE id = ?",
            (comment_id,),
        )
        conn.commit()
