"""Usage analytics API (/api/admin/usage/*) over mcp_call_log, plus /api/recall-feedback.

Every number is computed in SQL. Shared slicers (all repeatable lists AND-ed across
dimensions, OR-ed within one): from, to (ISO; a bare date is midnight in `tz`; `to` is
exclusive), user, tool, status (ok|empty|error|failed_recall), content_type, act,
q (substring of query/content_ref), include_legacy (rows logged before tool capture,
tool IS NULL — default excluded), include_protocol (MCP protocol traffic such as
initialize / tools/list, tool 'rpc:*' — default excluded), tz (IANA, default UTC).
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.db import cursor
from backend.config import is_admin_email
from backend.auth import require_admin
from backend.services.usage_log import FAILED_RECALL_SQL as FR, FAILED_RECALL_TEXT, STATUSES

router = APIRouter()

SEARCH_TOOLS = "(l.tool = 'search' OR l.tool LIKE 'web\\_%%')"
DIMS = {  # facet/breakdown name -> SQL expression
    "user": "COALESCE(l.user_email, '(unknown)')",
    "tool": "COALESCE(l.tool, '(legacy)')",
    "content_type": "COALESCE(l.content_type, '(none)')",
    "act": "l.act",
    "transport": "COALESCE(l.transport, '(legacy)')",
}
EVENT_FIELDS = ("id", "created_at", "user_email", "token_id", "token_name", "tool", "transport", "params",
                "query", "content_type", "content_ref", "act", "result_count", "top_score", "exact_match",
                "citation_shaped", "latency_ms", "status", "error_kind", "error_detail", "request_id",
                "client_ip", "response_bytes", "feedback", "feedback_by", "feedback_at")
EVENT_COLS = ", ".join(f"l.{c}" for c in EVENT_FIELDS) + f", {FR} AS failed_recall"
SORTS = {"created_at": "l.created_at", "latency_ms": "l.latency_ms", "result_count": "l.result_count"}


@dataclass
class F:
    lo: float | None
    hi: float | None
    tz: str
    user: list[str]
    tool: list[str]
    status: list[str]
    content_type: list[str]
    act: list[str]
    q: str
    include_legacy: bool
    include_protocol: bool

    def echo(self) -> dict:
        return {"from": self.lo, "to": self.hi, "tz": self.tz, "user": self.user, "tool": self.tool,
                "status": self.status, "content_type": self.content_type, "act": self.act, "q": self.q,
                "include_legacy": self.include_legacy, "include_protocol": self.include_protocol}


def _ts(s: str | None, tz: ZoneInfo) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Bad date: {s!r} (use ISO 8601)")
    return (d if d.tzinfo else d.replace(tzinfo=tz)).timestamp()


def filters(request: Request) -> F:
    qp = request.query_params
    tz = qp.get("tz") or "UTC"
    try:
        zone = ZoneInfo(tz)
    except Exception:
        raise HTTPException(400, f"Unknown tz: {tz!r}")
    status = qp.getlist("status")
    bad = set(status) - {*STATUSES, "failed_recall"}
    if bad:
        raise HTTPException(400, f"Unknown status: {sorted(bad)}")
    flag = lambda k: (qp.get(k) or "").lower() in ("1", "true", "yes")
    return F(_ts(qp.get("from"), zone), _ts(qp.get("to"), zone), tz, qp.getlist("user"), qp.getlist("tool"),
             status, qp.getlist("content_type"), qp.getlist("act"), (qp.get("q") or "").strip(),
             flag("include_legacy"), flag("include_protocol"))


def _where(f: F, skip: str = "", extra: str = "") -> tuple[str, list]:
    c, p = ([extra] if extra else []), []
    if not f.include_legacy:
        c.append("l.tool IS NOT NULL")
    if not f.include_protocol:
        c.append("(l.tool IS NULL OR l.tool NOT LIKE 'rpc:%%')")
    if f.lo is not None:
        c.append("l.created_at >= %s"); p.append(f.lo)
    if f.hi is not None:
        c.append("l.created_at < %s"); p.append(f.hi)
    for dim, vals in (("user", f.user), ("tool", f.tool), ("content_type", f.content_type), ("act", f.act)):
        if vals and skip != dim:
            c.append(f"{DIMS[dim]} = ANY(%s)"); p.append(vals)
    if f.status and skip != "status":
        ors = []
        plain = [s for s in f.status if s != "failed_recall"]
        if plain:
            ors.append("l.status = ANY(%s)"); p.append(plain)
        if "failed_recall" in f.status:
            ors.append(FR)
        c.append("(" + " OR ".join(ors) + ")")
    if f.q:
        c.append("(l.query ILIKE %s OR l.content_ref ILIKE %s)"); p += [f"%{f.q}%"] * 2
    return ("WHERE " + " AND ".join(c)) if c else "", p


def _q(sql: str, params: list | dict = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


AGG = (f"COUNT(*) AS calls, COUNT(*) FILTER (WHERE l.status = 'error') AS errors, "
       f"COUNT(*) FILTER (WHERE l.status = 'empty') AS empty, "
       f"COUNT(*) FILTER (WHERE {FR}) AS failed_recall, "
       f"COUNT(*) FILTER (WHERE l.result_count = 0) AS zero_results, "
       f"percentile_cont(0.95) WITHIN GROUP (ORDER BY l.latency_ms) AS p95_ms, MAX(l.created_at) AS last_at")


def _legacy_note(f: F) -> dict:
    w, p = _where(F(**{**f.__dict__, "include_legacy": True}))
    n = _q(f"SELECT COUNT(*) FILTER (WHERE l.tool IS NULL) AS n FROM mcp_call_log l {w}", p)[0]["n"]
    return {"includes_legacy": f.include_legacy, "legacy_rows_in_range": n,
            "legacy_note": "Legacy rows predate tool capture: they have time and token only "
                           "(tool, params, status, latency are NULL and are not invented)."}


@router.get("/api/admin/usage/summary")
def usage_summary(request: Request, top: int = 20):
    require_admin(request)
    f = filters(request)
    w, p = _where(f)
    k = _q(f"""SELECT COUNT(*) AS total_calls, COUNT(DISTINCT l.user_email) AS unique_users,
                      COUNT(DISTINCT l.token_id) AS unique_tokens,
                      COUNT(*) FILTER (WHERE l.status = 'ok') AS ok,
                      COUNT(*) FILTER (WHERE l.status = 'empty') AS empty,
                      COUNT(*) FILTER (WHERE l.status = 'error') AS error,
                      COUNT(*) FILTER (WHERE {FR}) AS failed_recall,
                      COUNT(*) FILTER (WHERE l.result_count = 0) AS zero_results,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms) AS latency_p50_ms,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY l.latency_ms) AS latency_p95_ms,
                      MAX(l.latency_ms) AS latency_max_ms, COALESCE(SUM(l.response_bytes), 0) AS response_bytes,
                      MIN(l.created_at) AS first_at, MAX(l.created_at) AS last_at
               FROM mcp_call_log l {w}""", p)[0]
    n = k["total_calls"]
    k["error_rate"] = round(k["error"] / n, 4) if n else None
    k["failed_recall_rate"] = round(k["failed_recall"] / n, 4) if n else None
    nu_c, nu_p = [], []
    if f.lo is not None:
        nu_c.append("NULLIF(created_at, '')::timestamptz >= to_timestamp(%s)"); nu_p.append(f.lo)
    if f.hi is not None:
        nu_c.append("NULLIF(created_at, '')::timestamptz < to_timestamp(%s)"); nu_p.append(f.hi)
    k["new_users"] = _q("SELECT COUNT(*) AS n FROM users" + (" WHERE " + " AND ".join(nu_c) if nu_c else ""),
                        nu_p)[0]["n"]

    # Series: hourly when the range is <= 2 days, else daily; zero-filled in SQL.
    lo, hi = f.lo, f.hi
    if lo is None or hi is None:
        lo = lo if lo is not None else k["first_at"]
        hi = hi if hi is not None else (k["last_at"] + 1 if k["last_at"] else None)
    grain = "hour" if (lo is not None and hi is not None and hi - lo <= 2 * 86400) else "day"
    series = []
    if lo is not None and hi is not None:
        series = _q(f"""
            WITH f AS (SELECT date_trunc(%s, to_timestamp(l.created_at) AT TIME ZONE %s) AS t, l.status
                       FROM mcp_call_log l {w}),
                 b AS (SELECT generate_series(date_trunc(%s, to_timestamp(%s) AT TIME ZONE %s),
                                              date_trunc(%s, to_timestamp(%s) AT TIME ZONE %s),
                                              ('1 ' || %s)::interval) AS t)
            SELECT to_char(b.t, 'YYYY-MM-DD"T"HH24:MI') AS bucket,
                   COUNT(f.t) AS total,
                   COUNT(f.t) FILTER (WHERE f.status = 'ok') AS ok,
                   COUNT(f.t) FILTER (WHERE f.status = 'empty') AS empty,
                   COUNT(f.t) FILTER (WHERE f.status = 'error') AS error,
                   COUNT(f.t) FILTER (WHERE f.status IS NULL) AS legacy
            FROM b LEFT JOIN f ON f.t = b.t GROUP BY b.t ORDER BY b.t""",
            [grain, f.tz, *p, grain, lo, f.tz, grain, hi - 0.001, f.tz, grain])

    def breakdown(dim: str) -> list[dict]:
        return _q(f"SELECT {DIMS[dim]} AS key, {AGG} FROM mcp_call_log l {w} GROUP BY 1 "
                  f"ORDER BY calls DESC, key LIMIT %s", [*p, top])

    by_status = _q(f"SELECT COALESCE(l.status, '(legacy)') AS key, COUNT(*) AS calls FROM mcp_call_log l {w} "
                   f"GROUP BY 1 ORDER BY calls DESC", p)
    by_hour = _q(f"""SELECT h.hour AS key, COUNT(l.id) AS calls,
                            COUNT(l.id) FILTER (WHERE l.status = 'error') AS errors,
                            COUNT(l.id) FILTER (WHERE {FR}) AS failed_recall
                     FROM generate_series(0, 23) AS h(hour)
                     LEFT JOIN (SELECT l.* FROM mcp_call_log l {w}) l
                       ON EXTRACT(HOUR FROM to_timestamp(l.created_at) AT TIME ZONE %s) = h.hour
                     GROUP BY h.hour ORDER BY h.hour""", [*p, f.tz])
    return {"filters": f.echo(), "kpis": k, "grain": grain, "series": series,
            "breakdowns": {"user": breakdown("user"), "tool": breakdown("tool"),
                           "content_type": breakdown("content_type"), "status": by_status, "hour": by_hour},
            "failed_recall_definition": FAILED_RECALL_TEXT, **_legacy_note(f)}


def _events_sql(f: F, sort: str, dir_: str) -> tuple[str, list]:
    if sort not in SORTS:
        raise HTTPException(400, f"sort must be one of {sorted(SORTS)}")
    d = "ASC" if dir_.lower() == "asc" else "DESC"
    w, p = _where(f)
    return f"SELECT {EVENT_COLS} FROM mcp_call_log l {w} ORDER BY {SORTS[sort]} {d} NULLS LAST, l.id {d}", p


@router.get("/api/admin/usage/events")
def usage_events(request: Request, limit: int = 50, offset: int = 0, sort: str = "created_at", dir: str = "desc"):
    require_admin(request)
    f = filters(request)
    limit, offset = min(200, max(1, limit)), max(0, offset)
    sql, p = _events_sql(f, sort, dir)
    w, wp = _where(f)
    total = _q(f"SELECT COUNT(*) AS n FROM mcp_call_log l {w}", wp)[0]["n"]
    rows = _q(sql + " LIMIT %s OFFSET %s", [*p, limit, offset])
    return {"filters": f.echo(), "total": total, "limit": limit, "offset": offset, "events": rows,
            "failed_recall_definition": FAILED_RECALL_TEXT}


@router.get("/api/admin/usage/facets")
def usage_facets(request: Request):
    """Every slicer value with its count under all the *other* active slicers (so a
    selected value keeps its siblings visible); zero counts are kept for dimming."""
    require_admin(request)
    f = filters(request)
    out: dict = {}
    every = F(None, None, f.tz, [], [], [], [], [], "", f.include_legacy, f.include_protocol)
    for dim in ("user", "tool", "content_type", "act"):
        extra = "l.act IS NOT NULL" if dim == "act" else ""
        w, p = _where(f, skip=dim, extra=extra)
        counts = {r["key"]: r["n"] for r in _q(
            f"SELECT {DIMS[dim]} AS key, COUNT(*) AS n FROM mcp_call_log l {w} GROUP BY 1", p)}
        # every value seen in the same legacy/protocol scope, so options never vanish
        base, bp = _where(every, extra=extra)
        allv = [r["key"] for r in _q(f"SELECT DISTINCT {DIMS[dim]} AS key FROM mcp_call_log l {base}", bp)]
        out[dim] = sorted(({"value": v, "count": counts.get(v, 0)} for v in allv),
                          key=lambda x: (-x["count"], str(x["value"])))
    w, p = _where(f, skip="status")
    s = _q(f"""SELECT COUNT(*) FILTER (WHERE l.status = 'ok') AS ok,
                      COUNT(*) FILTER (WHERE l.status = 'empty') AS empty,
                      COUNT(*) FILTER (WHERE l.status = 'error') AS error,
                      COUNT(*) FILTER (WHERE {FR}) AS failed_recall
               FROM mcp_call_log l {w}""", p)[0]
    out["status"] = [{"value": k, "count": s[k]} for k in ("ok", "empty", "error", "failed_recall")]
    return {"filters": f.echo(), "facets": out, "failed_recall_definition": FAILED_RECALL_TEXT, **_legacy_note(f)}


@router.get("/api/admin/usage/content")
def usage_content(request: Request, top: int = 20):
    require_admin(request)
    f = filters(request)
    w, p = _where(f)
    a = "AND" if w else "WHERE"

    def top_by(expr: str, cond: str) -> list[dict]:
        return _q(f"""SELECT {expr} AS label, COUNT(*) AS calls, COUNT(DISTINCT l.user_email) AS users,
                             COUNT(*) FILTER (WHERE {FR}) AS failed_recall, MAX(l.created_at) AS last_at
                      FROM mcp_call_log l {w} {a} {cond} GROUP BY 1 ORDER BY calls DESC, label LIMIT %s""",
                  [*p, top])

    ref = "l.content_ref IS NOT NULL AND l.content_type = '{}'"
    qry = f"l.query IS NOT NULL AND btrim(l.query) <> '' AND {SEARCH_TOOLS}"
    return {
        "filters": f.echo(),
        "acts": top_by("l.act", "l.act IS NOT NULL"),
        "sections": top_by("l.content_ref", ref.format("section")),
        "rulings": top_by("l.content_ref", ref.format("ruling")),
        "cases": top_by("l.content_ref", ref.format("case")),
        "queries": top_by("lower(btrim(l.query))", qry),
        "zero_result_queries": top_by("lower(btrim(l.query))", f"{qry} AND l.result_count = 0"),
        "failed_recall_queries": top_by("lower(btrim(l.query))", f"l.query IS NOT NULL AND {FR}"),
        "failed_recall_definition": FAILED_RECALL_TEXT,
    }


@router.get("/api/admin/usage/user/{email}")
def usage_user(email: str, request: Request):
    """One user's usage. Admin for anyone; a signed-in user for themself only."""
    from backend.auth import require_user
    me = require_user(request)
    if not is_admin_email(me.get("email")) and me.get("email") != email:
        raise HTTPException(403, "Admin access required")
    tokens = _q("""SELECT t.id, t.name, t.created_at, t.last_used, t.request_count, t.revoked_at,
                          (SELECT COUNT(*) FROM mcp_call_log l WHERE l.token_id = t.id) AS logged_calls
                   FROM mcp_tokens t WHERE t.created_by = %s ORDER BY t.created_at DESC""", [email])
    u = _q("SELECT tier, created_at, last_login_at FROM users WHERE email = %s", [email])
    # same default scope as the dashboard: no legacy rows, no protocol traffic
    mine = "l.user_email = %s AND l.tool IS NOT NULL AND l.tool NOT LIKE 'rpc:%%'"
    per_day = _q(f"""SELECT to_char(to_timestamp(l.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                            COUNT(*) AS calls, COUNT(*) FILTER (WHERE l.status = 'error') AS errors,
                            COUNT(*) FILTER (WHERE {FR}) AS failed_recall
                     FROM mcp_call_log l WHERE {mine} GROUP BY 1 ORDER BY 1""", [email])
    totals = _q(f"SELECT {AGG} FROM mcp_call_log l WHERE {mine}", [email])[0]
    top_tools = _q(f"SELECT l.tool AS key, COUNT(*) AS calls FROM mcp_call_log l WHERE {mine} "
                   f"GROUP BY 1 ORDER BY calls DESC, key LIMIT 20", [email])
    top_content = _q(f"""SELECT l.content_type, l.content_ref, COUNT(*) AS calls FROM mcp_call_log l
                         WHERE {mine} AND l.content_ref IS NOT NULL GROUP BY 1, 2
                         ORDER BY calls DESC, l.content_ref LIMIT 20""", [email])
    errors = _q(f"SELECT {EVENT_COLS} FROM mcp_call_log l WHERE {mine} AND l.status = 'error' "
                f"ORDER BY l.created_at DESC LIMIT 20", [email])
    failed = _q(f"SELECT {EVENT_COLS} FROM mcp_call_log l WHERE {mine} AND {FR} "
                f"ORDER BY l.created_at DESC LIMIT 20", [email])
    last_login = None
    try:
        from backend.services.login_log import LOG_DB
        conn = sqlite3.connect(LOG_DB)
        last_login = conn.execute("SELECT MAX(login_at) FROM login_log WHERE email = ?", (email,)).fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    return {"email": email, "tier": u[0]["tier"] if u else None, "created_at": u[0]["created_at"] if u else None,
            "last_login_log": last_login, "last_login_at": u[0]["last_login_at"] if u else None,
            "tokens": tokens, "totals": totals, "calls_per_day": per_day, "top_tools": top_tools,
            "top_content": top_content, "errors": errors, "failed_recalls": failed,
            "failed_recall_definition": FAILED_RECALL_TEXT,
            "note": "Excludes legacy rows (tool IS NULL, no user recorded) and MCP protocol requests "
                    "(tool rpc:*); token logged_calls counts every row for the token."}


@router.get("/api/admin/usage/export.csv")
def usage_export(request: Request, sort: str = "created_at", dir: str = "desc"):
    require_admin(request)
    f = filters(request)
    sql, p = _events_sql(f, sort, dir)
    rows = _q(sql + " LIMIT 100000", p)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[*EVENT_FIELDS, "failed_recall"])
    w.writeheader()
    for r in rows:
        r["created_at"] = datetime.fromtimestamp(r["created_at"], ZoneInfo(f.tz)).isoformat()
        w.writerow(r)
    stamp = datetime.now(ZoneInfo(f.tz)).strftime("%Y%m%d-%H%M")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="lawkitty-usage-{stamp}.csv"',
                             "X-Row-Count": str(len(rows))})


@router.post("/api/recall-feedback")
async def recall_feedback(request: Request):
    """Mark one usage event good/bad (null clears). Owner of the event, or the admin."""
    from backend.auth import require_user
    me = require_user(request)
    body = await request.json()
    fb = body.get("feedback")
    if fb not in ("good", "bad", None):
        raise HTTPException(400, "feedback must be 'good', 'bad' or null")
    try:
        event_id = int(body.get("event_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "event_id required")
    admin = is_admin_email(me.get("email"))
    with cursor() as cur:
        cur.execute("UPDATE mcp_call_log SET feedback = %s, feedback_by = %s, "
                    "feedback_at = EXTRACT(EPOCH FROM now()) "
                    "WHERE id = %s AND (%s OR user_email = %s) RETURNING id, feedback",
                    (fb, me.get("email"), event_id, admin, me.get("email")))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Event not found")
    return dict(row)
