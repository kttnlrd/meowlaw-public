"""Issues API — list and create issues in the cadena_knowledge DB."""
from __future__ import annotations

import hashlib
import json as _json
import logging

from fastapi import APIRouter, Body, HTTPException, Request

from backend.auth import require_admin, require_user
from backend.services.tax_case_sql import _sql_dict, _sql_write_params

logger = logging.getLogger(__name__)

router = APIRouter()



@router.get("/api/issues")
def list_issues(request: Request, status: str | None = None):
    require_user(request)
    """List issues, ordered by most recent first.

    Query params:
      status: filter by status (open, known, fixed). Omit for all.
    """
    where = ""
    params: tuple = ()
    if status:
        where = "WHERE status = %s"
        params = (status,)

    rows = _sql_dict(
        ["id", "ticket", "category", "tool", "params", "expected", "actual",
         "note", "server_ver", "status", "hits", "created", "fixed"],
        f"SELECT id, ticket, category, tool, params, "
        f"LEFT(COALESCE(expected, ''), 200)::text, "
        f"LEFT(COALESCE(actual, ''), 200)::text, "
        f"LEFT(COALESCE(note, ''), 200)::text, "
        f"server_ver, status, hits, created, fixed "
        f"FROM issues {where} ORDER BY id DESC",
        params,
    )

    for r in rows:
        if r.get("expected") and len(str(r["expected"])) > 200:
            r["expected"] = str(r["expected"])[:200] + "…"
        if r.get("actual") and len(str(r["actual"])) > 200:
            r["actual"] = str(r["actual"])[:200] + "…"
        if r.get("note") and len(str(r["note"])) > 200:
            r["note"] = str(r["note"])[:200] + "…"

    return {"issues": rows, "total": len(rows)}


@router.patch("/api/issues/{issue_id}")
def update_issue(
    request: Request,
    issue_id: int,
    status: str | None = Body(None),
    note: str | None = Body(None),
):
    """Update an issue's status and/or note.

    Body:
      status: open | known | fixed
      note: replacement note.
    """
    require_admin(request)
    valid = {"open", "known", "fixed"}
    if status is not None and status not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(valid)}",
        )

    existing = _sql_dict(
        ["id", "status", "note"],
        f"SELECT id, status, note FROM issues WHERE id = {int(issue_id)}",
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Issue not found")
    row = existing[0]

    sets = []
    params: list = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
        if status == "fixed" and row.get("status") != "fixed":
            sets.append("fixed = NOW()::text")
    if note is not None:
        sets.append("note = %s")
        params.append(note)

    if not sets:
        raise HTTPException(status_code=422, detail="Nothing to update")

    params.append(int(issue_id))
    _sql_write_params(
        f"UPDATE issues SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )
    updated = _sql_dict(
        ["id", "ticket", "status", "note", "fixed"],
        f"SELECT id, ticket, status, LEFT(COALESCE(note, ''), 200)::text, fixed "
        f"FROM issues WHERE id = {int(issue_id)}",
    )[0]
    return updated


@router.post("/api/issues")
def create_issue(
    request: Request,
    category: str = "bug",
    tool: str | None = Body(None),
    params: str | None = Body(None),
    expected: str | None = Body(None),
    actual: str | None = Body(None),
    note: str | None = Body(None),
):
    """Create a new manual bug report in the issues table."""
    require_user(request)
    if not any(
        str(v).strip()
        for v in (tool, params, expected, actual, note)
        if v is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Issue submission must include at least one of tool, params, expected, actual, note",
        )
    max_rows = _sql_dict(["next_id"], "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM issues")
    next_id = max_rows[0]["next_id"] if max_rows else 1
    ticket = f"CDN-{next_id:04d}"

    raw = f"{tool or ''}{params or ''}{note or ''}"
    param_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    from backend.routes.api import VERSION

    _sql_write_params(
        "INSERT INTO issues "
        "(ticket, category, tool, params, param_hash, expected, actual, note, "
        " server_ver, created, status, hits) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'open', 1)",
        (ticket, category, tool, params, param_hash, expected, actual, note, VERSION),
    )

    return {"ticket": ticket, "status": "open"}