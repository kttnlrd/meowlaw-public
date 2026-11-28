"""G4 gate — entity resolution backstop (spec §7).

Integrity checks:
  1. every mapped key in data/entity_alias_map.json resolves to a graph node
  2. ambiguous strings are flagged (status == ambiguous), never mapped
  3. mapped keys are well-formed (section:<slug>:<num> | case:[year] COURT no)
  4. the 100-entry manual review list is written
  5. the map is stable: re-running `local` from the map does not change it
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

import pytest

from dotenv import load_dotenv

load_dotenv()

from backend.services.graph_neighborhood import GRAPH_DB  # noqa: E402
from pipeline.entity_backstop import ALIAS_MAP, BASE, REVIEW  # noqa: E402

KEY_RE = re.compile(r"^(section:[a-z0-9-]+:[0-9A-Za-z-]+|case:(\[\d{4}\] [A-Z]+ \d+|\(\d{4}\) \d+ [A-Z]+ \d+))$")


def _load_map() -> dict:
    if not ALIAS_MAP.exists():
        pytest.skip("entity_alias_map.json not built yet — run entity_backstop")
    return json.loads(ALIAS_MAP.read_text())


def test_every_mapped_key_resolves_to_graph_node():
    mapping = _load_map()
    keys = {v["key"] for v in mapping.values() if v["status"] == "mapped"}
    assert keys, "no mapped keys"
    conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True)
    try:
        ph = ",".join("?" * len(keys))
        found = {r[0] for r in conn.execute(
            f"SELECT key FROM nodes WHERE key IN ({ph})", list(keys))}
    finally:
        conn.close()
    missing = keys - found
    assert not missing, f"{len(missing)} mapped keys not in graph: {sorted(missing)[:10]}"


def test_ambiguous_never_mapped():
    mapping = _load_map()
    for ref, v in mapping.items():
        if v["status"] == "ambiguous":
            assert "key" not in v, f"ambiguous ref got a key: {ref!r} -> {v}"
        if v["status"] == "mapped":
            assert KEY_RE.match(v["key"]), f"malformed key: {v['key']!r} for {ref!r}"


def test_review_list_written():
    mapping = _load_map()
    n_mapped = sum(1 for v in mapping.values() if v["status"] == "mapped")
    assert n_mapped >= 100
    review = json.loads(REVIEW.read_text())
    assert len(review) == 100
    for entry in review:
        assert entry["key"] in {v["key"] for v in mapping.values() if v["status"] == "mapped"}


def test_map_is_idempotent_under_relocal():
    """Re-running the deterministic local stage from the map changes nothing."""
    import subprocess
    import sys
    before = _load_map()
    r = subprocess.run(
        [sys.executable, "-m", "pipeline.entity_backstop", "local"],
        capture_output=True, text=True, cwd=str(BASE),
    )
    assert r.returncode == 0, r.stderr[-500:]
    after = json.loads(ALIAS_MAP.read_text())
    assert before == after, "local stage is not idempotent"
