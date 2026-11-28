"""MCP graph tools and `related` accept every id form a client is likely to send."""
import asyncio
import json
import sqlite3

import pytest

import backend.fastmcp_server as fm
from backend.services import content_access as ca
from backend.services.mcp_ids import graph_key_candidates, related_doc_id


# --- pure normalisers -------------------------------------------------------

@pytest.mark.parametrize("raw,first", [
    ("section:itaa-1997:8-1", "section:itaa-1997:8-1"),
    ("leg:itaa-1997:8-1", "section:itaa-1997:8-1"),
    ("leg:itaa-1997:855-10(1)", "section:itaa-1997:855-10"),
    ("itaa-1997:8-1", "section:itaa-1997:8-1"),
    ("s 8-1 ITAA 1997", "section:itaa-1997:8-1"),
    ("case:2025-fcafc-116", "case:[2025] FCAFC 116"),
    ("[2015] HCA 48", "case:[2015] HCA 48"),
    ("ruling:tr-2024-1", "public_ruling:TR 2024/1"),
    ("TR 2024/1", "public_ruling:TR 2024/1"),
    ("pbr:1051234567890", "private_ruling:EV/1051234567890"),
    ("rg:146", "regulatory_guide:146"),
    ("public_ruling:TR 2025/1", "public_ruling:TR 2025/1"),
    ("private_ruling:EV/1011261243735", "private_ruling:EV/1011261243735"),
    ("commentary:MTG:ch12", "commentary:MTG:ch12"),
])
def test_graph_key_candidates(raw, first):
    assert graph_key_candidates(raw)[0] == first


def test_bracket_case_key_also_tries_dashed():
    assert "case:2025-fcafc-116" in graph_key_candidates("case:[2025] FCAFC 116")


def test_psla_ruling_has_spaced_variant():
    assert "public_ruling:PS LA 2011/12" in graph_key_candidates("ruling:psla-2011-12")


@pytest.mark.parametrize("raw,want", [
    ("itaa-1997:8-1", "leg:itaa-1997:8-1"),
    ("section:itaa-1997:8-1", "leg:itaa-1997:8-1"),
    ("leg:itaa-1997:8-1", "leg:itaa-1997:8-1"),
    ("case:[2025] FCAFC 116", "case:2025-fcafc-116"),
    ("private_ruling:EV/123", "pbr:123"),
    ("rg:146", "rg:146"),
    ("garbage", "garbage"),
])
def test_related_doc_id(raw, want):
    assert related_doc_id(raw) == want


# --- tools ------------------------------------------------------------------

def _run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture
def owner(monkeypatch):
    """Switch gating on with a given available set."""
    def _set(avail):
        monkeypatch.setattr(fm, "_mcp_avail", lambda: avail)
        monkeypatch.setattr(fm, "_mcp_owner", lambda: {"id": "t"})
    return _set


@pytest.fixture
def seen(monkeypatch):
    calls = []
    import backend.services.graph_serialize as gs

    def fake(key, depth=1, deny=frozenset()):
        calls.append(key)
        return {"key": key, "depth": depth} if key == "section:itaa-1997:8-1" else None
    monkeypatch.setattr(gs, "serialize", fake)
    return calls


@pytest.mark.parametrize("form", [
    "section:itaa-1997:8-1", "leg:itaa-1997:8-1", "itaa-1997:8-1", "s 8-1 ITAA 1997",
])
def test_neighbourhood_accepts_forms(form, owner, seen):
    owner({"legislation"})
    out = _run(fm.graph_neighbourhood(form))
    assert out["key"] == "section:itaa-1997:8-1"


def test_neighbourhood_unknown_still_errors(owner, seen):
    owner({"legislation"})
    assert "unknown graph key" in _run(fm.graph_neighbourhood("leg:itaa-1997:999-999"))["error"]


@pytest.mark.parametrize("form", [
    "leg:itaa-1997:8-1", "itaa-1997:8-1", "section:itaa-1997:8-1", "insolvency:keays:ch-12",
    "commentary:MTG:ch12",
])
def test_neighbourhood_refuses_gated(form, owner, seen):
    owner({"case"})  # no legislation, insolvency or commentary
    out = _run(fm.graph_neighbourhood(form))
    assert "unknown graph key" in out["error"]
    assert seen == []  # refused before any lookup


@pytest.fixture
def memdb(monkeypatch):
    real = sqlite3.connect

    def factory(*a, **k):
        c = real(":memory:")
        c.execute("create table nodes(id integer primary key, key text, label text)")
        c.executemany("insert into nodes values(?,?,?)", [
            (1, "section:itaa-1997:8-1", "s 8-1"), (2, "public_ruling:TR 2024/1", "TR 2024/1")])
        return c
    monkeypatch.setattr(sqlite3, "connect", factory)
    import backend.services.graph_path as gp
    monkeypatch.setattr(gp, "find_path", lambda conn, a, b, max_hops=10: ([(a, None), (b, None)], 1))


@pytest.mark.parametrize("a,b", [
    ("leg:itaa-1997:8-1", "ruling:tr-2024-1"),
    ("itaa-1997:8-1", "TR 2024/1"),
    ("section:itaa-1997:8-1", "public_ruling:TR 2024/1"),
])
def test_path_accepts_forms(a, b, owner, memdb):
    owner({"legislation", "ruling"})
    out = _run(fm.graph_path(a, b))
    assert out["hops"] == 1


def test_path_refuses_gated_before_lookup(owner, memdb):
    owner({"legislation"})  # no ruling
    out = _run(fm.graph_path("leg:itaa-1997:8-1", "ruling:tr-2024-1"))
    assert "unknown graph key" in out["error"]
    owner(set())
    assert "unknown graph key" in _run(fm.graph_path("itaa-1997:8-1", "itaa-1997:8-1"))["error"]


@pytest.fixture
def relnone(monkeypatch):
    for n in ("_graph_cases_for_section", "_graph_rulings_for_section", "_graph_private_rulings_for_section"):
        monkeypatch.setattr(fm, n, lambda *a, **k: [])


@pytest.mark.parametrize("form", ["leg:itaa-1997:8-1", "itaa-1997:8-1", "section:itaa-1997:8-1"])
def test_related_accepts_forms(form, owner, relnone):
    owner({"legislation"})
    out = _run(fm.related(form))
    assert "error" not in out and out["type"] == "legislation"


def test_related_rejects_garbage(owner, relnone):
    owner({"legislation"})
    assert "Invalid id" in _run(fm.related("not an id"))["error"]


@pytest.mark.parametrize("form", ["leg:itaa-1997:8-1", "itaa-1997:8-1", "section:itaa-1997:8-1"])
def test_related_refuses_gated(form, owner, relnone):
    owner({"case"})
    assert "not available" in _run(fm.related(form))["error"]


# --- regressions: forms fetch accepts must reach the graph node (adversarial review) ----------

@pytest.mark.parametrize("raw,want", [
    # act-first section citations were read as the bracketless case "[1997] S 8-1"
    ("ITAA 1997 s 8-1", ["section:itaa-1997:8-1"]),
    ("Income Tax Assessment Act 1997 s 8-1", ["section:itaa-1997:8-1"]),
    ("ITAA 1936 s 6", ["section:itaa-1936:6"]),
    ("Corporations Act 2001 s 601ED", ["section:corporations-act-2001:601ED"]),
    ("ITAA 1997 s 104-10", ["section:itaa-1997:104-10"]),
    ("2015 HCA 48", ["case:[2015] HCA 48"]),  # bracketless case still recognised
    # multi-word and 5-letter series kept whole; drafts and IT (no year) mapped
    ("ATO ID 2001/1", ["public_ruling:ATOID 2001/1", "public_ruling:AID 2001/1"]),
    ("ATOID 2010/12", ["public_ruling:ATOID 2010/12", "public_ruling:AID 2010/12"]),
    ("TD 2026/D1", ["public_ruling:TD 2026/D1"]),
    ("IT 2621", ["public_ruling:IT 2621"]),
    ("Taxation Ruling TR 2024/1", ["public_ruling:TR 2024/1"]),
    # NZ rulings are keyed nz_ruling:<STEM> (graph_etl.load_nz_rulings)
    ("ruling:brprd-2004-1", ["nz_ruling:BRPRD_2004_1"]),
    ("BR Prd 04/01", ["nz_ruling:BRPRD_2004_1"]),
    ("BR Pub 24/01", ["nz_ruling:BRPUB_2024_1"]),
    ("QB 15/11", ["nz_ruling:QB_2015_11"]),
    # NZ lettered sections are hyphenated in the graph
    ("leg:nz-it-2007:CB1", ["section:nz-it-2007:CB-1"]),
    ("nz-it-2007:CB1", ["section:nz-it-2007:CB-1"]),
    ("s CB 1 NZ IT Act", ["section:nz-it-2007:CB-1"]),
    # bare <act>:<section> for every act with a tree, not only doc_ids.ACT_SLUGS
    ("bankruptcy-act-1966:58", ["section:bankruptcy-act-1966:58"]),
    ("fair-work-2009:3", ["section:fair-work-2009:3"]),
    ("aml-ctf-rules-2025:1", ["section:aml-ctf-rules-2025:1"]),
    # rg:<n>:<para> short form; fetch resolves rg:146:1 to paragraph 146.1
    ("rg:146:1", ["regulatory_guide:146:1", "regulatory_guide:146:146.1"]),
    ("not-an-act-dir:5", ["not-an-act-dir:5"]),
])
def test_graph_key_candidates_regressions(raw, want):
    assert graph_key_candidates(raw) == want


@pytest.mark.parametrize("raw", ["PS LA 2005/24", "PSLA 2005/24", "ruling:psla-2005-24"])
def test_psla_plain_citation_keeps_series(raw):
    assert "public_ruling:PS LA 2005/24" in graph_key_candidates(raw)


@pytest.mark.parametrize("raw,want", [
    ("bankruptcy-act-1966:58", "leg:bankruptcy-act-1966:58"),
    ("fair-work-2009:3", "leg:fair-work-2009:3"),
    ("section:fair-work-2009:3", "leg:fair-work-2009:3"),
    ("leg:nz-it-2007:CB1", "leg:nz-it-2007:CB-1"),
    ("leg:itaa-1997:8-1(1)", "leg:itaa-1997:8-1(1)"),
    ("afsa:some-guide", "afsa:some-guide"),
    ("dta:gb:7", "dta:gb:7"),
])
def test_related_doc_id_regressions(raw, want):
    assert related_doc_id(raw) == want


@pytest.fixture
def etl_graph(tmp_path, monkeypatch):
    """A small graph.db with the real ETL schema and key builders, read by the real
    serialize/find_path code (adapted from the adversarial repro)."""
    import backend.services.graph_neighborhood as gn
    import backend.services.graph_serialize as gs
    from pipeline.graph_etl import SCHEMA, ruling_label

    nodes = [
        (1, "section", "section:itaa-1997:8-1", "s 8-1 itaa-1997"),
        (2, "public_ruling", "public_ruling:" + ruling_label("PSLA_2011_12"), "PS LA 2011/12"),
        (3, "public_ruling", "public_ruling:" + ruling_label("TR_2024_1"), "TR 2024/1"),
        (4, "public_ruling", "public_ruling:" + ruling_label("TD_2026_D1"), "TD 2026/D1"),
        (5, "public_ruling", "public_ruling:" + ruling_label("IT_2167"), "IT 2167"),
        (6, "public_ruling", "nz_ruling:BRPRD_2004_1", "BR Prd 04/01"),
        (7, "section", "section:nz-it-2007:CB-1", "s CB-1 nz-it-2007"),
        (8, "public_ruling", "public_ruling:" + ruling_label("ATOID_2010_12"), "ATOID 2010/12"),
        (9, "section", "section:bankruptcy-act-1966:58", "s 58 bankruptcy-act-1966"),
    ]
    edges = [(1, 2, "interpreted_by"), (1, 3, "interpreted_by"), (1, 4, "interpreted_by"),
             (1, 5, "interpreted_by"), (1, 8, "interpreted_by"), (2, 1, "applies"),
             (3, 1, "applies"), (4, 1, "applies"), (5, 1, "applies"), (8, 1, "applies"),
             (6, 7, "applies"), (7, 6, "interpreted_by"), (9, 1, "cites")]
    db = tmp_path / "graph.db"
    real = sqlite3.connect
    c = real(db)
    c.executescript(SCHEMA)
    c.executemany("insert into nodes(id,node_type,key,label) values(?,?,?,?)", nodes)
    c.executemany("insert into graph_edges(source_id,target_id,edge_type,source_doc) "
                  "values(?,?,?,'x')", edges)
    c.commit()
    c.close()
    monkeypatch.setattr(gs, "GRAPH_DB", db)
    monkeypatch.setattr(gn, "GRAPH_DB", db)

    def connect(path, *a, **k):  # graph_path opens <repo>/data/graph.db directly
        if str(path).endswith("graph.db") and "mode=ro" not in str(path):
            return real(db, *a, **k)
        return real(path, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(fm, "_mcp_avail", lambda: set(ca.all_type_keys()))  # operator, all types ON
    return db


@pytest.mark.parametrize("form,key", [
    ("PS LA 2011/12", "public_ruling:PS LA 2011/12"),
    ("ATO ID 2010/12", "public_ruling:ATOID 2010/12"),
    ("TD 2026/D1", "public_ruling:TD 2026/D1"),
    ("IT 2167", "public_ruling:IT 2167"),
    ("ruling:brprd-2004-1", "nz_ruling:BRPRD_2004_1"),
    ("BR Prd 04/01", "nz_ruling:BRPRD_2004_1"),
    ("leg:nz-it-2007:CB1", "section:nz-it-2007:CB-1"),
    ("ITAA 1997 s 8-1", "section:itaa-1997:8-1"),
    ("bankruptcy-act-1966:58", "section:bankruptcy-act-1966:58"),
])
def test_neighbourhood_real_graph(etl_graph, form, key):
    out = _run(fm.graph_neighbourhood(form))
    assert "error" not in out, out
    assert key in json.dumps(out)


@pytest.mark.parametrize("a,b", [
    ("PS LA 2011/12", "ITAA 1997 s 8-1"),
    ("bankruptcy-act-1966:58", "TD 2026/D1"),
    ("ruling:brprd-2004-1", "leg:nz-it-2007:CB1"),
])
def test_path_real_graph(etl_graph, a, b):
    out = _run(fm.graph_path(a, b, 3))
    assert "error" not in out and out["hops"] is not None, out


def test_nz_ruling_id_gated_as_nz_ruling(owner, seen):
    owner({"ruling", "legislation"})  # AU rulings only
    assert "unknown graph key" in _run(fm.graph_neighbourhood("ruling:brprd-2004-1"))["error"]
    assert "unknown graph key" in _run(fm.graph_neighbourhood("BR Prd 04/01"))["error"]
    assert seen == []


def test_act_first_citation_gated_as_legislation(owner, seen):
    owner({"case"})  # a case grant must not open a legislation citation
    assert "unknown graph key" in _run(fm.graph_neighbourhood("ITAA 1997 s 8-1"))["error"]
    assert seen == []
    owner({"legislation"})
    assert _run(fm.graph_neighbourhood("ITAA 1997 s 8-1"))["key"] == "section:itaa-1997:8-1"


@pytest.mark.parametrize("form", ["keays:ch-12", "master-tax-guide:ch12", "nz-master-tax-guide:1"])
def test_bare_form_of_gated_tree_stays_gated(form, owner, seen, monkeypatch):
    import backend.services.mcp_ids as mi
    monkeypatch.setattr(mi, "_is_act", lambda slug: True)  # as if the tree were on disk
    owner({"legislation", "case", "ruling"})  # no commentary, insolvency or nz-mtg
    assert "unknown graph key" in _run(fm.graph_neighbourhood(form))["error"]
    assert seen == []


def test_hostile_long_input_is_cheap():
    import time

    for s in ("s 1" + "-s1" * 16000 + "!", "public_ruling:a" + " " * 48000 + "x",
              "TR 2024" + " " * 48000 + "/", "AB 12" + "_" * 48000):
        t0 = time.perf_counter()
        assert graph_key_candidates(s) == [s]
        assert related_doc_id(s) == s.strip()
        assert time.perf_counter() - t0 < 0.5


# --- the live test and audit spec must target sections that exist -----------------------------

def test_live_graph_path_targets_exist():
    import re as _re
    from pathlib import Path
    from backend.services.data_loader import find_section_path

    root = Path(__file__).resolve().parents[2]
    live = (root / "backend/tests/test_mcp_comprehensive.py").read_text(encoding="utf-8")
    spec = json.loads((root / "specs/audit-lawkitty.json").read_text(encoding="utf-8"))
    targets = _re.findall(r'm\.tool\("graph_path", \{"from_key": "([^"]+)", "to_key": "([^"]+)"', live)
    args = spec["mcp_calls"]["graph_path"]["args"]
    targets.append((args["from_key"], args["to_key"]))
    assert len(targets) >= 2
    for pair in targets:
        for ref in pair:
            key = graph_key_candidates(ref)[0]
            _, act, sec = key.split(":", 2)
            assert find_section_path(act, sec), f"{ref} -> {key}: no such section in data/{act}"
