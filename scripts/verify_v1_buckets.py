#!/usr/bin/env python3
"""Validate the CDN-0193 v1 triage buckets before trusting the v2 delta.

Reimplements `scan_truncation_scope.py`'s classification *verbatim* (last
4-word suffix, last-occurrence match over the concatenated raw lines of the
act, page split guessed from the next 1-3 non-empty lines) and runs it twice:

  A. exactly as v1 did it  -> must reproduce .hermes/plans/cdn-0193-scope.json
                              (338 / 117 / 49) or the harness differs elsewhere
  B. with itaa-1997 pointed at the comp-266 staging raw -> isolates how much of
     v1's bucket split is an artefact of probing the stale comp-263 repo raw.

Usage: python3 scripts/verify_v1_buckets.py
"""
from __future__ import annotations
import os

import json
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STAGING = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data"))
FINDINGS = Path("/tmp/corpus_scan_20260826.json")
V1 = REPO / ".hermes/plans/cdn-0193-scope.json"

FOOTER = re.compile(
    r"compilation no|authorised version|^\s*\d+\s*$|"
    r"^\s*(income tax assessment act|taxation administration act|"
    r"a new tax system|fringe benefits tax assessment act|"
    r"superannuation industry \(supervision\) act)",
    re.I,
)
NEWROW = re.compile(r"^\s*(item\b|\d+(\.\d+)*[A-Z]?\s|\(\w+\))", re.I)


def norm(s: str) -> str:
    s = re.sub(r"[*_`]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def raw_lines(act: str, staging_itaa: bool):
    base = STAGING / "itaa-1997" / "raw" if (staging_itaa and act == "itaa-1997") else DATA / act / "raw"
    out = []
    for p in sorted(base.glob("*.txt")):
        for ln in p.read_text(errors="replace").splitlines():
            out.append(norm(ln))
    return out


def run(staging_itaa: bool, dump: Path | None = None):
    findings = json.loads(FINDINGS.read_text())
    trunc = [f for f in findings if f["class"] == "C11_table_truncated"]
    stats = Counter()
    per_act: dict[str, Counter] = {}
    per_finding: dict[str, str] = {}
    cache: dict[str, list[str]] = {}
    for f in trunc:
        path = f["path"]
        act = path.split("/", 1)[0]
        m = re.search(r"line (\d+)", f["detail"])
        if not m:
            continue
        ln_no = int(m.group(1))
        try:
            md_line = (DATA / path).read_text(errors="replace").splitlines()[ln_no - 1]
        except Exception:
            continue
        cells = [c.strip() for c in md_line.strip().strip("|").split("|")]
        if not cells:
            continue
        tail = norm(cells[-1])
        probe = " ".join(tail.split()[-4:])
        if not probe:
            continue
        if act not in cache:
            cache[act] = raw_lines(act, staging_itaa)
        lines = cache[act]
        hit = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].endswith(probe):
                hit = i
                break
        if hit is None:
            bucket = "probe_not_found"
        else:
            nxt = None
            for j in range(hit + 1, min(hit + 4, len(lines))):
                if lines[j]:
                    nxt = j
                    break
            if nxt is None or FOOTER.search(lines[nxt]) or NEWROW.match(lines[nxt]):
                bucket = "page_split"
            else:
                bucket = "parser_side_recoverable"
        stats[bucket] += 1
        per_act.setdefault(act, Counter())[bucket] += 1
        per_finding[f"{path}:{ln_no}"] = bucket
    if dump is not None:
        dump.write_text(json.dumps(per_finding, indent=1))
        print(f"per-finding v1 buckets -> {dump}")
    return stats, per_act, len(trunc)


def main():
    expected = json.loads(V1.read_text())["stats"]
    print("v1 declared buckets:", expected)
    a_stats, a_act, n = run(staging_itaa=False, dump=Path("/tmp/v1_buckets.json"))
    print(f"\nA. v1 method, v1 sources (stale comp-263 repo raw for itaa-1997) — {n} findings")
    for k in ("parser_side_recoverable", "page_split", "probe_not_found"):
        mark = "MATCH" if a_stats.get(k, 0) == expected.get(k) else f"DIFFERS (v1={expected.get(k)})"
        print(f"   {k:26s} {a_stats.get(k, 0):4d}  {mark}")
    b_stats, b_act, _ = run(staging_itaa=True)
    print("\nB. v1 method, itaa-1997 pointed at comp-266 staging raw")
    for k in ("parser_side_recoverable", "page_split", "probe_not_found"):
        print(f"   {k:26s} {b_stats.get(k, 0):4d}")
    print("\nper-act delta on itaa-1997 (A -> B):")
    for k in ("parser_side_recoverable", "page_split", "probe_not_found"):
        print(f"   {k:26s} {a_act.get('itaa-1997', {}).get(k, 0):4d} -> {b_act.get('itaa-1997', {}).get(k, 0):4d}")


if __name__ == "__main__":
    main()
