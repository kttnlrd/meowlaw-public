"""Cases carry the full judgment from the case-law collection, and a long fetch is paged, not cut off."""
import asyncio
import json

from backend import resolve_ref as rr
from backend import fastmcp_server as srv

META = {"id": "case:2015-hca-48", "type": "case", "citation": "[2015] HCA 48", "text": ""}
BODY = "**A v B** [2015] HCA 48\n\n---\n\n## Judgment\n\n" + "\n\n".join(
    f"[{n}] " + "Reasons for judgment. " * 40 for n in range(1, 400))


def test_case_fetch_carries_the_judgment(monkeypatch):
    monkeypatch.setattr(rr, "_resolve_case_meta", lambda c: dict(META))
    monkeypatch.setattr(rr.data_loader, "get_act_section_content", lambda act, sec: ({}, BODY))
    out = rr._resolve_case("[2015] HCA 48")
    assert out["collection"] == "case-law" and out["text_source"] == "judgment" and "[399]" in out["text"]
    assert out["id"] == "case:2015-hca-48" and out["summary_is_generated"] is False


def test_ai_summary_stays_structured_beside_the_judgment(monkeypatch):
    summary = {"case_name": "A v B", "outcome": "dismissed"}
    monkeypatch.setattr(rr, "_resolve_case_meta", lambda c: {
        **META, "summary": summary, "summary_is_generated": True, "text": "[AI-GENERATED SUMMARY ...]"})
    monkeypatch.setattr(rr.data_loader, "get_act_section_content", lambda act, sec: ({}, BODY))
    out = rr._resolve_case("[2015] HCA 48")
    assert out["summary"] == summary and out["summary_is_generated"] is False
    assert not out["text"].startswith("[AI-GENERATED") and "## Judgment" in out["text"]


def test_case_without_judgment_keeps_metadata(monkeypatch):
    monkeypatch.setattr(rr, "_resolve_case_meta", lambda c: dict(META))

    def missing(act, sec):
        raise FileNotFoundError(sec)

    monkeypatch.setattr(rr.data_loader, "get_act_section_content", missing)
    out = rr._resolve_case("[2015] HCA 48")
    assert "collection" not in out and out["text"] == ""


def _fetch(monkeypatch, **kw):
    import backend.resolve_ref
    monkeypatch.setattr(backend.resolve_ref, "resolve_ref", lambda ref: {**META, "text": BODY})
    monkeypatch.setattr(srv, "_deny_ref", lambda ref: None)
    monkeypatch.setattr(srv, "_deny_resolved", lambda result, ref=None: None)
    fn = getattr(srv.fetch, "__wrapped__", srv.fetch)
    return json.loads(asyncio.run(fn(ref="[2015] HCA 48", **kw)))


def test_fetch_all_is_capped_and_says_where_to_continue(monkeypatch):
    d = _fetch(monkeypatch, chunk="all")
    total = d["total_chunks"]
    assert total > 10 and len(d["text"]) <= srv.FETCH_MAX_CHARS + 10_000
    assert d["next_chunk"] and d["next_chunk"] < total and f"chunk={d['next_chunk']}" in d["notice"]


def test_single_chunks_page_to_the_end(monkeypatch):
    d = _fetch(monkeypatch, chunk=1)
    assert d["chunk"] == 1 and d["next_chunk"] == 2
    last = _fetch(monkeypatch, chunk=d["total_chunks"])
    assert last["next_chunk"] is None and "[399]" in last["text"]
