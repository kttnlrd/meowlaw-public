"""Adapters write the standard collection shape: collection.yaml + tree.json + sections/*.md."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ingest = load("scripts/ingest_collection.py", "ingest_collection")


def check_collection(out: Path, cid: str) -> dict:
    """The shape /api/tree and /api/section read: unique ids, every path present, front matter."""
    meta = yaml.safe_load((out / "collection.yaml").read_text(encoding="utf-8"))
    assert set(meta) >= {"title", "group", "access"}
    tree = json.loads((out / "tree.json").read_text(encoding="utf-8"))
    ids = [s["id"] for p in tree["parts"] for s in p["sections"]]
    assert ids and len(ids) == len(set(ids))
    for p in tree["parts"]:
        assert p["id"] and p["title"] and p["divisions"] == []
        for s in p["sections"]:
            text = (out / "sections" / s["path"]).read_text(encoding="utf-8")
            assert text.startswith("---\n") and f'act: "{cid}"' in text and f'section: "{s["id"]}"' in text
    assert sorted(f.name for f in (out / "sections").iterdir()) == sorted(
        s["path"] for p in tree["parts"] for s in p["sections"])
    return {"meta": meta, "tree": tree}


def test_keays_adapter(tmp_path):
    src = tmp_path / "keays"
    (src / "chapters").mkdir(parents=True)
    (src / "chapters" / "01-intro.md").write_text(
        '---\nact: "x"\nchapter: 1\n---\n\n# Chapter 1: Intro\n\n[1.05] Insolvency means...\n', encoding="utf-8")
    (src / "chapters" / "02-more.md").write_text("[2.05] Bankruptcy is...\n", encoding="utf-8")
    (src / "ch-tree.json").write_text(json.dumps({"chapters": [
        {"chapter": 2, "title": "More", "file": "chapters/02-more.md"},
        {"chapter": 1, "title": "Intro", "file": "chapters/01-intro.md"}]}), encoding="utf-8")
    mod = load("scripts/adapters/keays.py", "keays")
    ingest.write("insolvency-keays", mod.parts(src), mod.TITLE, mod.GROUP, "restricted", tmp_path / "data")
    c = check_collection(tmp_path / "data" / "insolvency-keays", "insolvency-keays")
    assert c["meta"] == {"title": "Keays Insolvency", "group": "Commentary", "access": "restricted"}
    secs = c["tree"]["parts"][0]["sections"]
    assert [s["id"] for s in secs] == ["ch-1", "ch-2"] and secs[0]["title"] == "Chapter 1: Intro"
    body = (tmp_path / "data" / "insolvency-keays" / "sections" / "ch-1.md").read_text(encoding="utf-8")
    assert "[1.05] Insolvency means" in body and "# Chapter 1" not in body and "chapter: 1" not in body


def test_keays_adapter_refuses_paths_outside_chapters(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    (tmp_path / "ch-tree.json").write_text(json.dumps({"chapters": [
        {"chapter": 1, "title": "x", "file": "chapters/../secret.md"}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load("scripts/adapters/keays.py", "keays").parts(tmp_path)


def test_meta_only_writes_collection_yaml_for_an_existing_tree(tmp_path, monkeypatch):
    out = tmp_path / "master-tax-guide"
    out.mkdir()
    (out / "tree.json").write_text('{"act": "Guide", "parts": []}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", "--collection", "master-tax-guide", "--group", "Commentary",
                                     "--access", "restricted", "--data-dir", str(tmp_path)])
    assert ingest.main() == 0
    meta = yaml.safe_load((out / "collection.yaml").read_text(encoding="utf-8"))
    assert meta == {"title": "master-tax-guide", "group": "Commentary", "access": "restricted"}
    monkeypatch.setattr("sys.argv", ["x", "--collection", "missing", "--data-dir", str(tmp_path)])
    assert ingest.main() == 1 and not (tmp_path / "missing").exists()


# ---- fixture tests for the remaining adapters, the generic ingest paths and the linter ----

lint_mod = load("scripts/lint_collection.py", "lint_collection")


def run_adapter(name: str, src: Path, tmp_path: Path, cid: str, access: str = "signed-in") -> dict:
    mod = load(f"scripts/adapters/{name}.py", f"adapter_{name}")
    parts = mod.parts(src)
    ingest.write(cid, parts, getattr(mod, "TITLE", None), getattr(mod, "GROUP", None), access,
                 tmp_path / "data")
    out = tmp_path / "data" / cid
    c = check_collection(out, cid)
    errs, _warns = lint_mod.lint(out)
    assert errs == []
    c["out"] = out
    c["mod"] = mod
    return c


def front_matter(out: Path, sid: str) -> str:
    return (out / "sections" / f"{sid}.md").read_text(encoding="utf-8").split("---\n")[1]


def titles(c: dict) -> list[str]:
    return [p["title"] for p in c["tree"]["parts"]]


def sec_ids(c: dict) -> list[str]:
    return [s["id"] for p in c["tree"]["parts"] for s in p["sections"]]


def sec_titles(c: dict) -> dict[str, str]:
    return {s["id"]: s["title"] for p in c["tree"]["parts"] for s in p["sections"]}


def make_treaty(root: Path, country: str, treaty: str | None, articles: list[dict], files: dict) -> None:
    d = root / country
    (d / "articles").mkdir(parents=True)
    for rel, text in files.items():
        (d / rel).write_text(text, encoding="utf-8")
    tree = {"articles": articles}
    if treaty:
        tree["treaty"] = treaty
    (d / "tree.json").write_text(json.dumps(tree), encoding="utf-8")


def test_treaties_adapter(tmp_path):
    src = tmp_path / "treaties"
    make_treaty(src, "japan", "Australia-Japan DTA", [
        {"article": 1, "slug": "art-1", "title": "Persons covered", "file": "articles/1.md"},
        {"article": 2, "title": "Taxes covered", "file": "articles/2.md"},
        {"article": 3, "file": "articles/3.md"}],
        {"articles/1.md": "This Convention applies to residents.", "articles/2.md": "Taxes covered text.",
         "articles/3.md": "Article three text."})
    make_treaty(src, "new-zealand", None, [{"article": 1, "slug": "a1", "title": "Scope", "file": "articles/1.md"}],
                {"articles/1.md": "NZ scope text."})
    c = run_adapter("treaties", src, tmp_path, "treaties")
    assert c["meta"] == {"title": "Tax treaties", "group": "Treaties", "access": "signed-in"}
    assert titles(c) == ["Australia-Japan DTA", "New-Zealand"]
    assert sec_ids(c) == ["japan-art-1", "japan-2", "japan-3", "new-zealand-a1"]
    assert [s["title"] for s in c["tree"]["parts"][0]["sections"]] == [
        "Persons covered", "Taxes covered", "Article 3"]
    assert "residents" in (c["out"] / "sections" / "japan-art-1.md").read_text(encoding="utf-8")


def test_treaties_adapter_skips_files_outside_the_country_folder(tmp_path):
    src = tmp_path / "treaties"
    make_treaty(src, "japan", "JP", [
        {"article": 1, "slug": "ok", "title": "Fine", "file": "articles/1.md"},
        {"article": 2, "slug": "evil", "title": "Evil", "file": "../../secret.md"},
        {"article": 3, "slug": "missing", "title": "Missing", "file": "articles/none.md"}],
        {"articles/1.md": "fine text"})
    (tmp_path / "secret.md").write_text("TOP SECRET", encoding="utf-8")
    c = run_adapter("treaties", src, tmp_path, "treaties")
    assert sec_ids(c) == ["japan-ok"]
    assert "TOP SECRET" not in json.dumps(c["mod"].parts(src))


def test_treaties_adapter_bad_json_raises(tmp_path):
    (tmp_path / "t" / "x").mkdir(parents=True)
    (tmp_path / "t" / "x" / "tree.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load("scripts/adapters/treaties.py", "treaties").parts(tmp_path / "t")


def test_guides_adapter_asic(tmp_path):
    src = tmp_path / "rg"
    (src / "texts").mkdir(parents=True)
    (src / "rg_manifest.json").write_text(json.dumps([
        {"rg_number": 1, "title": "Licensing", "status": "current", "url": "https://asic.example/rg1"},
        {"rg_number": 105, "title": "Disclosure"},
        {"rg_number": 999, "title": "No text file"}]), encoding="utf-8")
    (src / "texts" / "RG_001.txt").write_text("RG 1 body text", encoding="utf-8")
    (src / "texts" / "RG_105.txt").write_text("RG 105 body text", encoding="utf-8")
    c = run_adapter("guides", src, tmp_path, "regulatory-guides")
    assert c["meta"]["group"] == "Regulator guidance" and c["meta"]["title"] == "regulatory-guides"
    assert titles(c) == ["Guides"] and sec_ids(c) == ["rg-1", "rg-105"]
    assert sec_titles(c) == {"rg-1": "RG 1 Licensing", "rg-105": "RG 105 Disclosure"}
    fm = front_matter(c["out"], "rg-1")
    assert 'citation: "RG 1"' in fm and 'status: "current"' in fm
    body = (c["out"] / "sections" / "rg-1.md").read_text(encoding="utf-8")
    assert "Source: https://asic.example/rg1" in body and "RG 1 body text" in body
    assert "status" not in front_matter(c["out"], "rg-105")


def test_guides_adapter_afsa(tmp_path):
    src = tmp_path / "afsa"
    (src / "texts").mkdir(parents=True)
    (src / "afsa_manifest.json").write_text(json.dumps([
        {"slug": "debtor-guide", "title": "Debtor guide", "page_url": "https://afsa.example/d", "status": "current"},
        {"slug": "no-title"}]), encoding="utf-8")
    (src / "texts" / "debtor-guide.txt").write_text("Debtor text", encoding="utf-8")
    (src / "texts" / "no-title.txt").write_text("Untitled text", encoding="utf-8")
    c = run_adapter("guides", src, tmp_path, "afsa-guides")
    assert sec_ids(c) == ["debtor-guide", "no-title"]
    assert sec_titles(c) == {"debtor-guide": "Debtor guide", "no-title": "no-title"}
    fm = front_matter(c["out"], "debtor-guide")
    assert "citation" not in fm and 'status: "current"' in fm
    assert "Source: https://afsa.example/d" in (c["out"] / "sections" / "debtor-guide.md").read_text(encoding="utf-8")


def test_guides_adapter_bad_manifest_raises(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "rg_manifest.json").write_text("[oops", encoding="utf-8")
    with pytest.raises(ValueError):
        load("scripts/adapters/guides.py", "guides").parts(tmp_path / "g")


def test_guides_adapter_without_manifest_has_no_docs(tmp_path):
    (tmp_path / "g").mkdir()
    assert load("scripts/adapters/guides.py", "guides").parts(tmp_path / "g") == [{"title": "Guides", "docs": []}]


def test_rulings_adapter_ato_names(tmp_path):
    src = tmp_path / "rulings"
    src.mkdir()
    (src / "TR_2024_1.txt").write_text("Income tax: a ruling about things\n\nBody one.", encoding="utf-8")
    (src / "TR_2023_3.txt").write_text("TR 2023/3 Already has the citation\nBody.", encoding="utf-8")
    (src / "PS_LA_2011_12.txt").write_text("Practice statement title\nBody.", encoding="utf-8")
    (src / "TD_2020_5.txt").write_text("\n" + "x" * 250 + "\nBody.", encoding="utf-8")
    (src / "notes.txt").write_text("ignored", encoding="utf-8")
    (src / "README.md").write_text("ignored", encoding="utf-8")
    c = run_adapter("rulings", src, tmp_path, "public-rulings")
    assert c["meta"]["group"] == "Rulings" and c["meta"]["title"] == "public-rulings"
    # series ordered by number of documents (largest first), then by name
    assert titles(c) == ["TR", "PS LA", "TD"]
    assert sec_ids(c) == ["tr-2023-3", "tr-2024-1", "ps-la-2011-12", "td-2020-5"]
    t = sec_titles(c)
    assert t["tr-2024-1"] == "TR 2024/1 Income tax: a ruling about things"
    assert t["tr-2023-3"] == "TR 2023/3 Already has the citation"
    assert t["ps-la-2011-12"] == "PS LA 2011/12 Practice statement title"
    assert t["td-2020-5"] == "TD 2020/5"  # over-long first line falls back to the citation
    fm = front_matter(c["out"], "tr-2024-1")
    assert 'citation: "TR 2024/1"' in fm and 'date: "2024"' in fm
    assert 'citation: "PS LA 2011/12"' in front_matter(c["out"], "ps-la-2011-12")


def test_rulings_adapter_nz_names(tmp_path):
    src = tmp_path / "nz"
    src.mkdir()
    (src / "BRPUB_2024_1.txt").write_text("Income tax - Something\nBody.", encoding="utf-8")
    (src / "QB_2023_12.txt").write_text("Question asked\nBody.", encoding="utf-8")
    (src / "BRPUB_24_01.txt").write_text("Two digit year\nBody.", encoding="utf-8")  # regex wants 4 digits
    c = run_adapter("rulings", src, tmp_path, "nz-rulings")
    assert sorted(sec_ids(c)) == ["brpub-2024-1", "qb-2023-12"]  # BRPUB_24_01 is not recognised
    assert 'citation: "BRPUB 2024/1"' in front_matter(c["out"], "brpub-2024-1")
    assert sec_titles(c)["brpub-2024-1"] == "BRPUB 2024/1 Income tax - Something"


def test_rulings_adapter_empty_folder_gives_no_parts(tmp_path):
    (tmp_path / "r").mkdir()
    assert load("scripts/adapters/rulings.py", "rulings").parts(tmp_path / "r") == []


def make_pr(folder: Path, fname: str, **kw) -> None:
    (folder / f"{fname}.json").write_text(json.dumps(kw), encoding="utf-8")


def test_private_rulings_adapter(tmp_path):
    src = tmp_path / "pr"
    src.mkdir()
    make_pr(src, "1051111", authorisation_number="1051111", name="Trust distribution", date_of_advice="2023-05-04",
            qa_pairs=[{"question": "Is it income?", "answer": "Yes."}],
            facts="The trust earned money.", applies_for_periods="Year ended 30 June 2023",
            relevant_legislative_provisions=["s 6-5 ITAA 1997", "s 97 ITAA 1936"],
            reasons_for_decision="Because.")
    make_pr(src, "2000000", subject="Subject only", date_of_advice="2021-01-01", facts="F")
    make_pr(src, "3000000", name="No date", relevant_legislation=["s 8-1 ITAA 1997"])  # filed under "Undated"
    make_pr(src, "4000000", authorisation_number="4000000", date_of_advice="14 July 2026",
            subject="Deductibility", relevant_legislation=["ITAA 1997 s 8-1"])  # long-form date
    make_pr(src, "5000000", authorisation_number="5000000", date_of_advice="2012-08-20",
            mop_status="empty")  # nothing published: skipped and reported
    c = run_adapter("private_rulings", src, tmp_path, "private-rulings")
    assert c["meta"] == {"title": "ATO private rulings", "group": "Rulings", "access": "signed-in"}
    assert titles(c) == ["Undated", "2026", "2023", "2021"]  # reverse-sorted by label
    assert sorted(sec_ids(c)) == ["pr-1051111", "pr-2000000", "pr-3000000", "pr-4000000"]
    mod = load("scripts/adapters/private_rulings.py", "private_rulings_chk")
    mod.parts(src)
    assert mod.ISSUES == ["5000000: no content in the source record (status empty), skipped"]
    b4 = (c["out"] / "sections" / "pr-4000000.md").read_text(encoding="utf-8")
    assert "**Subject:** Deductibility" in b4 and "## Legislation" in b4 and "- ITAA 1997 s 8-1" in b4
    t = sec_titles(c)
    assert t["pr-1051111"] == "1051111 Trust distribution"
    assert t["pr-2000000"] == "2000000 Subject only"
    fm = front_matter(c["out"], "pr-1051111")
    assert 'citation: "Authorisation 1051111"' in fm and 'date: "2023-05-04"' in fm
    assert "\ndate:" not in front_matter(c["out"], "pr-3000000")
    body = (c["out"] / "sections" / "pr-1051111.md").read_text(encoding="utf-8")
    for v in ("**Question:** Is it income?", "**Answer:** Yes.", "## Facts", "- s 6-5 ITAA 1997",
              "## Period", "## Reasons"):
        assert v in body, v


def test_private_rulings_adapter_skips_bad_json(tmp_path):
    src = tmp_path / "pr"
    src.mkdir()
    (src / "bad.json").write_text("{not json", encoding="utf-8")
    make_pr(src, "good", authorisation_number="42", name="Good", date_of_advice="2020-02-02", facts="F")
    c = run_adapter("private_rulings", src, tmp_path, "private-rulings")
    assert sec_ids(c) == ["pr-42"]


def write_cases(src: Path, name: str, rows) -> None:
    (src / name).write_text(json.dumps(rows), encoding="utf-8")


def test_cases_adapter(tmp_path):
    src = tmp_path / "cases"
    src.mkdir()
    write_cases(src, "hca_all_cases.json", [
        {"citation": "[2019] HCA 1", "case_name": "A v B", "date": "2019-02-01", "court": "HCA",
         "catchwords": "TAXES - income", "parallel_citations": ["(2019) 1 CLR 1", "99 ATR 1"],
         "austlii_url": "https://austlii.example/1"},
        {"citation": "[2019] HCA 1", "case_name": "Duplicate"},
        {"case_name": "No citation"}])
    write_cases(src, "fca_all_cases.json", {"cases": [
        {"citation": "[2020] FCA 7", "case_name": "C v D", "subject": "Residency"}]})
    write_cases(src, "aata_tax_cases.json", [{"citation": "[2015] AATA 9", "court_url": "https://c.example/9"}])
    c = run_adapter("cases", src, tmp_path, "case-law")
    assert c["meta"] == {"title": "Case law", "group": "Case law", "access": "signed-in"}
    assert titles(c) == ["HCA", "FCA", "AATA"]
    assert sec_ids(c) == ["2019-hca-1", "2020-fca-7", "2015-aata-9"]
    t = sec_titles(c)
    assert t["2019-hca-1"] == "A v B [2019] HCA 1"
    fm = front_matter(c["out"], "2019-hca-1")
    assert 'citation: "[2019] HCA 1"' in fm and 'date: "2019-02-01"' in fm
    body = (c["out"] / "sections" / "2019-hca-1.md").read_text(encoding="utf-8")
    assert "Also reported: (2019) 1 CLR 1; 99 ATR 1" in body and "Source: https://austlii.example/1" in body
    assert "Duplicate" not in json.dumps(c["tree"]) and "No citation" not in json.dumps(c["tree"])


def test_cases_adapter_court_defaults_to_file_name(tmp_path):
    src = tmp_path / "cases"
    src.mkdir()
    write_cases(src, "fcafc_all_cases.json", [{"citation": "[2021] FCAFC 3", "case_name": "X v Y"}])
    c = run_adapter("cases", src, tmp_path, "case-law")
    assert titles(c) == ["FCAFC"]


def test_cases_adapter_bad_json_raises(tmp_path):
    (tmp_path / "hca_all_cases.json").write_text("[oops", encoding="utf-8")
    with pytest.raises(ValueError):
        load("scripts/adapters/cases.py", "cases").parts(tmp_path)


# ---- generic ingest through main() ----

def run_main(monkeypatch, tmp_path, *args) -> int:
    monkeypatch.setattr("sys.argv", ["x", *args, "--data-dir", str(tmp_path / "data")])
    return ingest.main()


def test_main_markdown_file(tmp_path, monkeypatch, capsys):
    src = tmp_path / "guide.md"
    src.write_text("# Part One\n\n## Doc A\n\nText A\n\n## Doc B\n\nText B\n\n# Part Two\n\n## Doc C\n\nText C\n",
                   encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "md-guide", "--title", "MD",
                    "--group", "Notes", "--access", "public") == 0
    out = tmp_path / "data" / "md-guide"
    c = check_collection(out, "md-guide")
    assert c["meta"] == {"title": "MD", "group": "Notes", "access": "public"}
    assert titles(c) == ["Part One", "Part Two"] and sec_ids(c) == ["doc-a", "doc-b", "doc-c"]
    assert "lint: 0 errors" in capsys.readouterr().out
    assert lint_mod.lint(out)[0] == []


def test_main_folder_of_markdown(tmp_path, monkeypatch):
    src = tmp_path / "notes"
    (src / "sub-area").mkdir(parents=True)
    (src / "top-note.md").write_text("# Top Heading\n\nTop body\n", encoding="utf-8")
    (src / "plain-name.md").write_text("No heading body\n", encoding="utf-8")
    (src / "sub-area" / "deep.md").write_text("# Deep Heading\n\nDeep body\n", encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "folder-c") == 0
    out = tmp_path / "data" / "folder-c"
    c = check_collection(out, "folder-c")
    assert c["meta"]["access"] == "restricted"
    assert titles(c) == ["Documents", "sub area"]
    assert sec_titles(c) == {"plain-name": "plain name", "top-heading": "Top Heading",
                             "deep-heading": "Deep Heading"}
    assert lint_mod.lint(out)[0] == []


def test_main_json_array(tmp_path, monkeypatch):
    src = tmp_path / "items.json"
    src.write_text(json.dumps([{"title": "One", "text": "First"}, {"title": "Two", "text": "Second", "part": "P"},
                               "not an object", {"text": "no title"}]), encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "json-c") == 0
    out = tmp_path / "data" / "json-c"
    c = check_collection(out, "json-c")
    assert titles(c) == ["Documents", "P"] and sec_ids(c) == ["one", "untitled", "two"]
    assert lint_mod.lint(out)[0] == []


def test_main_json_not_an_array_exits(tmp_path, monkeypatch):
    src = tmp_path / "bad.json"
    src.write_text('{"title": "x"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, str(src), "--collection", "bad")


def test_main_empty_source_exits_1(tmp_path, monkeypatch):
    src = tmp_path / "empty.md"
    src.write_text("just prose, no headings\n", encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "empty") == 1
    assert not (tmp_path / "data" / "empty").exists()


def test_main_rerun_keeps_access_unless_given(tmp_path, monkeypatch):
    src = tmp_path / "guide.md"
    src.write_text("## Doc A\n\nText A\n", encoding="utf-8")
    meta_f = tmp_path / "data" / "keep" / "collection.yaml"
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "keep", "--access", "public") == 0
    assert yaml.safe_load(meta_f.read_text(encoding="utf-8"))["access"] == "public"
    src.write_text("## Doc A\n\nChanged\n\n## Doc B\n\nNew\n", encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "keep") == 0
    assert yaml.safe_load(meta_f.read_text(encoding="utf-8"))["access"] == "public"
    assert sec_ids(check_collection(meta_f.parent, "keep")) == ["doc-a", "doc-b"]
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "keep", "--access", "signed-in") == 0
    assert yaml.safe_load(meta_f.read_text(encoding="utf-8"))["access"] == "signed-in"


def test_main_adapter_flag_and_bad_adapter_names(tmp_path, monkeypatch):
    src = tmp_path / "pr"
    src.mkdir()
    make_pr(src, "7", authorisation_number="7", name="N", date_of_advice="2022-01-01", facts="F")
    assert run_main(monkeypatch, tmp_path, str(src), "--collection", "pr", "--adapter", "private_rulings") == 0
    assert yaml.safe_load((tmp_path / "data" / "pr" / "collection.yaml").read_text(encoding="utf-8"))["title"] \
        == "ATO private rulings"
    for bad in ("../x", "Nope", "does_not_exist"):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, tmp_path, str(src), "--collection", "pr2", "--adapter", bad)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_folder_ingest_skips_symlinks(tmp_path):
    src = tmp_path / "notes"
    src.mkdir()
    (src / "ok.md").write_text("# Ok\n\nfine\n", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("# LEAK\n\nsecret\n", encoding="utf-8")
    (src / "link.md").symlink_to(secret)
    assert "LEAK" not in json.dumps(ingest.from_folder(src))


def test_cases_adapter_title_without_case_name_does_not_repeat_the_citation(tmp_path):
    src = tmp_path / "cases"
    src.mkdir()
    write_cases(src, "aata_tax_cases.json", [{"citation": "[2015] AATA 9"}])
    c = run_adapter("cases", src, tmp_path, "case-law")
    assert sec_titles(c)["2015-aata-9"] == "[2015] AATA 9"


@pytest.fixture(autouse=True)
def _ingest_log(tmp_path, monkeypatch):
    """Ingest runs append to a run log; keep the tests' runs out of the real one."""
    monkeypatch.setenv("INGEST_LOG", str(tmp_path / "ingest-checks.jsonl"))


def test_ingest_logs_the_run(tmp_path, monkeypatch):
    src = tmp_path / "a.md"
    src.write_text("# P\n## Doc\nText.\n## Empty\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["x", str(src), "--collection", "logged", "--data-dir", str(tmp_path / "d")])
    assert ingest.main() == 0
    rec = json.loads((tmp_path / "ingest-checks.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["collection"] == "logged" and rec["status"] == "ok" and rec["docs"] == 2
    assert rec["warnings"] == 1 and rec["kinds"] == {"warn: <file>: empty body": 1}


def test_built_tree_adapter(tmp_path):
    """CCH / NZ MTG trees: ids kept, clean front matter, example numbers, overwritten duplicates dropped."""
    src = tmp_path / "master-tax-guide"
    (src / "sections").mkdir(parents=True)
    def sec(name, part, title, para, body):
        (src / "sections" / f"{name}.md").write_text(
            f'---\nact: "Australian Master Tax Guide"\npart: "{part}"\nsection: "{name}"\n'
            f'title: "{title}"\nparagraph: "{para}"\n---\n# {title} {para}\n\n{body}\n', encoding="utf-8")
    sec("dividends", "10", "Dividends", "¶10-455", "Ch 10 text.")  # Ch 03's copy was overwritten
    sec("reduced-credit", "10", '75% "reduced" credit', "¶10-040", "Quoted title.")
    sec("1-000-royalties", "1", "1-000 - Royalties", "None", "Example.")
    (src / "tree.json").write_text(json.dumps({"act": "Australian Master Tax Guide", "parts": [
        {"id": "ch-03", "title": "Ch 03 — Companies", "sections": [
            {"id": "dividends", "title": "dividends", "path": "dividends.md"}]},
        {"id": "ch-10", "title": "Ch 10 — Assessable Income", "sections": [
            {"id": "dividends", "title": "Dividends", "path": "dividends.md"},
            {"id": "reduced-credit", "title": '75% "reduced" credit', "path": "reduced-credit.md"},
            {"id": "1-000-royalties", "title": "1-000 - Royalties", "path": "1-000-royalties.md"}]}]}),
        encoding="utf-8")
    mod = load("scripts/adapters/built_tree.py", "built_tree")
    parts = mod.parts(src)
    assert [p["title"] for p in parts] == ["Ch 10 — Assessable Income"]  # Ch 03 lost its only entry
    assert len(mod.ISSUES) == 1 and "dividends in Ch 03" in mod.ISSUES[0] and "¶10-455" in mod.ISSUES[0]
    ingest.write("master-tax-guide", parts, "Australian Master Tax Guide", "Commentary", "restricted",
                 tmp_path / "data")
    out = tmp_path / "data" / "master-tax-guide"
    errs, warns = lint_mod.lint(out)
    assert errs == [] and warns == []
    assert 'citation: "¶10-455"' in front_matter(out, "dividends")
    assert 'title: "75% \\"reduced\\" credit"' in front_matter(out, "reduced-credit")
    assert 'citation: "¶1-000"' in front_matter(out, "1-000-royalties")
    body = (out / "sections" / "dividends.md").read_text(encoding="utf-8")
    assert "Ch 10 text." in body and "# Dividends" not in body


def test_ingest_groups_become_divisions(tmp_path):
    parts = [{"title": "HCA", "groups": [
        {"title": "2015", "docs": [{"id": "2015-hca-48", "title": "A v B", "text": "x"}]},
        {"title": "2014", "docs": [{"id": "2014-hca-1", "title": "C v D", "text": "y"}]}]}]
    ingest.write("cases-x", parts, "Cases", "Case law", "signed-in", tmp_path / "data")
    out = tmp_path / "data" / "cases-x"
    tree = json.loads((out / "tree.json").read_text(encoding="utf-8"))
    p = tree["parts"][0]
    assert p["sections"] == [] and [(d["id"], d["title"]) for d in p["divisions"]] == [("1.1", "2015"), ("1.2", "2014")]
    assert [s["id"] for s in p["divisions"][0]["sections"]] == ["2015-hca-48"]
    text = (out / "sections" / "2014-hca-1.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and 'division: "1.2"' in text
