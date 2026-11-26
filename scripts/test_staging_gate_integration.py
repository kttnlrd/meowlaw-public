#!/usr/bin/env python3
"""CDN-0193 Phase 0 — integration proof of the offline-staging convention.

Exercises the real apply driver (scripts/apply_itaa_table_fixes.py) with its
corpus root pointed at a throwaway tree and its PDF extraction replaced by a
fixed table, so the codex-R4 guard rails are proven end-to-end with no
possibility of touching ~/legislation-explorer/data.

Cases
  1. unstaged section               -> REFUSED, corpus file byte-identical
  2. staged but gate REJECTED       -> REFUSED
  3. staged output != this run's    -> REFUSED (a review cannot be bypassed)
  4. emitted candidate staged+gate  -> applied, prose preserved exactly
  5. --allow-unstaged escape hatch  -> applies (documented override)

Run: python3.12 scripts/test_staging_gate_integration.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import table_rebuild_staging as STAGING  # noqa: E402
import apply_itaa_table_fixes as DRIVER  # noqa: E402

SECTION = "6"
TABLE = {"header": ["Provision", "Regulator"],
         "rows": [{"item": "1", "cols": {"1": "Part 2A", "2": "APRA"}}]}

CORPUS_MD = """---
act: "itaa-1997"
part: "1"
division: "1"
section: "6"
compilation_no: "266"
---

# 6  Test section

prose line one
prose line two

| Item | Provision | Regulator |
| --- | --- | --- |
| 1 | Part 2A ast | erisked APRA |
| 1 | Part 2A ast | erisked APRA |
"""


def _prose(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.strip().startswith("|")]


def main() -> int:
    bad = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:34s} {extra}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus_dir = tmp / "corpus" / "itaa-1997" / "sections" / "part-1" / "div-1"
        corpus_dir.mkdir(parents=True)
        live = corpus_dir / f"{SECTION}.md"
        live.write_text(CORPUS_MD)
        stage_dir = tmp / "stage"
        os.environ[STAGING.ENV_ROOT] = str(stage_dir)

        # Point the driver at the throwaway corpus and a fixed table.
        DRIVER.DATA = tmp / "corpus" / "itaa-1997" / "sections"
        DRIVER.extract_tables = lambda section: [TABLE]

        gate_ok = tmp / "gate-ok.json"
        gate_ok.write_text('{"verdict": "ACCEPTED", "gates": {"G1": {"pass": true}}}')
        gate_no = tmp / "gate-no.json"
        gate_no.write_text('{"verdict": "REJECTED", "gates": {"G1": {"pass": false}}}')

        def fresh() -> str:
            live.write_text(CORPUS_MD)
            return hashlib.sha256(live.read_bytes()).hexdigest()

        # 1. unstaged -> refused
        before = fresh()
        check("unstaged refused", DRIVER.apply_fix(SECTION) is False)
        check("corpus untouched", hashlib.sha256(live.read_bytes()).hexdigest() == before)

        # 2. staged, gate REJECTED -> refused
        cand_bad = tmp / "bad.md"
        cand_bad.write_text(CORPUS_MD.replace("| 1 | Part 2A ast | erisked APRA |",
                                              "| 1 | Part 2A | APRA |"))
        STAGING.stage("itaa-1997", SECTION, live, cand_bad, gate_report=gate_no,
                      root=stage_dir)
        before = fresh()
        check("gate REJECTED refused", DRIVER.apply_fix(SECTION) is False)
        check("corpus untouched", hashlib.sha256(live.read_bytes()).hexdigest() == before)

        # 3. staged + ACCEPTED but different bytes from this run -> refused
        STAGING.stage("itaa-1997", SECTION, live, cand_bad, gate_report=gate_ok,
                      root=stage_dir, force=True)
        check("staged READY", STAGING._inspect(stage_dir / "itaa-1997" / SECTION)["status"] == "READY")
        before = fresh()
        check("byte mismatch refused", DRIVER.apply_fix(SECTION) is False)
        check("corpus untouched", hashlib.sha256(live.read_bytes()).hexdigest() == before)

        # 4. the sanctioned flow: emit candidate -> gate -> stage -> apply
        before = fresh()
        emitted = tmp / "candidates" / f"{SECTION}.md"
        check("emit candidate", DRIVER.apply_fix(SECTION, emit_candidate=emitted) is True
              and emitted.is_file())
        check("emit changed nothing",
              hashlib.sha256(live.read_bytes()).hexdigest() == before)
        STAGING.stage("itaa-1997", SECTION, live, emitted, gate_report=gate_ok,
                      root=stage_dir, force=True)
        STAGING.review("itaa-1997", SECTION, by="integration-test", root=stage_dir)
        check("apply from staging", DRIVER.apply_fix(SECTION) is True)
        applied = live.read_text()
        check("applied == reviewed output", applied == emitted.read_text())
        check("prose preserved", _prose(applied) == _prose(CORPUS_MD))
        check("table rebuilt", "| 1 | Part 2A | APRA |" in applied
              and "erisked" not in applied)

        # 5. documented escape hatch still works (and is the only bypass)
        (stage_dir / "itaa-1997" / SECTION / "output.md").unlink()
        before = fresh()
        check("allow-unstaged overrides", DRIVER.apply_fix(SECTION, allow_unstaged=True) is True
              and hashlib.sha256(live.read_bytes()).hexdigest() != before)

    print("integration:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
