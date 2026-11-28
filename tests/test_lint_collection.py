"""The collection linter: a valid collection passes, and each rule catches its own breakage."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from tests.test_collection_adapters import ingest, lint_mod


@pytest.fixture
def col(tmp_path) -> Path:
    parts = [{"title": "Part A", "docs": [{"title": "First", "text": "Body one"},
                                          {"title": "Second", "text": "Body two"}]},
             {"title": "Part B", "docs": [{"title": "Third", "text": "Body three"}]}]
    ingest.write("demo", parts, "Demo", "Collections", "signed-in", tmp_path)
    return tmp_path / "demo"


def tree_of(col: Path) -> dict:
    return json.loads((col / "tree.json").read_text(encoding="utf-8"))


def save_tree(col: Path, tree: dict) -> None:
    (col / "tree.json").write_text(json.dumps(tree), encoding="utf-8")


def has(msgs: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in msgs)


def test_valid_collection_is_clean(col):
    assert lint_mod.lint(col) == ([], [])


def test_valid_collection_with_aliases_and_licence_is_clean(col):
    (col / "aliases.json").write_text(json.dumps({"s 1": "first"}), encoding="utf-8")
    (col / "licence.json").write_text(json.dumps({
        "jurisdiction": "AU", "source_url": "https://x", "terms_url": "https://y",
        "licence": {"name": "CC BY", "url": "https://z"}}), encoding="utf-8")
    assert lint_mod.lint(col) == ([], [])


def test_bad_collection_id(tmp_path):
    parts = [{"title": "P", "docs": [{"title": "D", "text": "t"}]}]
    ingest.write("Demo", parts, "Demo", "G", "public", tmp_path)  # write() does not slug the id
    errs, _ = lint_mod.lint(tmp_path / "Demo")
    assert has(errs, "collection id")


def test_collection_yaml_missing(col):
    (col / "collection.yaml").unlink()
    errs, _ = lint_mod.lint(col)
    assert len(errs) == 1 and has(errs, "collection.yaml", "missing")


def test_collection_yaml_unparseable(col):
    (col / "collection.yaml").write_text("title: [unclosed\n  - : :\n", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert len(errs) == 1 and has(errs, "unreadable")


def test_collection_yaml_not_a_mapping(col):
    (col / "collection.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert len(errs) == 1 and has(errs, "must be a mapping")


@pytest.mark.parametrize("access", ["secret", "", None, "Public"])
def test_bad_access(col, access):
    meta = yaml.safe_load((col / "collection.yaml").read_text(encoding="utf-8"))
    meta["access"] = access
    (col / "collection.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "access")


def test_missing_title_and_group(col):
    (col / "collection.yaml").write_text("access: public\n", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "title missing") and has(errs, "group missing")


def test_tree_json_missing(col):
    (col / "tree.json").unlink()
    errs, _ = lint_mod.lint(col)
    assert has(errs, "tree.json", "unreadable")


def test_tree_json_malformed(col):
    (col / "tree.json").write_text("{not json", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "tree.json", "unreadable")


@pytest.mark.parametrize("body", ['[]', '{"act": "x"}', '{"parts": "nope"}'])
def test_tree_json_without_parts_list(col, body):
    (col / "tree.json").write_text(body, encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "no parts list")


def test_part_without_title(col):
    t = tree_of(col)
    t["parts"][0]["title"] = ""
    save_tree(col, t)
    errs, _ = lint_mod.lint(col)
    assert has(errs, "part without id/title")


def test_section_without_title(col):
    t = tree_of(col)
    t["parts"][0]["sections"][0]["title"] = ""
    save_tree(col, t)
    errs, _ = lint_mod.lint(col)
    assert has(errs, "section without id/title")


def test_duplicate_section_id(col):
    t = tree_of(col)
    t["parts"][1]["sections"][0]["id"] = "first"
    t["parts"][1]["sections"][0]["path"] = "first.md"
    save_tree(col, t)
    errs, _ = lint_mod.lint(col)
    assert has(errs, "duplicate section id", "first")


def test_section_path_escaping_sections_dir(col):
    (col / "secret.md").write_text('---\nact: "demo"\nsection: "first"\n---\nleak\n', encoding="utf-8")
    t = tree_of(col)
    t["parts"][0]["sections"][0]["path"] = "../secret.md"
    save_tree(col, t)
    errs, _ = lint_mod.lint(col)
    assert has(errs, "escapes sections/")


def test_missing_section_file(col):
    (col / "sections" / "second.md").unlink()
    errs, _ = lint_mod.lint(col)
    assert has(errs, "second.md", "missing")


def test_front_matter_missing(col):
    (col / "sections" / "first.md").write_text("no front matter here\n", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "first.md", "no front matter")


def test_front_matter_act_mismatch(col):
    f = col / "sections" / "first.md"
    f.write_text(f.read_text(encoding="utf-8").replace('act: "demo"', 'act: "other"'), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "front matter act", "other")


def test_front_matter_section_mismatch(col):
    f = col / "sections" / "first.md"
    f.write_text(f.read_text(encoding="utf-8").replace('section: "first"', 'section: "wrong"'), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "front matter section", "wrong")


def test_crlf_front_matter_is_accepted(col):
    f = col / "sections" / "first.md"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert lint_mod.lint(col)[0] == []


def test_orphan_markdown_not_in_tree(col):
    (col / "sections" / "orphan.md").write_text('---\nact: "demo"\nsection: "orphan"\n---\nx\n', encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "orphan.md", "not in tree.json")


def test_orphan_in_subfolder_of_sections(col):
    (col / "sections" / "sub").mkdir()
    (col / "sections" / "sub" / "deep.md").write_text("x\n", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "deep.md", "not in tree.json")


def test_aliases_pointing_at_unknown_id(col):
    (col / "aliases.json").write_text(json.dumps({"s 1": "first", "s 9": "nowhere"}), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "aliases.json", "unknown ids", "s 9")


@pytest.mark.parametrize("body", ['["first"]', "{oops", '"first"'])
def test_aliases_not_a_map(col, body):
    (col / "aliases.json").write_text(body, encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "aliases.json", "map")


@pytest.mark.parametrize("drop,expected", [
    ("jurisdiction", "jurisdiction"), ("source_url", "source_url"), ("terms_url", "terms_url"),
    ("licence", "licence.name")])
def test_licence_missing_a_field(col, drop, expected):
    d = {"jurisdiction": "AU", "source_url": "https://x", "terms_url": "https://y",
         "licence": {"name": "CC BY", "url": "https://z"}}
    d.pop(drop)
    (col / "licence.json").write_text(json.dumps(d), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "licence.json", "missing", expected)


def test_licence_missing_inner_url(col):
    (col / "licence.json").write_text(json.dumps({
        "jurisdiction": "AU", "source_url": "u", "terms_url": "t", "licence": {"name": "n"}}), encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "licence.json", "licence.url")


def test_licence_unreadable(col):
    (col / "licence.json").write_text("{oops", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "licence.json", "unreadable")


def test_licence_not_an_object(col):
    (col / "licence.json").write_text("[1, 2]", encoding="utf-8")
    errs, _ = lint_mod.lint(col)
    assert has(errs, "licence.json")


def test_empty_body_is_a_warning_only(col):
    f = col / "sections" / "first.md"
    f.write_text('---\nact: "demo"\nsection: "first"\ntitle: "First"\n---\n   \n', encoding="utf-8")
    errs, warns = lint_mod.lint(col)
    assert errs == [] and has(warns, "first.md", "empty body")


def test_no_sections_is_a_warning_only(col):
    shutil.rmtree(col / "sections")
    save_tree(col, {"act": "Demo", "parts": []})
    errs, warns = lint_mod.lint(col)
    assert errs == [] and has(warns, "no sections")


def test_sections_in_divisions_are_linted(col):
    t = tree_of(col)
    sec = t["parts"][1]["sections"].pop()
    t["parts"][1]["divisions"] = [{"id": "d1", "title": "Div", "sections": [sec]}]
    save_tree(col, t)
    assert lint_mod.lint(col) == ([], [])
    (col / "sections" / "third.md").unlink()
    assert has(lint_mod.lint(col)[0], "third.md", "missing")


def test_main_exit_codes(col, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["x", str(col)])
    assert lint_mod.main() == 0
    (col / "tree.json").unlink()
    assert lint_mod.main() == 1
    assert "FAIL" in capsys.readouterr().out
