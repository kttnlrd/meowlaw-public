"""MCP content gating fails closed.

Only the static operator token (MCP_AUTH_TOKEN / LEGISLATION_BEARER_TOKEN) and the stdio
transport are unrestricted. Every other HTTP request without a resolved owner (DEV_MODE, an
owner lookup that came back empty, an OAuth or token-table owner whose email reads "operator",
a usage event lost on the way to the tool) gets the anonymous view, never the operator view.

Admin-only registry types (commentary, insolvency, nz-mtg) are the signal: the operator sees
their rows in list_content_types, an anonymous caller does not.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import backend.fastmcp_server as fm
from backend.mcp_token_manager import token_manager
from backend.oauth_provider import provider as oauth_provider
from backend.services import content_access as ca
from backend.services import usage_log
from backend.tests import mcp_harness as h
from content.registry import REGISTRY

ADMIN_ONLY = {k for k, t in REGISTRY.items() if t["auth"] == "admin" and not t.get("hidden")}
USER_TYPES = {k for k, t in REGISTRY.items() if t["auth"] == "user"}
COMMENTARY_ACT = REGISTRY["commentary"]["data_markers"][0]


def _types(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {t["key"] for t in json.loads(h.tool_text(resp))["content_types"]}


@pytest.fixture
def wire(monkeypatch):
    with h.live_client(monkeypatch) as (client, events):
        yield client, events


def test_operator_token_is_unrestricted(wire):
    c, events = wire
    keys = _types(h.call(c, "list_content_types", token=h.OPERATOR_TOKEN))
    assert ADMIN_ONLY and ADMIN_ONLY <= keys and USER_TYPES <= keys
    assert events[-1]["user_email"] == "operator"  # the usage row is unchanged


def test_standard_owner_is_restricted(wire):
    c, _ = wire
    keys = _types(h.call(c, "list_content_types", token="tok-std"))
    assert USER_TYPES <= keys and not keys & ADMIN_ONLY


def test_dev_mode_without_owner_gets_anonymous_view(wire, monkeypatch):
    """DEV_MODE skips auth; the request reads as the web app's synthetic user (never the
    operator token), so MCP and the browser show the same content on a local instance."""
    c, events = wire
    monkeypatch.setattr(fm, "AUTH_DISABLED", True)
    keys = _types(h.call(c, "list_content_types", token=None))
    assert USER_TYPES <= keys
    from backend.auth import _synthetic_user
    assert events[-1].get("user_email") == _synthetic_user()["email"]


def test_empty_owner_from_token_table_gets_anonymous_view(wire, monkeypatch):
    c, _ = wire
    owners = {**h.OWNERS, "tok-blank": ""}
    monkeypatch.setattr(token_manager, "owner_of", lambda t: owners.get(t))
    keys = _types(h.call(c, "list_content_types", token="tok-blank"))
    assert not keys & ADMIN_ONLY


@pytest.mark.parametrize("source", ["oauth", "token_table"])
def test_owner_named_operator_is_not_the_operator(source, wire, monkeypatch):
    """Only the static token is the operator; an account whose email reads "operator" is not."""
    c, _ = wire
    if source == "oauth":
        monkeypatch.setattr(oauth_provider, "load_access_token",
                            lambda t: {"user_email": "operator"} if t == "tok-named" else None)
    else:
        monkeypatch.setattr(token_manager, "owner_of",
                            lambda t: "operator" if t == "tok-named" else h.OWNERS.get(t))
    keys = _types(h.call(c, "list_content_types", token="tok-named"))
    assert USER_TYPES <= keys and not keys & ADMIN_ONLY


def test_tool_call_without_event_while_serving_http_is_anonymous(wire):
    """A tool that runs under the HTTP app with its usage event lost fails closed."""
    assert fm._http_serving > 0  # the app lifespan is running
    assert fm._mcp_owner() == {"email": ""}
    keys = {t["key"] for t in json.loads(asyncio.run(fm.list_content_types()))["content_types"]}
    assert not keys & ADMIN_ONLY


# --------------------------------------------------------------------------- unit: _mcp_owner

def test_owner_resolution(monkeypatch):
    monkeypatch.setattr(fm, "_http_serving", 0)
    assert fm._mcp_owner() is None  # stdio / in-process: no usage event, no HTTP app
    cases = [
        ({"user_email": "operator", fm._OPERATOR_FIELD: fm._OPERATOR}, None),
        ({"user_email": "operator"}, {"email": "operator"}),
        ({"user_email": "operator", fm._OPERATOR_FIELD: "yes"}, {"email": "operator"}),
        ({"user_email": "a@example.com"}, {"email": "a@example.com"}),
        ({"user_email": ""}, {"email": ""}),
        ({"user_email": "  "}, {"email": ""}),
        ({"user_email": None}, {"email": ""}),
        ({}, {"email": ""}),
    ]
    for ev, want in cases:
        tok = usage_log.current.set(ev)
        try:
            assert fm._mcp_owner() == want, ev
        finally:
            usage_log.current.reset(tok)
    with fm.serving_http():
        assert fm._mcp_owner() == {"email": ""}
    assert fm._mcp_owner() is None


# --------------------------------------------------------------------------- search_all

@pytest.fixture
def two_sections(monkeypatch):
    rows = [{"act": COMMENTARY_ACT, "section": "1", "title": "c"},
            {"act": "itaa-1997", "section": "8-1", "title": "s"}]
    monkeypatch.setattr(fm, "_hybrid_sections", lambda q, act, limit: [dict(r) for r in rows])
    monkeypatch.setattr(ca, "_settings", lambda: {})
    monkeypatch.setattr(ca, "_grants", lambda email: set())
    monkeypatch.setattr(fm, "_http_serving", 0)


def _acts(ev) -> list[str]:
    tok = usage_log.current.set(ev) if ev is not None else None
    try:
        out = json.loads(asyncio.run(fm.search_all("x", types={"legislation"})))
    finally:
        if tok is not None:
            usage_log.current.reset(tok)
    return [r["act"] for r in out["results"]["sections"]]


def test_search_all_gate_fails_closed(two_sections):
    assert _acts(None) == [COMMENTARY_ACT, "itaa-1997"]  # stdio
    assert _acts({"user_email": "operator", fm._OPERATOR_FIELD: fm._OPERATOR}) == [COMMENTARY_ACT, "itaa-1997"]
    for ev in ({"user_email": None}, {"user_email": ""}, {"user_email": "operator"},
               {"user_email": "std@example.invalid"}):
        assert _acts(ev) == ["itaa-1997"], ev


# --------------------------------------------------------------------------- global OFF

def test_global_off_hides_a_type_from_the_operator_token(wire, monkeypatch):
    """A type switched OFF globally is hidden from the operator token; an admin keeps it
    (admins and grants override the global switch)."""
    c, _ = wire
    monkeypatch.setattr(ca, "_settings", lambda: {"commentary": False, "case": False})
    assert {"commentary", "case"} <= _types(h.call(c, "list_content_types", token="tok-admin"))
    for token in (h.OPERATOR_TOKEN,):
        keys = _types(h.call(c, "list_content_types", token=token))
        assert not keys & {"commentary", "case"} and "insolvency" in keys, token
        info = json.loads(h.tool_text(h.call(c, "get_info", token=token)))
        assert "commentary paragraph" not in info["usage"]["document_ids"]
        assert COMMENTARY_ACT not in info["usage"]["act_ids"]
    monkeypatch.setattr(ca, "_settings", lambda: {})
    keys = _types(h.call(c, "list_content_types", token=h.OPERATOR_TOKEN))
    assert {"commentary", "case"} <= keys


def test_global_off_applies_to_stdio(monkeypatch):
    monkeypatch.setattr(fm, "_http_serving", 0)
    monkeypatch.setattr(ca, "_settings", lambda: {"case": False})
    assert fm._mcp_owner() is None
    assert "case" not in fm._mcp_avail() and "commentary" in fm._mcp_avail()
    assert json.loads(fm._mcp_denied("case"))["error"] == "case is not available to your account"
    assert fm._mcp_denied("commentary") is None
    rows = [{"content_type": "case"}, {"content_type": "ruling"}, {"title": "untagged"}]
    assert fm._mcp_filter(rows) == rows[1:]


def test_enabled_keys(monkeypatch):
    monkeypatch.setattr(ca, "_settings", lambda: {"case": False, "maps": False})
    assert ca.enabled_keys() == set(ca.all_type_keys()) - {"case", "maps"}
    # DB outage: open-to-users types stay on, every private type is off (a global OFF on one
    # of them cannot be read, so none may come back)
    monkeypatch.setattr(ca, "_settings", lambda: None)
    private = {k for k in ca.all_type_keys() if ca.auth_of(k) == "admin"}
    assert private >= {"maps", "workflows", "chains", "commentary", "insolvency", "nz-mtg"}
    assert ca.enabled_keys() == set(ca.all_type_keys()) - private
    assert not ca.global_enabled("commentary") and ca.global_enabled("case")


def test_settings_outage_hides_private_types_from_operator_and_admin(wire, monkeypatch):
    """A Postgres blip must not undo a global OFF: with the settings table unreadable, the
    operator token and an ADMIN_EMAILS account lose every private type (MCP and REST)."""
    c, _ = wire
    private = {k for k in ca.all_type_keys() if ca.auth_of(k) == "admin"}
    for settings in ({"commentary": False}, None):
        monkeypatch.setattr(ca, "_settings", lambda s=settings: s)
        monkeypatch.setattr(ca, "_grants", lambda email: None if settings is None else set())
        for token in (h.OPERATOR_TOKEN, "tok-admin"):
            keys = _types(h.call(c, "list_content_types", token=token))
            assert USER_TYPES <= keys, (token, settings)
            if settings is None or token == h.OPERATOR_TOKEN:
                assert "commentary" not in keys, (token, settings)
            if settings is None:
                assert not keys & private, token
        if settings is None:  # outage: the admin loses it; a deliberate OFF it keeps
            assert "commentary" not in ca.available_keys({"email": h.OWNERS["tok-admin"]})
    assert not private & ca.available_keys({"email": h.OWNERS["tok-admin"]})
    monkeypatch.setattr(ca, "_settings", lambda: {})  # DB back, nothing switched off
    assert private <= ca.available_keys({"email": h.OWNERS["tok-admin"]})


# --------------------------------------------------------------------------- memo per call

@pytest.mark.parametrize("token", ["tok-std", "tok-admin", h.OPERATOR_TOKEN])
@pytest.mark.parametrize("name,args", [("get_info", {}), ("list_content_types", {}), ("search", {"query": "deduction"}),
                                       ("fetch", {"ref": "leg:itaa-1997:8-1"}), ("related", {"id": "leg:itaa-1997:8-1"})])
def test_availability_looked_up_once_per_call(name, args, token, wire, monkeypatch):
    """get_info used to open ~184 Postgres connections (two per _mcp_avail(), once per act)."""
    c, _ = wire
    n = {"settings": 0, "grants": 0}

    def settings():
        n["settings"] += 1
        return {}

    def grants(email):
        n["grants"] += 1
        return set(h.GRANTS.get(email, set()))
    monkeypatch.setattr(ca, "_settings", settings)
    monkeypatch.setattr(ca, "_grants", grants)
    for _ in range(2):  # a fresh memo per call: one lookup each, never zero
        before = dict(n)
        r = h.call(c, name, args, token=token)
        assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text
        assert n["settings"] - before["settings"] == 1, (name, n)
        assert n["grants"] - before["grants"] == (0 if token == h.OPERATOR_TOKEN else 1), (name, n)


def test_memo_does_not_outlive_the_call(wire, monkeypatch):
    c, _ = wire
    for token in (h.OPERATOR_TOKEN,):  # an admin keeps a type through a global OFF
        monkeypatch.setattr(ca, "_settings", lambda: {})
        assert "commentary" in _types(h.call(c, "list_content_types", token=token))
        monkeypatch.setattr(ca, "_settings", lambda: {"commentary": False})
        assert "commentary" not in _types(h.call(c, "list_content_types", token=token))
    # one caller's answer is never served to the next caller
    monkeypatch.setattr(ca, "_settings", lambda: {})
    assert "commentary" in _types(h.call(c, "list_content_types", token="tok-granted"))
    assert "commentary" not in _types(h.call(c, "list_content_types", token="tok-std"))


def test_memo_is_keyed_by_caller(monkeypatch):
    monkeypatch.setattr(fm, "_http_serving", 0)
    monkeypatch.setattr(ca, "_settings", lambda: {})
    monkeypatch.setattr(ca, "_grants", lambda email: {"commentary"} if email == "g@example.com" else set())
    tok = fm._avail_memo.set({})
    try:
        assert "commentary" in fm._mcp_avail()  # stdio
        for email, has in (("g@example.com", True), ("s@example.com", False), ("g@example.com", True)):
            ev = usage_log.current.set({"user_email": email})
            try:
                assert ("commentary" in fm._mcp_avail()) is has, email
            finally:
                usage_log.current.reset(ev)
        assert set(fm._avail_memo.get()) == {None, "g@example.com", "s@example.com"}
    finally:
        fm._avail_memo.reset(tok)
    assert fm._avail_memo.get() is None  # outside a tool call nothing is memoised
