#!/usr/bin/env python3
"""CDN-0193 blocker: turn "C11 counts are a floor, not a measurement" into a measurement.

Reviewer finding (opus B6 / codex B5), verbatim from the plan's blocker table:

    "detector unchanged: classes are mutually exclusive per line and
     DANGLE_CELL_END only matches a connector before the *final* `|`,
     so 504 truncated is a lower bound"

Both halves are true, and both are *quantifiable* without changing the
detector the audit's finding counts depend on. The detector's per-line rules
are (scan_corpus_error_classes.py:235-268):

  * one class per line, checked in the order glyph -> truncated -> midword;
    the first match `continue`s, so a line that is BOTH glyphed and truncated
    is counted only as glyph;
  * midword `break`s after the FIRST split boundary, so N splits on one line
    = 1 finding;
  * DANGLE_CELL_END demands `\\s*\\|\\s*$` — the connector must precede the
    line's *final* pipe.

This script imports the detector's own helpers (no reimplementation, so
reproduction is exact), computes the full line-level truth table for the C11
flags, and reports:

  1. reproduction of the published counts (152 glyph / 115 midword / 504 truncated);
  2. true line incidence per class (a line counts once per property it has,
     regardless of the other properties);
  3. the undercount delta, split by cause (class shadowing vs first-boundary
     truncation vs final-pipe-only connector test);
  4. wider connector/shape probes to bound how much truncation is not
     detectable by the published regex at all.

Read-only. Writes .hermes/plans/cdn-0193-counts.json.

Usage: python3 scripts/c11_incidence_measurement.py [--show N]
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import scan_corpus_error_classes as S  # noqa: E402  (detector helpers, verbatim)

REPO = Path(__file__).resolve().parent.parent
DATA = S.DATA
OUT = REPO / ".hermes/plans/cdn-0193-counts.json"

# Connectors the published regex accepts.
CORE = set(
    "the and a an or of to for in on was is you if that which with by as at when".split()
)
# The same family extended to the rest of the function-word set that can
# dangle at a cut point ("The partnership and" / "... subject to the").
EXT = CORE | set(
    "are were be been being has have had do does did will would shall should "
    "may might must can could not no than then there their this these those "
    "such all any each every both other another only also so but because while "
    "where whose whom how why more most less least very just still yet ever "
    "never again further upon into onto within without through during before "
    "after above below between among against about under over since until till "
    "per via from".split()
)

SAFE = re.compile(r"\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tail(cell: str) -> str:
    return S._tail_word(cell).lower()


def classify_line(line: str) -> dict:
    """Full truth table for one pipe-table line (no short-circuit)."""
    lines_flags: dict[str, object] = {}

    gm = next((m for m in S.EXTRACT_GLYPH.finditer(line)
               if not S._is_natural_word_accent(line, m)), None)
    lines_flags["glyph"] = bool(gm)

    lines_flags["dangle_core_final"] = bool(S.DANGLE_CELL_END.search(line))

    cells = _cells(line)
    # connector ending ANY cell (this is what a mid-row cut looks like when the
    # row has more cells to the right, or a trailing empty cell)
    core_any = ext_any = 0
    for c in cells:
        t = _tail(c)
        if t in CORE:
            core_any += 1
        if t in EXT:
            ext_any += 1
    lines_flags["dangle_core_anycell"] = core_any
    lines_flags["dangle_ext_anycell"] = ext_any

    # shape probe the published regex cannot see: row's final cell is empty and
    # the cell before it ends on a connector ("| ... of | |")
    lines_flags["empty_final_cell"] = bool(cells and cells[-1] == "")

    # midword boundaries (all of them, not just the first)
    n_mid = 0
    if S._WORDS is not None:
        for ci in range(len(cells) - 1):
            tw = S._tail_word(cells[ci]).lower()
            hw = S._head_word(cells[ci + 1]).lower()
            if not tw or not hw:
                continue
            joined = tw + hw
            if joined in S._WORDS and not (tw in S._WORDS and hw in S._WORDS):
                n_mid += 1
    lines_flags["midword_n"] = n_mid
    return lines_flags


def published_scan(lines: list[str]) -> Counter:
    """Reproduce the detector's per-line short-circuit exactly."""
    c: Counter = Counter()
    for ln in lines:
        if not ln.startswith("|") or re.match(r"^\|\s*---", ln):
            continue
        gm = next((m for m in S.EXTRACT_GLYPH.finditer(ln)
                   if not S._is_natural_word_accent(ln, m)), None)
        if gm:
            c["C11_table_glyph"] += 1
            continue
        if S.DANGLE_CELL_END.search(ln):
            c["C11_table_truncated"] += 1
            continue
        if S._WORDS is None:
            continue
        cells = [x.strip() for x in ln.strip().strip("|").split("|")]
        for ci in range(len(cells) - 1):
            tw = S._tail_word(cells[ci]).lower()
            hw = S._head_word(cells[ci + 1]).lower()
            if not tw or not hw:
                continue
            joined = tw + hw
            if joined in S._WORDS and not (tw in S._WORDS and hw in S._WORDS):
                c["C11_table_midword"] += 1
                break
    return c


def main() -> int:
    show = 3
    if "--show" in sys.argv:
        show = int(sys.argv[sys.argv.index("--show") + 1])

    truth: Counter = Counter()
    per_act: dict[str, Counter] = {}
    examples: dict[str, list[str]] = {}
    published: Counter = Counter()
    files = 0
    table_lines = 0

    for act_dir in sorted(DATA.iterdir()):
        sec = act_dir / "sections"
        if not sec.is_dir():
            continue
        for p in sorted(sec.rglob("*.md")):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            files += 1
            plines = [ln for ln in lines
                      if ln.startswith("|") and not re.match(r"^\|\s*---", ln)]
            table_lines += len(plines)
            published.update(published_scan(lines))
            act = act_dir.name
            pa = per_act.setdefault(act, Counter())
            for ln in plines:
                f = classify_line(ln)
                glyph = f["glyph"]
                tr_final = f["dangle_core_final"]
                tr_core_any = f["dangle_core_anycell"] > 0
                tr_ext_any = f["dangle_ext_anycell"] > 0
                mid_n = int(f["midword_n"])
                if glyph:
                    truth["has_glyph"] += 1
                    pa["has_glyph"] += 1
                if tr_final:
                    truth["has_trunc_final"] += 1
                    pa["has_trunc_final"] += 1
                if tr_core_any:
                    truth["has_trunc_core_anycell"] += 1
                    pa["has_trunc_core_anycell"] += 1
                if tr_ext_any:
                    truth["has_trunc_ext_anycell"] += 1
                    pa["has_trunc_ext_anycell"] += 1
                    if not tr_final:
                        truth["trunc_ext_only"] += 1
                        examples.setdefault("trunc_ext_only", [])
                        if len(examples["trunc_ext_only"]) < 6:
                            examples["trunc_ext_only"].append(f"{p.relative_to(DATA)}: {ln.strip()[:130]}")
                if mid_n:
                    truth["has_midword"] += 1
                    truth["midword_boundaries"] += mid_n
                    pa["has_midword"] += 1
                    if mid_n > 1:
                        truth["has_midword_multi"] += 1
                # shadowing matrix
                key = ("glyph" if glyph else "-") + "/" + ("trunc" if tr_final else "-") + "/" + ("mid" if mid_n else "-")
                truth[f"combo:{key}"] += 1

    # undercount decomposition
    shadow_glyph_trunc = truth["combo:glyph/trunc/-"] + truth["combo:glyph/trunc/mid"]
    shadow_glyph_mid = truth["combo:glyph/-/mid"]
    shadow_trunc_mid = truth["combo:-/trunc/mid"]
    repro_glyph = published["C11_table_glyph"]
    repro_trunc = published["C11_table_truncated"]
    repro_mid = published["C11_table_midword"]

    print(f"files scanned        : {files:,}")
    print(f"pipe-table lines     : {table_lines:,}")
    print()
    print("REPRODUCTION of the published detector (must match 152 / 115 / 504):")
    print(f"  glyph     published={repro_glyph:5d}  truth(lines with glyph)={truth['has_glyph']:5d}")
    print(f"  truncated published={repro_trunc:5d}  truth(lines w/ final-cell connector)={truth['has_trunc_final']:5d}")
    print(f"  midword   published={repro_mid:5d}  truth(lines w/ >=1 split)={truth['has_midword']:5d}  (boundaries={truth['midword_boundaries']})")
    print()
    print("UNDERCOUNT vs line incidence:")
    print(f"  truncated lines hidden by glyph shadowing : {shadow_glyph_trunc}")
    print(f"  midword lines hidden by glyph shadowing   : {shadow_glyph_mid}")
    print(f"  midword lines hidden by trunc shadowing   : {shadow_trunc_mid}")
    print(f"  truncated visible if class exclusivity dropped : {truth['has_trunc_final'] - repro_trunc}")
    print(f"  midword  visible if class exclusivity dropped : {truth['has_midword'] - repro_mid}")
    print()
    print("WIDER SHAPE PROBE (not detectable by published regex):")
    print(f"  lines with connector ending ANY cell : {truth['has_trunc_core_anycell']}")
    print(f"  lines with extended-connector any cell: {truth['has_trunc_ext_anycell']}")
    print(f"  of which NOT caught by the final-pipe regex: {truth['trunc_ext_only']}")
    print()
    print("CO-OCCURRENCE MATRIX (glyph/trunc/mid):")
    for k in sorted(k for k in truth if k.startswith("combo:")):
        print(f"  {k[6:]:18s} {truth[k]:5d}")

    for b, ex in examples.items():
        print(f"\n--- examples: {b} ---")
        for e in ex[:show]:
            print("  " + e[:200])

    payload = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "ticket": "CDN-0193",
        "purpose": ("measure the published C11 floor: line-level incidence vs the "
                    "detector's mutually-exclusive, first-match-wins attribution"),
        "detector": "scripts/scan_corpus_error_classes.py (imported, helpers used verbatim)",
        "files_scanned": files,
        "pipe_table_lines": table_lines,
        "published_counts": dict(published),
        "line_incidence": {k: v for k, v in truth.items() if not k.startswith("combo:")},
        "combo_matrix": {k[6:]: v for k, v in truth.items() if k.startswith("combo:")},
        "undercount": {
            "truncated_hidden_by_glyph_shadowing": shadow_glyph_trunc,
            "midword_hidden_by_glyph_shadowing": shadow_glyph_mid,
            "midword_hidden_by_trunc_shadowing": shadow_trunc_mid,
            "truncated_true_minus_published": truth["has_trunc_final"] - repro_trunc,
            "midword_true_minus_published": truth["has_midword"] - repro_mid,
        },
        "wider_shape_probe": {
            "connector_ends_any_cell_core": truth["has_trunc_core_anycell"],
            "connector_ends_any_cell_extended": truth["has_trunc_ext_anycell"],
            "not_caught_by_final_pipe_regex": truth["trunc_ext_only"],
        },
        "per_act": {a: dict(c) for a, c in sorted(per_act.items())},
        "examples": examples,
        "interpretation": (
            "line incidence is a measurement of the same detector over the same corpus; "
            "it is NOT a new finding class and does not change any ticket's remediation. "
            "Published counts are per-class-first-match, so they undercount every class "
            "except the first-checked one (glyph)."
        ),
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nwritten: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
