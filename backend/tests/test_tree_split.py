"""Shallow trees: a very large part is sent as lazy divisions, and one division loads on its own."""
import json

from backend.routes import acts


def _tree():
    big = {"id": "1", "title": "FCA", "sections": [], "divisions": [
        {"id": f"1.{y}", "title": str(2026 - y), "sections": [{"id": f"{2026 - y}-fca-{n}"} for n in range(500)]}
        for y in range(5)]}
    small = {"id": "2", "title": "HCA", "sections": [{"id": f"hca-{n}"} for n in range(10)], "divisions": []}
    return {"act": "Case law", "parts": [big, small]}


def _patch(monkeypatch):
    monkeypatch.setattr(acts, "require_act", lambda request, act: None)
    monkeypatch.setattr(acts, "load_tree", lambda act: _tree())
    monkeypatch.setattr(acts, "licence_notice", lambda act: None)


def test_split_part_sends_lazy_divisions(monkeypatch):
    _patch(monkeypatch)
    tree = json.loads(acts.get_tree("case-law", None, shallow=True).body)
    fca, hca = tree["parts"]
    assert tree["shallow"] and not fca.get("lazy")
    assert [(d["title"], d["count"], d["lazy"], d["sections"]) for d in fca["divisions"]][0] == ("2026", 500, True, [])
    assert hca["lazy"] and hca["count"] == 10


def test_open_keeps_only_the_division_holding_the_section(monkeypatch):
    _patch(monkeypatch)
    tree = json.loads(acts.get_tree("case-law", None, shallow=True, open="2024-fca-7").body)
    divs = tree["parts"][0]["divisions"]
    assert [d["title"] for d in divs if not d.get("lazy")] == ["2024"]
    assert len(divs[2]["sections"]) == 500


def test_part_endpoint_returns_a_division_by_id_or_section(monkeypatch):
    _patch(monkeypatch)
    assert acts.get_tree_part("case-law", None, id="1.3")["title"] == "2023"
    assert acts.get_tree_part("case-law", None, section="2022-fca-1")["id"] == "1.4"
    assert acts.get_tree_part("case-law", None, section="hca-3")["id"] == "2"  # small part: whole part
