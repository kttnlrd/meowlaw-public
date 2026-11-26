"""Admin-only routes: token dashboard, system health, re-index, logs, users."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.mcp_token_manager import token_manager
from backend.auth import require_admin, store
from backend.auth.providers import sso
from backend.config import ADMIN_EMAILS
from backend.db import cursor
from backend.services.usage_log import REJECT_KINDS

router = APIRouter()

ADMIN_EMAIL = min(ADMIN_EMAILS, default="")  # one admin address, for callers that need a single one (tests)

# Server start time for uptime calculation
_SERVER_START = time.time()


@router.get("/api/admin/tokens")
def admin_list_tokens(request: Request):
    require_admin(request)
    tokens = token_manager.list_tokens()
    return {"tokens": tokens}


@router.post("/api/admin/tokens/{token_id}/revoke")
def admin_revoke_token(token_id: str, request: Request):
    require_admin(request)
    revoked = token_manager.revoke_token(token_id)
    if revoked:
        return JSONResponse({"message": "Token revoked"})
    return JSONResponse({"error": "Token not found"}, status_code=404)


@router.get("/api/admin/health")
def admin_health(request: Request):
    require_admin(request)
    from backend.routes.api import VERSION

    uptime = time.time() - _SERVER_START

    # Corpus counts (SQLite FTS indexes)
    db_stats = {}
    try:
        import sqlite3 as _sqlite3
        from backend.config import SEARCH_DB
        conn = _sqlite3.connect(str(SEARCH_DB))
        def _cnt(tbl):
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                return None
        db_stats = {
            "sections": _cnt("sections_fts"),
            "rulings": _cnt("rulings_fts"),
            "private_rulings": _cnt("private_rulings_fts"),
            "case_summaries": _cnt("case_summaries_fts"),
            "treaties": _cnt("treaties_fts"),
        }
        conn.close()
    except Exception as e:
        db_stats["error"] = f"Could not query search index: {e}"

    # MCP tokens count
    mcp_tokens_count = len(token_manager.list_tokens())

    # Last backup
    from backend.config import BASE, DATA_DIR
    backup_dir = DATA_DIR
    backups = sorted(backup_dir.glob("cadena_db_backup_*.sql.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    last_backup = backups[0].stat().st_mtime if backups else None

    # Search index
    search_db = BASE / "search_index.db"
    search_index_built = search_db.stat().st_mtime if search_db.exists() else None

    # Legislation compile info
    legislation = []
    try:
        from backend.config import DATA_DIR
        for act_dir in sorted(DATA_DIR.iterdir()):
            if act_dir.is_dir() and (act_dir / "tree.json").exists():
                import json as _json
                tree = _json.loads((act_dir / "tree.json").read_text())
                legislation.append({
                    "act": act_dir.name,
                    "name": tree.get("act", act_dir.name),
                    "compilation_no": tree.get("compilation_no"),
                    "compilation_date": tree.get("compilation_date"),
                })
    except Exception:
        pass

    return {
        "backend": {
            "uptime_seconds": round(uptime),
            "version": VERSION,
            "auth_enabled": True,
            "sso_providers": list(sso.LABELS),
        },
        "database": {
            **db_stats,
            "mcp_tokens": mcp_tokens_count,
            "last_backup": last_backup,
            "search_index_built": search_index_built,
        },
        "legislation": legislation,
    }


@router.post("/api/admin/reindex")
def admin_reindex(request: Request):
    require_admin(request)
    # Run reindex in background
    import threading
    def _reindex():
        try:
            from backend.services.search_service import init_search_index
            init_search_index()
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Reindex failed")
    threading.Thread(target=_reindex, daemon=True).start()
    return {"status": "started", "message": "Reindexing in background. Check health endpoint for updated search_index_built."}


@router.get("/api/admin/logs")
def admin_logs(request: Request, lines: int = 50, level: str = ""):
    require_admin(request)
    lines = min(200, max(10, lines))
    try:
        service_name = os.environ.get("SYSTEMD_SERVICE_NAME", "lawkitty")
        cmd = ["journalctl", "-u", service_name, "--no-pager", "-n", str(lines),
               "-o", "short-iso"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        log_lines = r.stdout.strip().split("\n") if r.stdout.strip() else []
        if level:
            log_lines = [l for l in log_lines if level.upper() in l.upper()]
        return {"logs": log_lines[-lines:], "total": len(log_lines)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    return {"users": store.list_users()}


@router.post("/api/admin/users/{user_id}/tier")
async def admin_set_tier(user_id: int, request: Request):
    require_admin(request)
    try:
        body = await request.json()
        tier = body.get("tier") if isinstance(body, dict) else None
    except ValueError:
        tier = None
    try:
        user = store.update_user_tier(user_id, tier if isinstance(tier, str) else "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not user:
        raise HTTPException(404, "User not found")
    return {"user": user}


@router.post("/api/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request):
    require_admin(request)
    if not store.delete_user(user_id):
        raise HTTPException(404, "User not found")
    return {"message": "User deleted"}


@router.get("/api/admin/stats")
def admin_stats(request: Request):
    require_admin(request)
    from collections import Counter

    users = store.list_users()
    by_tier = {t: 0 for t in store.TIERS}
    for u in users:
        t = u.get("tier") or "trial"
        by_tier[t] = by_tier.get(t, 0) + 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    # last_login_at is an ISO-8601 UTC string, so string comparison orders correctly
    active_7d = sum(1 for u in users if (u.get("last_login_at") or "") >= cutoff)

    signup_days = Counter()
    for u in users:
        ca = (u.get("created_at") or "")[:10]
        if ca:
            signup_days[ca] += 1

    call_days = Counter()
    calls_by_user = []
    with cursor() as cur:
        # Lifetime calls incl. revoked tokens; mcp_call_log only covers since it was added.
        cur.execute("SELECT COALESCE(SUM(request_count), 0) AS n FROM mcp_tokens")
        total_calls = int(cur.fetchone()["n"])
        # Same rows the pre-analytics logger wrote: authorised calls on a stored token
        # (usage rows for rejections and operator/OAuth calls are excluded here).
        same_as_before = "l.token_id IS NOT NULL AND (l.error_kind IS NULL OR l.error_kind <> ALL(%s))"
        cur.execute(f"SELECT l.created_at FROM mcp_call_log l WHERE l.created_at IS NOT NULL AND {same_as_before} "
                    "ORDER BY l.id DESC LIMIT 20000", (list(REJECT_KINDS),))
        for row in cur.fetchall():
            try:
                d = datetime.fromtimestamp(float(row["created_at"]), tz=timezone.utc).date().isoformat()
                call_days[d] += 1
            except (TypeError, ValueError, OSError):
                pass
        cur.execute(f"""
            SELECT COALESCE(NULLIF(t.created_by, ''), '(operator)') AS user_email,
                   COUNT(*) AS calls,
                   COUNT(DISTINCT l.token_id) AS tokens
            FROM mcp_call_log l
            LEFT JOIN mcp_tokens t ON t.id = l.token_id
            WHERE {same_as_before}
            GROUP BY t.created_by
            ORDER BY calls DESC
        """, (list(REJECT_KINDS),))
        calls_by_user = [
            {"email": row["user_email"], "calls": int(row["calls"]), "tokens": int(row["tokens"])}
            for row in cur.fetchall()
        ]

    today = datetime.now(timezone.utc).date()

    def _series(counter: Counter, days: int = 14) -> list[dict]:
        return [
            {"date": (today - timedelta(days=i)).isoformat(), "count": counter.get((today - timedelta(days=i)).isoformat(), 0)}
            for i in range(days - 1, -1, -1)
        ]

    return {
        "total_users": len(users),
        "by_tier": by_tier,
        "active_7d": active_7d,
        "total_mcp_tokens": len(token_manager.list_tokens()),
        "total_mcp_calls": total_calls,
        "mcp_calls_by_user": calls_by_user,
        "signups_per_day": _series(signup_days),
        "mcp_calls_per_day": _series(call_days),
    }
