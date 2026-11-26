#!/usr/bin/env python3
"""Size the CDN-0193 C11_table_truncated class.

For every C11_table_truncated finding, probe the act's raw extraction text
(data/<act>/raw/*.txt) for the offending cell's tail. If the raw line that
carries the tail CONTINUES onto a following raw line that is body text (not a
page footer/header, not a fresh table row), the loss is parser-side and the
text is recoverable from the repo raw. If the raw line also stops there, the
row was split at a PDF page boundary and recovery needs the source PDF.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
FINDINGS = Path("/tmp/corpus_scan_20260826.json")

FOOTER = re.compile(
    r"compilation no|authorised version|^\s*\d+\s*$|"
    r"^\s*(income tax assessment act|taxation administration act|"
    r"a new tax system|fringe benefits tax assessment act|"
    r"superannuation industry \(supervision\) act)",
    re.I,
)
# a fresh table row / header starts a new logical row, not a continuation
NEWROW = re.compile(r"^\s*(item\b|\d+(\.\d+)*[A-Z]?\s|\(\w+\))", re.I)


def norm(s: str) -> str:
    s = re.sub(r"[*_`]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def raw_lines(act: str):
    out = []
    for p in sorted((DATA / act / "raw").glob("*.txt")):
        for ln in p.read_text(errors="replace").splitlines():
            out.append(norm(ln))
    return out


def main() -> int:
    findings = json.loads(FINDINGS.read_text())
    trunc = [f for f in findings if f["class"] == "C11_table_truncated"]

    cache: dict[str, list[str]] = {}
    stats = {"parser_side_recoverable": 0, "page_split": 0, "probe_not_found": 0}
    per_act: dict[str, dict] = {}
    detail_examples: dict[str, list[str]] = {k: [] for k in stats}

    for f in trunc:
        path = f["path"]  # e.g. itaa-1997/sections/...
        act = path.split("/", 1)[0]
        m = re.search(r"line (\d+)", f["detail"])
        if not m:
            continue
        ln_no = int(m.group(1))
        md = DATA / path
        try:
            md_line = md.read_text(errors="replace").splitlines()[ln_no - 1]
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
            cache[act] = raw_lines(act)
        lines = cache[act]

        hit_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].endswith(probe):
                hit_idx = i
                break
        bucket = "probe_not_found"
        if hit_idx is not None:
            nxt = None
            for j in range(hit_idx + 1, min(hit_idx + 4, len(lines))):
                if lines[j]:
                    nxt = j
                    break
            if nxt is None or FOOTER.search(lines[nxt]) or NEWROW.match(lines[nxt]):
                bucket = "page_split"
            else:
                bucket = "parser_side_recoverable"
                if len(detail_examples[bucket]) < 8:
                    detail_examples[bucket].append(
                        f"{path}:{ln_no} tail={probe!r} cont={lines[nxt][:70]!r}"
                    )
        stats[bucket] += 1
        pa = per_act.setdefault(act, {"total": 0, "parser_side_recoverable": 0,
                                      "page_split": 0, "probe_not_found": 0})
        pa["total"] += 1
        pa[bucket] += 1

    print(f"C11_table_truncated findings: {len(trunc)}")
    print(f"  parser-side recoverable from repo raw : {stats['parser_side_recoverable']}")
    print(f"  split at PDF page boundary (needs PDF): {stats['page_split']}")
    print(f"  probe not located in raw              : {stats['probe_not_found']}")
    print()
    for act, pa in sorted(per_act.items()):
        print(f"{act:14s} total={pa['total']:4d}  recoverable={pa['parser_side_recoverable']:4d}"
              f"  page_split={pa['page_split']:4d}  not_found={pa['probe_not_found']:4d}")
    print()
    print("--- example recoverable (parser-side) ---")
    for e in detail_examples["parser_side_recoverable"]:
        print("  " + e)

    out = REPO / ".hermes/plans/cdn-0193-scope.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"stats": stats, "per_act": per_act}, indent=2))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
