#!/usr/bin/env python3.12
"""Inventory formula fences in ITAA 1997 and find which are MISSING an operator.

The re-ingest keeps a region it cannot prove is a table as a fenced block whose marker names its own
source page:

    ```ingest-formula source=vol08.pdf page=160
    Owned deductions + Acquired deductions        *Corporate tax rate
    ```

That marker is the reliable locator - unlike /tmp/operator-worklist.json, whose section-to-page
mapping the ingest gate refuses (R0_section_located) and whose expected characters came from the gate
rather than the page. Drawn operators are absent from the text layer, so a formula whose terms are
separated only by spaces is a formula missing its operators.

Heuristic for 'missing operator': a fence line with two or more runs separated by a wide gap (2+
spaces) or containing a term followed directly by a capitalised defined term ('*Corporate tax rate'),
where no operator character appears between them. Reported with the source page so each one can be
read and fixed individually. Nothing is written here.
"""
import collections, json, pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
SECTIONS = REPO / "data/itaa-1997/sections"
FENCE = re.compile(r"```ingest-formula source=(\S+) page=(\d+)")
OPS = "+\u2212\u00d7\u00f7="          # + - x / =  (the operators that survive the tokeniser)

rows = []
for p in sorted(SECTIONS.rglob("*.md")):
    lines = p.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        body = []
        j = i + 1
        while j < len(lines) and not lines[j].startswith("```"):
            body.append(lines[j])
            j += 1
        for ln in body:
            if not ln.strip() or ln.strip().startswith("---"):
                continue
            has_op = any(o in ln for o in OPS)
            wide_gap = re.search(r"\S\s{2,}\S", ln) is not None
            if not has_op or wide_gap:
                rows.append({"section": p.stem, "file": str(p.relative_to(REPO)),
                             "source": m.group(1), "page": int(m.group(2)),
                             "line": ln, "has_operator": has_op,
                             "reason": "no operator on the line" if not has_op else "terms separated by a gap"})
        i = j

print(f"formula fences with a suspect line: {len(rows)}")
print(f"  distinct sections: {len({r['section'] for r in rows})}")
print(f"  no operator at all: {sum(1 for r in rows if not r['has_operator'])}")
print(f"  gap-separated terms: {sum(1 for r in rows if r['has_operator'])}")
by_src = collections.Counter((r["source"], r["page"]) for r in rows)
print(f"  distinct source pages: {len(by_src)}")
print("\n  first 12:")
for r in rows[:12]:
    print(f"    {r['section']:9s} {r['source']} p{r['page']:<4d} {r['reason']:28s} {r['line'].strip()[:62]}")
pathlib.Path("/tmp/opcrops/formula-inventory.json").write_text(json.dumps(rows, indent=1))
print(f"\n  full inventory: /tmp/opcrops/formula-inventory.json")
