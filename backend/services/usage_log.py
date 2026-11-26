"""Usage events: one mcp_call_log row per MCP request / web search.

The MCP auth middleware owns the event for a request (``current``); the tool
wrapper fills in what only the tool knows (result count, content ref, status,
exception), and the middleware writes the row once in a ``finally`` — so a call
rejected before it reaches a tool is still logged, exactly once.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import time
import uuid
from contextvars import ContextVar

from backend.db import cursor, safe

STATUSES = ("ok", "empty", "error")
# content_type vocabulary: what the tools accept (search `types`, fetch/related id
# prefixes, outline collections) plus the non-document tools.
CONTENT_TYPES = ("act", "section", "definition", "ruling", "private_ruling", "case", "rg",
                 "afsa", "treaty", "graph", "issue", "mixed", "other")
# Rows that never reached a tool: not "calls" for the daily cap.
REJECT_KINDS = ("auth_missing", "auth_invalid", "auth_rate_limited", "no_account",
                "rate_limited", "daily_cap")

# The definition is the SQL; FAILED_RECALL_TEXT is what the API and UI show.
FAILED_RECALL_SQL = ("(l.status = 'error' OR l.result_count = 0 "
                     "OR (l.citation_shaped AND NOT COALESCE(l.exact_match, FALSE)) "
                     "OR l.feedback = 'bad')")
FAILED_RECALL_TEXT = ("failed recall = status is error, OR zero results, OR the query is "
                      "citation-shaped and no result is that exact document, OR the user/assistant "
                      "marked it bad (feedback = bad)")

PARAMS_MAX = 4096

current: ContextVar[dict | None] = ContextVar("usage_event", default=None)


def new_event(transport: str, **kw) -> dict:
    return {"transport": transport, "request_id": uuid.uuid4().hex[:12],
            "created_at": time.time(), **kw}


# --- params: redact + cap ---------------------------------------------------

_SECRET_KEY = re.compile(r"token|key|secret|passw|auth|bearer|cookie|session|credential|signature", re.I)
_SECRET_VAL = re.compile(
    r"^(bearer\s+\S+|sk-[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{32,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
    r"|[A-Za-z0-9_-]{40,})$", re.I)


def redact(v, key: str = ""):
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(v, dict):
        return {k: redact(x, str(k)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [redact(x) for x in v]
    if isinstance(v, str) and _SECRET_VAL.match(v.strip()):
        return "[REDACTED]"
    return v


def params_text(params) -> str | None:
    if params in (None, {}, []):
        return None
    s = json.dumps(redact(params), default=str, ensure_ascii=False)
    if len(s.encode()) > PARAMS_MAX:
        s = json.dumps({"_truncated": True, "_bytes": len(s.encode()), "preview": s[:PARAMS_MAX - 200]})
    return s


# --- citations ----------------------------------------------------------------

_RULING_RE = re.compile(r"^\s*(ato\s*id|[a-z]{1,6}(?:\s?la)?)\s*(\d{4})\s*/\s*(\d+)\s*$", re.I)
_CASE_RE = re.compile(r"^\s*\[(\d{4})\]\s*([a-z]+)\s*(\d+)\s*$", re.I)
_SECTION_RE = re.compile(r"^\s*(?:s|ss|sec|section)\.?\s*([0-9][0-9a-z.-]*)(?:\((\d+)\))?\s+(?:of\s+(?:the\s+)?)?(.+?)\s*$", re.I)
_PBR_RE = re.compile(r"^\s*(\d{10,14})\s*$")


def act_display(slug: str) -> str:
    """itaa-1997 -> 'ITAA 1997', corporations-act-2001 -> 'Corporations Act 2001'."""
    name, _, year = slug.rpartition("-")
    if not year.isdigit():
        return slug
    words = name.replace("-", " ")
    return f"{words.upper() if len(name) <= 6 else words.title()} {year}"


def _act_slug(text: str) -> str | None:
    from backend.config import DATA_DIR
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    for cand in (s, s.replace("-act-", "-"), re.sub(r"a-(\d{4})$", r"-\1", s)):
        if cand and (DATA_DIR / cand / "tree.json").exists():
            return cand
    return None


def cited(query: str | None) -> dict | None:
    """The one document a citation-shaped query names: {id, ref, type, act}, else None."""
    q = (query or "").strip()
    if not q or len(q) > 80:
        return None
    from backend.doc_ids import parse_id
    d = parse_id(q)
    if d:
        act, ref, parts = None, q, d.id.split(":")
        if d.type == "legislation":
            act, ref = parts[1], f"s {parts[2]} {act_display(parts[1])}"
        elif d.type == "case":
            y, court, n = parts[1].split("-", 2)
            ref = f"[{y}] {court.upper()} {n}"
        elif d.type == "ruling":
            series, y, n = parts[1].rsplit("-", 2)
            ref = f"{series.upper()} {y}/{n}"
        return {"id": d.id, "ref": ref, "type": d.type, "act": act}
    if m := _CASE_RE.match(q):
        y, court, n = m.groups()
        return {"id": f"case:{y}-{court.lower()}-{n}", "ref": f"[{y}] {court.upper()} {n}", "type": "case", "act": None}
    if m := _SECTION_RE.match(q):
        sec, sub, act_text = m.groups()
        slug = _act_slug(act_text)
        if slug:
            return {"id": f"leg:{slug}:{sec.lower()}", "ref": f"s {sec}{f'({sub})' if sub else ''} {act_display(slug)}",
                    "type": "legislation", "act": slug}
        return None
    if m := _RULING_RE.match(q):
        series, y, n = m.groups()
        series = re.sub(r"\s+", "", series).lower()
        series = "aid" if series == "atoid" else series
        return {"id": f"ruling:{series}-{y}-{int(n)}", "ref": f"{m.group(1).upper()} {y}/{int(n)}", "type": "ruling", "act": None}
    if m := _PBR_RE.match(q):
        return {"id": f"pbr:{m.group(1)}", "ref": m.group(1), "type": "private_ruling", "act": None}
    return None


# doc type (search `types` / id prefix / fetch result type) -> content_type
_TYPE_MAP = {"legislation": "section", "case": "case", "ruling": "ruling", "private_ruling": "private_ruling",
             "rg": "rg", "afsa": "afsa", "treaty": "treaty"}


# --- tool analysis -------------------------------------------------------------

_SCORE_KEYS = ("score", "fusion_score", "rrf_score", "similarity", "relevance")


def _items(obj: dict) -> list[dict]:
    res = obj.get("results")
    if isinstance(res, dict):
        return [x for v in res.values() if isinstance(v, list) for x in v if isinstance(x, dict)]
    if isinstance(res, list):
        return [x for x in res if isinstance(x, dict)]
    return []


def _top_score(items: list[dict]) -> float | None:
    vals = [float(x[k]) for x in items for k in _SCORE_KEYS
            if isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool)]
    return max(vals) if vals else None


def _exact(items: list[dict], cid: str | None) -> bool:
    return any(x.get("exact_match") is True or (cid and str(x.get("id", "")).lower() == cid) for x in items)


def describe_call(tool: str, args: dict) -> dict:
    """What a tool call targets, from its arguments alone (known even if it fails)."""
    a = args or {}
    ev: dict = {"tool": tool, "params": a}
    if tool == "search":
        c = cited(a.get("query"))
        types = [t for t in (a.get("types") or []) if t in _TYPE_MAP]
        ev.update(query=a.get("query"), content_type=_TYPE_MAP[types[0]] if len(types) == 1 else "mixed",
                  content_ref=c["ref"] if c else None, act=a.get("act") or (c and c["act"]),
                  citation_shaped=bool(c), _cited_id=c and c["id"])
    elif tool in ("fetch", "related"):
        ref = a.get("ref") if tool == "fetch" else a.get("id")
        c = cited(ref)
        ev.update(query=ref, content_type=_TYPE_MAP.get(c["type"], "other") if c else "other",
                  content_ref=c["ref"] if c else ref, act=c and c["act"],
                  citation_shaped=tool == "fetch" and bool(c), _cited_id=c and c["id"])
    elif tool == "outline":
        ev.update(content_type="act", content_ref=a.get("id"), act=a.get("id"))
    elif tool == "get_definition":
        ev.update(query=a.get("term"), content_type="definition", act=a.get("act"),
                  content_ref=f"{a.get('term')} ({act_display(a.get('act') or '')})")
    elif tool == "resolve_alias":
        ev.update(query=a.get("reference"), content_type="other", content_ref=a.get("reference"))
    elif tool in ("graph_neighbourhood", "graph_path"):
        ref = a.get("key") or f"{a.get('from_key')} -> {a.get('to_key')}"
        ev.update(content_type="graph", content_ref=ref)
    elif tool == "list_rulings":
        # a listing filter, not a document: kept in query so it never shows as a "top ruling"
        ev.update(content_type="ruling", query=" ".join(str(x) for x in (a.get("type"), a.get("year")) if x) or None)
    elif tool == "report_issue":
        ev.update(content_type="issue", content_ref=a.get("category"))
    elif tool == "report_bad_recall":
        ev.update(query=a.get("query"), content_type="issue", content_ref=a.get("expected"),
                  status="empty", result_count=0, feedback="bad")
    else:
        ev.update(content_type="other")
    return ev


_ABSENCE_RE = re.compile(
    r"not found|could not resolve|no (?:such|matching|matches|results)|not in the collection",
    re.I,
)


def analyse_result(tool: str, ev: dict, out) -> dict:
    """Result-derived fields: status, result_count, top_score, exact_match, content refinements."""
    upd: dict = {}
    try:
        obj = json.loads(out) if isinstance(out, str) else out
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        return {"status": "ok"}
    if obj.get("error"):
        e = obj["error"]
        detail = (e if isinstance(e, str) else json.dumps(e))[:1000]
        # A tool that ran fine and found nothing ("not found in any act", "could not resolve
        # reference") is an ABSENCE, not a failure. Log it as 'empty' so the error rate means
        # real breakage; the kind and detail are still recorded, so nothing is hidden.
        if _ABSENCE_RE.search(detail):
            return {"status": "empty", "error_kind": "NotFound", "error_detail": detail,
                    "result_count": 0}
        return {"status": "error", "error_kind": "ErrorResult", "error_detail": detail}

    cid = ev.get("_cited_id")
    n: int | None = None
    if tool == "search":
        items = _items(obj)
        n = len(items)
        upd.update(top_score=_top_score(items), exact_match=_exact(items, cid))
    elif tool == "fetch":
        n = 1
        rid = str(obj.get("id") or "").lower()
        upd.update(exact_match=bool(cid) and rid == cid,
                   content_type=_TYPE_MAP.get(obj.get("type"), ev.get("content_type")),
                   content_ref=obj.get("citation") or obj.get("id") or ev.get("content_ref"))
        if obj.get("type") == "legislation":
            upd["act"] = obj.get("act")
    elif tool == "related":
        n = int(obj.get("cites_total") or 0) + int(obj.get("cited_by_total") or 0)
    elif tool == "outline":
        if obj.get("collections"):
            n = sum(len(v) for v in obj["collections"].values())
        else:
            n = len(obj.get("parts") or obj.get("articles") or [])
            if obj.get("articles") is not None:
                upd["content_type"] = "treaty"
    elif tool == "get_definition":
        n = 1 if obj.get("text") else 0
        if obj.get("section"):
            upd["content_ref"] = f"{obj.get('term')} (s {obj['section']} {act_display(obj.get('act') or '')})"
    elif tool == "resolve_alias":
        key = obj.get("graph_key") or ""
        n = 1 if key else 0
        kind = key.split(":", 1)[0]
        upd["content_type"] = {"section": "section", "case": "case", "ruling": "ruling"}.get(kind, "other")
    elif tool == "graph_neighbourhood":
        n = 1 if obj.get("text") else 0
    elif tool == "graph_path":
        n = len(obj.get("path") or [])
    elif tool == "list_rulings":
        n = obj.get("ato_rulings_total") if isinstance(obj.get("ato_rulings_total"), int) else None
    if tool == "report_bad_recall":
        return upd
    upd["result_count"] = n
    upd["status"] = "empty" if n == 0 else "ok"
    return upd


def wrap_tools(mcp) -> None:
    """Wrap every registered FastMCP tool once so its call fills the request's event."""
    for t in mcp._tool_manager.list_tools():
        if getattr(t.fn, "_usage_wrapped", False):
            continue
        fn, is_async, name = t.fn, t.is_async, t.name

        @functools.wraps(fn)
        async def wrapper(*args, __fn=fn, __async=is_async, __name=name, **kwargs):
            ev = current.get()
            if ev is not None:
                ev.update(describe_call(__name, kwargs))
            try:
                out = (await __fn(*args, **kwargs)) if __async else __fn(*args, **kwargs)
            except BaseException as e:
                if ev is not None:
                    ev.update(status="error", error_kind=type(e).__name__, error_detail=str(e)[:1000])
                raise
            if ev is not None:
                ev.update(analyse_result(__name, ev, out))
                ev["_tool_ran"] = True
            return out

        wrapper._usage_wrapped = True
        t.fn, t.is_async = wrapper, True


# --- write -------------------------------------------------------------------------

_COLS = ("user_email", "tool", "transport", "params", "query", "content_type", "content_ref", "act",
         "result_count", "top_score", "exact_match", "citation_shaped", "latency_ms", "status",
         "error_kind", "error_detail", "request_id", "client_ip", "response_bytes", "feedback")


@safe(None)
def record(ev: dict) -> int | None:
    """Insert one event; never raises (logging must not break a call). Returns the row id."""
    row = {c: ev.get(c) for c in _COLS}
    row["params"] = params_text(ev.get("params"))
    for c in ("query", "content_ref", "error_detail"):
        if isinstance(row[c], str):
            row[c] = row[c][:1000]
    if ev.get("token_hashed"):
        row["token_hash"] = ev.get("token")          # already a sha256 digest
    else:
        row["token_hash"] = hashlib.sha256(ev["token"].encode()).hexdigest() if ev.get("token") else None
    row["created_at"] = ev.get("created_at") or time.time()
    with cursor() as cur:
        cur.execute(
            f"""INSERT INTO mcp_call_log (token_id, token_name, created_at, {', '.join(_COLS)})
                VALUES ((SELECT id FROM mcp_tokens WHERE token_hash = %(token_hash)s),
                        (SELECT name FROM mcp_tokens WHERE token_hash = %(token_hash)s),
                        %(created_at)s, {', '.join(f'%({c})s' for c in _COLS)})
                RETURNING id""",
            row,
        )
        return cur.fetchone()["id"]


# --- web search -----------------------------------------------------------------

def web_logged(tool: str, path: str, content_type=None):
    """Log a web search route as transport='http'. Only the request for `path` itself is
    logged, so nested calls (unified-search -> search, warm -> hybrid) don't double count."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(request, *args, **kwargs):
            if getattr(getattr(request, "url", None), "path", None) != path:
                return fn(request, *args, **kwargs)
            t0 = time.perf_counter()
            user = getattr(getattr(request, "state", None), "user", None) or {}
            q = kwargs.get("q")
            c = cited(q)
            qp = dict(request.query_params)
            ev = new_event("http", tool=tool, user_email=user.get("email"), query=q, params=qp,
                           act=_act_slug(kwargs.get("act") or "") or (c and c["act"]),
                           content_type=(content_type(kwargs) if content_type else "mixed"),
                           content_ref=c["ref"] if c else None, citation_shaped=bool(c),
                           client_ip=(request.headers.get("cf-connecting-ip")
                                      or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                                      or (request.client.host if request.client else None)))
            try:
                resp = fn(request, *args, **kwargs)
            except BaseException as e:
                ev.update(status="error", error_kind=type(e).__name__,
                          error_detail=str(getattr(e, "detail", "") or e)[:1000])
                raise
            else:
                items = [x for x in (resp.get("results") or []) if isinstance(x, dict)] if isinstance(resp, dict) else []
                if isinstance(resp, dict) and "categories" in resp:
                    n = sum(int(cat.get("count") or 0) for cat in resp["categories"])
                else:
                    n = int(resp.get("total") or len(items)) if isinstance(resp, dict) else None
                ev.update(result_count=n, status="empty" if n == 0 else "ok", top_score=_top_score(items),
                          exact_match=_exact(items, c and c["id"]),
                          response_bytes=len(json.dumps(resp, default=str)))
                return resp
            finally:
                ev["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                record(ev)
        return wrapper
    return deco
