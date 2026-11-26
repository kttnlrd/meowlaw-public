"""Usage analytics: capture (real MCP path), aggregates, filters, facets, admin gate, export.

Fixture rows live in a private window (Jan 2001) under unique emails and are deleted
afterwards, so the dev database's real usage is never mixed into the assertions.
"""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()
for k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET"):
    os.environ.pop(k, None)
os.environ["RATE_LIMIT_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402
from backend.auth import create_session_token, store  # noqa: E402
from backend.db import cursor  # noqa: E402
from backend.mcp_token_manager import token_manager  # noqa: E402
from backend.routes.admin import ADMIN_EMAIL  # noqa: E402
from backend.services import usage_log  # noqa: E402

client = TestClient(app)
RUN = uuid.uuid4().hex[:8]
U1, U2 = f"pytest-usage-1-{RUN}@example.invalid", f"pytest-usage-2-{RUN}@example.invalid"
T0 = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
# Scoped to this run's users too, so a concurrent run of this file can't leak in.
DATES = "from=2001-01-01T00:00:00Z&to=2001-02-01T00:00:00Z"
WINDOW = f"{DATES}&user={U1}&user={U2}"


def _cookie(email):
    return {"Cookie": f"session={create_session_token({'email': email, 'sub': email})}"}


ADMIN, USER = _cookie(ADMIN_EMAIL), _cookie(U1)

# (user, tool, status, result_count, latency, citation_shaped, exact_match, content_type, content_ref, act, query, feedback, day)
ROWS = [
    (U1, "search", "ok", 10, 100, False, False, "mixed", None, None, "general deductions", None, 0),
    (U1, "search", "ok", 9, 200, True, False, "ruling", "CR 2002/71", None, "CR 2002/71", None, 0),   # failed recall: cited, no exact
    (U1, "search", "empty", 0, 300, False, False, "mixed", None, None, "qqzz", None, 1),               # failed recall: zero
    (U1, "fetch", "ok", 1, 400, True, True, "section", "s 8-1 ITAA 1997", "itaa-1997", "s 8-1 ITAA 1997", None, 1),
    (U1, "fetch", "error", None, 500, True, None, "ruling", "TR 1899/999", None, "TR 1899/999", None, 2),  # failed recall: error
    (U2, "fetch", "ok", 1, 600, True, True, "section", "s 8-1 ITAA 1997", "itaa-1997", "s 8-1 ITAA 1997", "bad", 2),  # failed: feedback
    (U2, "web_search_hybrid", "ok", 50, 700, False, False, "mixed", None, None, "division 7a", None, 3),
]


@pytest.fixture(scope="module", autouse=True)
def rows():
    ids = []
    for i, (u, tool, st, n, ms, cit, ex, ct, ref, act, q, fb, day) in enumerate(ROWS):
        ids.append(usage_log.record({
            "created_at": T0 + day * 86400 + i, "user_email": u, "tool": tool,
            "transport": "http" if tool.startswith("web_") else "mcp", "status": st, "result_count": n,
            "latency_ms": ms, "citation_shaped": cit, "exact_match": ex, "content_type": ct,
            "content_ref": ref, "act": act, "query": q, "feedback": fb, "params": {"query": q},
            "error_kind": "RuntimeError" if st == "error" else None, "request_id": f"t{RUN}{i}"}))
    with cursor() as cur:  # one legacy-shaped row (tool IS NULL) in the window
        cur.execute("INSERT INTO mcp_call_log (token_id, token_name, created_at, user_email) "
                    "VALUES (NULL, 'legacy', %s, %s) RETURNING id", (T0 + 5, U1))
        ids.append(cur.fetchone()["id"])
    yield ids
    with cursor() as cur:
        cur.execute("DELETE FROM mcp_call_log WHERE id = ANY(%s) OR user_email IN (%s, %s)", (ids, U1, U2))
        cur.execute("DELETE FROM mcp_tokens WHERE created_by IN (%s, %s)", (U1, U2))
        cur.execute("DELETE FROM users WHERE email IN (%s, %s)", (U1, U2))


def _get(path, who=ADMIN):
    return client.get(path, headers=who)


def test_summary_kpis_match_rows():
    r = _get(f"/api/admin/usage/summary?{WINDOW}")
    assert r.status_code == 200
    k = r.json()["kpis"]
    assert k["total_calls"] == 7 and k["unique_users"] == 2
    assert (k["ok"], k["empty"], k["error"]) == (5, 1, 1)
    assert k["failed_recall"] == 4 and k["zero_results"] == 1
    assert k["latency_p50_ms"] == 400 and k["latency_max_ms"] == 700
    assert r.json()["legacy_rows_in_range"] == 1 and r.json()["includes_legacy"] is False
    assert "citation-shaped" in r.json()["failed_recall_definition"]
    # breakdown sums reconcile with the total
    b = r.json()["breakdowns"]
    assert sum(x["calls"] for x in b["user"]) == sum(x["calls"] for x in b["tool"]) == 7
    assert sum(x["calls"] for x in b["hour"]) == 7
    assert sum(x["total"] for x in r.json()["series"]) == 7
    assert r.json()["grain"] == "day"


def test_include_legacy_counts_the_legacy_row():
    k = _get(f"/api/admin/usage/summary?{WINDOW}&include_legacy=true").json()["kpis"]
    assert k["total_calls"] == 8


@pytest.mark.parametrize("qs,expected", [
    ("user=" + U2, 2),  # narrows the window's two users to one
    ("tool=fetch", 3),
    ("tool=fetch&tool=search", 6),
    ("status=failed_recall", 4),
    ("status=error&status=empty", 2),
    ("content_type=section&act=itaa-1997", 2),
    ("q=CR%202002", 1),
    ("user=" + U1 + "&status=failed_recall", 3),
])
def test_filters_events_total_and_rows(qs, expected):
    scope = DATES if "user=" in qs else WINDOW
    e = _get(f"/api/admin/usage/events?{scope}&{qs}&limit=200").json()
    assert e["total"] == expected == len(e["events"])
    s = _get(f"/api/admin/usage/summary?{scope}&{qs}").json()["kpis"]
    assert s["total_calls"] == expected


def test_events_sort_and_page():
    e = _get(f"/api/admin/usage/events?{WINDOW}&sort=latency_ms&dir=asc&limit=2&offset=1").json()
    assert [x["latency_ms"] for x in e["events"]] == [200, 300] and e["total"] == 7
    assert _get(f"/api/admin/usage/events?{WINDOW}&sort=bogus").status_code == 400
    assert _get(f"/api/admin/usage/events?{WINDOW}&limit=5000").json()["limit"] == 200


def test_facets_count_against_other_filters_and_keep_zero_values():
    f = _get(f"/api/admin/usage/facets?{WINDOW}&tool=web_search_hybrid").json()["facets"]
    users = {x["value"]: x["count"] for x in f["user"]}
    assert users[U2] == 1 and users[U1] == 0          # U1 kept, at zero (UI dims it)
    tools = {x["value"]: x["count"] for x in f["tool"]}
    assert tools["fetch"] == 3                         # tool facet ignores its own filter


def test_content_lists():
    c = _get(f"/api/admin/usage/content?{WINDOW}").json()
    assert c["sections"][0] == {**c["sections"][0], "label": "s 8-1 ITAA 1997", "calls": 2, "users": 2}
    assert [x["label"] for x in c["zero_result_queries"]] == ["qqzz"]
    assert {x["label"] for x in c["failed_recall_queries"]} >= {"cr 2002/71", "qqzz", "tr 1899/999"}


def test_empty_range_is_zero_not_error():
    r = _get("/api/admin/usage/summary?from=1999-01-01&to=1999-01-02")
    assert r.status_code == 200
    assert r.json()["kpis"]["total_calls"] == 0 and r.json()["kpis"]["error_rate"] is None
    assert r.json()["grain"] == "hour" and all(x["total"] == 0 for x in r.json()["series"])
    assert _get("/api/admin/usage/events?from=1999-01-01&to=1999-01-02").json()["total"] == 0


def test_admin_gate():
    for path in ("summary", "events", "facets", "content", "export.csv"):
        assert _get(f"/api/admin/usage/{path}", USER).status_code == 403
        assert client.get(f"/api/admin/usage/{path}").status_code == 401
    assert _get(f"/api/admin/usage/user/{U2}", USER).status_code == 403   # someone else
    assert _get(f"/api/admin/usage/user/{U1}", USER).status_code == 200   # self
    assert _get(f"/api/admin/usage/user/{U2}").status_code == 200         # admin


def test_export_csv_matches_events():
    r = _get(f"/api/admin/usage/export.csv?{WINDOW}&tool=fetch")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,created_at,user_email") and len(lines) == 1 + 3


def test_recall_feedback_owner_and_admin_only(rows):
    eid = rows[0]
    assert client.post("/api/recall-feedback", json={"event_id": eid, "feedback": "bad"}, headers=_cookie(U2)).status_code == 404
    assert client.post("/api/recall-feedback", json={"event_id": eid, "feedback": "meh"}, headers=USER).status_code == 400
    assert client.post("/api/recall-feedback", json={"event_id": eid, "feedback": "bad"}, headers=USER).json()["feedback"] == "bad"
    assert _get(f"/api/admin/usage/summary?{WINDOW}").json()["kpis"]["failed_recall"] == 5
    assert client.post("/api/recall-feedback", json={"event_id": eid, "feedback": None}, headers=ADMIN).status_code == 200


def test_stats_keys_unchanged():
    assert set(_get("/api/admin/stats").json()) == {
        "total_users", "by_tier", "active_7d", "total_mcp_tokens", "total_mcp_calls",
        "mcp_calls_by_user", "signups_per_day", "mcp_calls_per_day"}


def test_redaction_and_cap():
    p = usage_log.params_text({"query": "x " * 5000, "_auth": "abc", "h": "f" * 64, "nested": {"api_key": "k"}})
    assert "abc" not in p and "f" * 64 not in p and len(p.encode()) <= usage_log.PARAMS_MAX
    assert usage_log.params_text({"query": "s 8-1", "token": "zzz"}) == '{"query": "s 8-1", "token": "[REDACTED]"}'


def test_real_mcp_calls_are_logged_once_each_with_status():
    """Through the real /mcp route + auth middleware + tool wrapper."""
    store.upsert_user(U1, "pytest", "pytest", None)
    tok = token_manager.create_token(name="pytest", created_by=U1)
    since = time.time()
    hdr = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"}

    def call(name, args, headers=hdr):
        return life.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                         "params": {"name": name, "arguments": args}})

    with TestClient(app, base_url="http://127.0.0.1:8000") as life:  # lifespan runs the MCP session manager
        assert call("fetch", {"ref": "s 8-1 ITAA 1997"}).status_code == 200
        call("no_such_tool", {})
        call("report_bad_recall", {"query": "CR 2002/71", "expected": "CR 2002/71"})
        assert call("fetch", {"ref": "x"}, headers={"Accept": hdr["Accept"]}).status_code == 401
    with cursor() as cur:
        cur.execute("SELECT tool, status, error_kind, content_ref, feedback, params, user_email, token_id "
                    "FROM mcp_call_log WHERE created_at >= %s AND (user_email = %s OR error_kind = 'auth_missing') "
                    "ORDER BY id", (since, U1))
        got = [dict(r) for r in cur.fetchall()]
    assert [(g["tool"], g["status"], g["error_kind"]) for g in got] == [
        ("fetch", "ok", None), ("no_such_tool", "error", "UnknownTool"),
        ("report_bad_recall", "empty", None), ("fetch", "error", "auth_missing")]
    assert got[0]["content_ref"] == "s 8-1 ITAA 1997" and got[0]["token_id"] is not None
    assert got[2]["feedback"] == "bad"
    assert all(tok not in (g["params"] or "") for g in got)
    # the rejection row does not count toward the daily cap; the three real calls do
    assert token_manager.daily_usage(U1, "trial")["used"] == 3
    with cursor() as cur:
        cur.execute("DELETE FROM mcp_call_log WHERE created_at >= %s AND error_kind = 'auth_missing' "
                    "AND client_ip = 'testclient'", (since,))
