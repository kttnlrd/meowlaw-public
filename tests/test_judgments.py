"""AustLII extraction and the judgments adapter."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ex = load("scripts/austlii_extract.py", "austlii_extract")

PAGE = """<!DOCTYPE html><html><head><title>A v B [2015] HCA 48 (10 December 2015)</title></head>
<body><header><div id="primary">Menu Cases &amp; Legislation</div></header>
<div id="page-main"><article class="the-document">
<!--make_database: source=/somewhere/2015_48.html-->
<p>HIGH COURT OF
AUSTRALIA</p>
<table><tr><td><p>Judge:</p></td><td><p>SMITH
J</p></td></tr></table>
<ol><li value="1">The appeal
concerns s 254.</li><li value="2">It is dismissed.</li></ol>
</article></div>
<div id="page-side">PRINT Download RTF</div><footer>AustLII: Copyright Policy</footer></body></html>"""


def test_extract_html_keeps_the_judgment_only(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "2015_HCA_48.html").write_text(PAGE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    row = ex.work((str(src / "2015_HCA_48.html"), str(out)))
    assert row["citation"] == "[2015] HCA 48" and row["date"] == "2015-12-10" and row["court"] == "HCA"
    text = (out / "2015_HCA_48.txt").read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    assert "HIGH COURT OF AUSTRALIA" in body            # source line wraps folded
    assert "Judge: | SMITH J" in body                   # table row on one line
    assert "[1] The appeal concerns s 254." in body and "[2] It is dismissed." in body
    for junk in ("make_database", "Menu", "PRINT", "Copyright Policy"):
        assert junk not in body


def test_extract_txt_dump_cuts_menus_and_side_panel(tmp_path):
    dump = ("AustLII\nCases & Legislation\nAny\nX v Y [2026] ARTA 10 (6 January 2026)\n\nLast Updated: 7 January 2026\n\n"
            "[1] Reasons.\n\nPRINT\nPrint (pretty)\nAbout Copyright\n")
    (tmp_path / "2026_ARTA_10.txt").write_text(dump, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    row = ex.work((str(tmp_path / "2026_ARTA_10.txt"), str(out)))
    body = (out / "2026_ARTA_10.txt").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    assert row["court"] == "ARTA" and "[1] Reasons." in body
    assert "Cases & Legislation" not in body and "Print (pretty)" not in body


def test_judgments_adapter_merges_text_into_case_metadata(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "hca_all_cases.json").write_text(json.dumps([
        {"citation": "[2015] HCA 48", "case_name": "A v B", "court": "HCA", "catchwords": "TAX"}]), encoding="utf-8")
    j = tmp_path / "j"
    j.mkdir()
    (j / "2015_HCA_48.txt").write_text("A v B\n\nCitation: [2015] HCA 48\n\n---\n\n[1] Reasons.\n", encoding="utf-8")
    (j / "2026_ARTA_10.txt").write_text("X v Y\n\n---\n\n[1] Tribunal reasons.\n", encoding="utf-8")
    (j / "index.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"stem": "2015_HCA_48", "citation": "[2015] HCA 48", "court": "HCA", "status": "ok", "title": "A v B"},
        {"stem": "2026_ARTA_10", "citation": "[2026] ARTA 10", "court": "ARTA", "status": "ok",
         "title": "X v Y [2026] ARTA 10 (6 January 2026)", "date": "2026-01-06"},
        {"stem": "1900_HCA_1", "citation": "[1900] HCA 1", "court": "HCA", "status": "empty"}]) + "\n", encoding="utf-8")
    monkeypatch.setenv("JUDGMENTS_DIR", str(j))
    sys.modules.pop("adapters.judgments", None)
    import importlib
    mod = importlib.import_module("adapters.judgments")
    parts = {p["title"]: p for p in mod.parts(data)}
    # each court is split into years, newest first
    assert [g["title"] for g in parts["HCA"]["groups"]] == ["2015"]
    hca = {d["id"]: d for g in parts["HCA"]["groups"] for d in g["docs"]}
    assert "Catchwords: TAX" in hca["2015-hca-48"]["text"] and "[1] Reasons." in hca["2015-hca-48"]["text"]
    arta = parts["ARTA"]["groups"][0]["docs"][0]
    assert arta["id"] == "2026-arta-10" and arta["title"] == "X v Y [2026] ARTA 10" and "Tribunal reasons" in arta["text"]
    assert "1900-hca-1" not in hca  # empty extraction is not added


FEDCOURT = """<html><head><title>C v D [2025] FCA 1008</title></head><body><div id="content">
<div class="docx_judgment_content"><table><tr><td><p>Date of judgment:</p></td><td><p>7 August 2025</p></td></tr></table>
<p class="ParaNumbering"><span class="listBullet">1</span>&nbsp;&nbsp;Before the Court is an application.</p>
</div></div><footer>Site footer</footer></body></html>"""


def test_extract_federal_court_site_page(tmp_path):
    (tmp_path / "2025_FCA_1008.html").write_text(FEDCOURT, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    row = ex.work((str(tmp_path / "2025_FCA_1008.html"), str(out)))
    body = (out / "2025_FCA_1008.txt").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    assert row["date"] == "2025-08-07"
    assert "[1] Before the Court is an application." in body and "Site footer" not in body


FOOTNOTED = """<html><head><title>C v D [2026] HCA 18 (10 June 2026)</title></head><body>
<article class="the-document"><ol>
<li value="1">Dividends are income.<a name="fnB1" href="#fn1">[1]</a></li>
<li value="2">So are loans.<a name="fnB2" href="#fn2">[2]</a></li></ol><hr>
<p><b><a name="fn1" href="#fnB1">[1]</a></b> Income Tax Assessment Act 1936 (Cth), s 44(1).</p>
<p><b><a name="fn2" href="#fnB2">[2]</a></b> [2025] FCAFC 15 at [79].</p>
</article></body></html>"""


def test_footnotes_go_under_their_own_heading(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "2026_HCA_18.html").write_text(FOOTNOTED, encoding="utf-8")
    row = ex.work((str(src / "2026_HCA_18.html"), str(out)))
    body = (out / "2026_HCA_18.txt").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    paras, notes = body.split("\n## Footnotes\n")
    assert "[1] Dividends are income.[fn1]" in paras and "[2] So are loans.[fn2]" in paras
    assert "[fn1] Income Tax Assessment Act 1936" in notes and "[fn2] [2025] FCAFC 15 at [79]." in notes
    # no footnote can be read as a paragraph pinpoint
    assert not any(line.startswith("[1] ") or line.startswith("[2] ") for line in notes.splitlines())
    assert row["paragraphs"] == 2 and row["footnotes"] == 2


def test_txt_dump_footnotes_found_where_numbering_restarts():
    lines = ["[1] First paragraph.", "[2] Second paragraph.", "[3] Third.", "", "[1] Act s 1.", "[2] Case at [3]."]
    text = ex.txt_footnotes(lines)
    paras, notes = text.split("\n## Footnotes\n")
    assert "[3] Third." in paras and "[fn1] Act s 1." in notes and "[fn2] Case at [3]." in notes
    assert ex.txt_footnotes(["[1] Only.", "[2] Paragraphs."]) == "[1] Only.\n[2] Paragraphs."
