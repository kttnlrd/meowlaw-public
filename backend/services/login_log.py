"""Login log for tracking user sign-ins."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from backend.config import BASE

LOG_DB = str(BASE / "login_log.db")


def _init():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                name TEXT,
                login_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_log_email ON login_log(email)"
        )
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(LOG_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def log_login(email: str, name: str = ""):
    """Record a login event."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO login_log (email, name, login_at) VALUES (?, ?, ?)",
            (email, name, time.time()),
        )
        conn.commit()


def get_users() -> list[dict]:
    """Get all unique users with their last login and total count."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT email, name, MAX(login_at) as last_login, COUNT(*) as login_count
            FROM login_log
            GROUP BY email
            ORDER BY last_login DESC
            """
        ).fetchall()
    return [{"email": r["email"], "name": r["name"], "last_login": r["last_login"], "login_count": r["login_count"]} for r in rows]


_init()