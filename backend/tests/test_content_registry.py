"""Content-type registry + data_sources manifest (plan §2, §3, §6.1)."""
from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from backend import data_sources
from backend.data_sources import ROOT, manifest, resolve
from content import registry
from content.registry import REGISTRY, content_type_for, tag

DEFAULT = {"legislation", "case", "ruling", "ato-private-ruling", "nz-ruling",
           "treaty", "regulatory-guide", "afsa-guide"}
OPTIONAL = {"commentary", "insolvency", "nz-mtg"}
# Private-export-only types (hidden: true): present in the private repo, absent from the
# public snapshot, so they are not counted in DEFAULT | OPTIONAL.
HIDDEN = {k for k, t in REGISTRY.items() if t.get("hidden")}


def _write_type(d, name, **over):
    key = over.pop("key", name)
    t = {"key": key, "label": key, "default_configured": True, "loader": f"content.{key}.loader",
         "display": "paragraphs", "jurisdiction": "AU", "auth": "user", **over}
    (d / name).mkdir()
    (d / name / "type.yaml").write_text(yaml.safe_dump(t))


def _yaml(path, data):
    path.write_text(yaml.safe_dump(data))
    return path


# --- registry ----------------------------------------------------------------

def test_registry_has_all_eleven_types():
    assert set(REGISTRY) - HIDDEN == DEFAULT | OPTIONAL
    assert {k for k, t in REGISTRY.items() if not t["default_configured"]} - HIDDEN == OPTIONAL
    for k in HIDDEN:  # a hidden type is always optional and admin-only
        assert REGISTRY[k]["default_configured"] is False and REGISTRY[k]["auth"] == "admin"
    for t in REGISTRY.values():
        mod = registry.loader(t["key"])
        assert all(callable(getattr(mod, f)) for f in ("count", "iter_docs", "get"))


def test_registry_rejects_duplicate_key(tmp_path):
    _write_type(tmp_path, "a")
    _write_type(tmp_path, "b", key="a")
    with pytest.raises(ValueError, match="duplicate"):
        registry.load(tmp_path)


def test_registry_rejects_unknown_recommend_target(tmp_path):
    _write_type(tmp_path, "a", recommend=[{"target": "nope", "via": "embedding", "limit": 1}])
    with pytest.raises(ValueError, match="nope"):
        registry.load(tmp_path)


def test_registry_rejects_optional_without_markers(tmp_path):
    _write_type(tmp_path, "p", **{"default_configured": False, "auth": "admin"})
    with pytest.raises(ValueError, match="data_markers"):
        registry.load(tmp_path)


def test_content_type_mapping():
    expect = {"section": "legislation", None: "legislation", "case": "case", "ruling": "ruling",
              "private_ruling": "ato-private-ruling", "regulatory_guide": "regulatory-guide",
              "afsa_guide": "afsa-guide", "treaty": "treaty"}
    assert {k: content_type_for(k) for k in expect} == expect
    assert tag({"source_type": "ruling", "act": "private-rulings"})["content_type"] == "ato-private-ruling"
    assert tag({"source_type": "ruling", "jurisdiction": "NZ"})["content_type"] == "nz-ruling"


# --- manifest §6.1 -------------------------------------------------------------

def test_committed_manifest_is_default_only():
    m = yaml.safe_load((ROOT / "data_sources.yaml").read_text())
    data_sources.check_committed(m)  # the real file passes
    assert set(m["sources"]) == DEFAULT


@pytest.mark.parametrize("bad", [
    {"allow_optional_sources": True, "sources": {}},
    {"allow_optional_sources": False, "sources": {"commentary": {"path": "x"}}},
    {"sources": {}},  # allow_optional_sources must be explicitly false
])
def test_committed_manifest_with_optional_raises(tmp_path, bad):
    committed = _yaml(tmp_path / "data_sources.yaml", {"version": 1, **bad})
    with pytest.raises(RuntimeError):
        manifest(committed, tmp_path / "absent.yaml", env={})


def test_optional_not_configured_without_allow_optional(tmp_path):
    (tmp_path / "cch").mkdir()
    committed = _yaml(tmp_path / "c.yaml", {"allow_optional_sources": False, "sources": {}})
    local = _yaml(tmp_path / "l.yaml", {"sources": {"commentary": {"path": str(tmp_path / "cch")}}})
    assert resolve(manifest(committed, local, env={}))["commentary"].status == "not_configured"
    # env can neither enable optional sources nor smuggle an optional path in
    env = {"LAWKITTY_ALLOW_OPTIONAL_SOURCES": "true", "LAWKITTY_SOURCE_COMMENTARY": str(tmp_path / "cch")}
    assert resolve(manifest(committed, local, env=env))["commentary"].status == "not_configured"
    _yaml(local, {"allow_optional_sources": True, "sources": {"commentary": {"path": str(tmp_path / "cch")}}})
    assert resolve(manifest(committed, local, env={}))["commentary"].status == "ok"


def test_composition_order_and_status(tmp_path):
    for d in ("a", "b", "c", "pr/data/json"):
        (tmp_path / d).mkdir(parents=True)
    committed = _yaml(tmp_path / "c.yaml", {"allow_optional_sources": False, "sources": {
        "case": {"path": str(tmp_path / "a")},
        "treaty": {"path": str(tmp_path / "a")},
        "ruling": {"path": str(tmp_path / "gone")},
    }})
    local = _yaml(tmp_path / "l.yaml", {"instance": "x", "sources": {
        "case": {"path": str(tmp_path / "b")},
        "treaty": {"path": None},  # explicit unset
    }})
    env = {"LAWKITTY_SOURCE_CASE": str(tmp_path / "c"),
           "PRIVATE_RULINGS_ROOT": str(tmp_path / "pr")}  # legacy shim, root -> data/json
    m = manifest(committed, local, env=env)
    s = resolve(m)
    assert m["instance"] == "x"
    assert s["case"].path == tmp_path / "c" and s["case"].status == "ok"
    assert s["treaty"].status == "not_configured"
    assert s["ruling"].status == "missing"
    assert s["ato-private-ruling"].path == tmp_path / "pr/data/json"
    assert s["legislation"].status == "not_configured"


# `--assert-default-only` is a CI guard for a default-only instance. On a dev box
# whose local overlay deliberately enables optional sources (commentary/insolvency/
# nz-mtg), the correct exit is 1 — so skip the zero-exit assertion here.
_LEAKED_OPTIONAL = [k for k, s in resolve().items()
                    if k in OPTIONAL and s.status != "not_configured"]


@pytest.mark.skipif(bool(_LEAKED_OPTIONAL),
                    reason=f"dev instance enables optional sources: {_LEAKED_OPTIONAL}")
def test_cli_assert_default_only_exits_zero():
    r = subprocess.run([sys.executable, "-m", "backend.data_sources", "--assert-default-only"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
