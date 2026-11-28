#!/usr/bin/env python3
"""CDN-0193 — the human worklist behind the re-ingest gate's needs_review.

A preserved region is text the ingester could not PROVE was a table, so it kept
it verbatim.  R1/R2 prove the bytes are complete and real; nothing proves what
the structure MEANT.  That is the only thing left for a human, and this turns
it into a queue: biggest regions first, because a 40-line preserved region is
where the reading is worth the most.

Input is the ingest report (scripts/ingest_itaa_compilation.py --report).
Output is worklist.json + worklist.md in --out.

  python3.12 scripts/reingest_review_worklist.py --report /tmp/fv3-report.json \\
      --out ~/table-rebuild-out
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path



# The ingester's reason strings carry the offending payload (which cells were
# empty, which line broke), so they are unique per section and useless as a
# count.  These patterns strip the payload back to the DEFECT, which is what
# decides whether the 508 are one problem or several.
CAUSES: list[tuple[str, re.Pattern]] = [
    ("formula_band: baselines packed tighter than body leading — rendered "
     "formula or figure, not a table", re.compile(r"baselines packed tighter")),
    ("too_few_rows: band held < 2 data rows", re.compile(r"data row\(s\), need")),
    ("page_split_continuation: block starts mid-row",
     re.compile(r"starts mid-row")),
    ("empty_header_cell: header row has a blank cell",
     re.compile(r"header cell\(s\) empty")),
    ("header_is_data_row: no header row could be identified",
     re.compile(r"header row is itself a data row")),
    ("formula_glyph_in_cells: G5 formula glyphs inside the band",
     re.compile(r"formula glyph\(s\)")),
    ("c11_bare_connector: a row ends on a bare connector (C11)",
     re.compile(r"row ends on bare connector")),
    ("c11_other: another C11 coherence detector fired",
     re.compile(r"C11 coherence")),
]


def cause_of(reason: str) -> str:
    for label, pat in CAUSES:
        if pat.search(reason):
            return label
    return f"other: {reason.strip()[:70]}"


def build(report: dict) -> dict:
    items = []
    for r in report["results"]:
        if not r["preserved_regions"]:
            continue
        regions = [{"kind": p["kind"], "page": p["page"], "lines": p["lines"],
                    "reason": p["reason"], "cause": cause_of(p["reason"])}
                   for p in r["preserved_regions"]]
        items.append({
            "act": report["act"],
            "section": r["section"],
            "path": r["path"],
            "volume": r["volume"],
            "pages": r["pages"],
            "status": r["status"],
            "tables": r["tables"],
            "pdf_tokens": r["pdf_tokens"],
            "regions": regions,
            "region_count": len(regions),
            "preserved_lines": sum(p["lines"] for p in regions),
        })
    items.sort(key=lambda i: (-i["preserved_lines"], -i["region_count"], i["section"]))

    causes = Counter(g["cause"] for i in items for g in i["regions"])
    kinds = Counter(g["kind"] for i in items for g in i["regions"])
    return {
        "source_report": report.get("out"),
        "act": report["act"], "comp": report["comp"],
        "sections_total": report["sections"],
        "sections_needing_review": len(items),
        "regions_total": sum(i["region_count"] for i in items),
        "preserved_lines_total": sum(i["preserved_lines"] for i in items),
        "by_cause": dict(causes.most_common()),
        "by_region_kind": dict(kinds.most_common()),
        "items": items,
    }


def to_md(w: dict) -> str:
    out = [f"# Re-ingest review worklist — {w['act']} compilation {w['comp']}", "",
           f"{w['sections_needing_review']} of {w['sections_total']} sections hold at "
           f"least one preserved region: {w['regions_total']} regions, "
           f"{w['preserved_lines_total']} lines of text whose STRUCTURE no machine "
           "has certified.  The bytes are already proven complete (R1) and "
           "invention-free (R2); what is open is what the region means.", "",
           "## Why a region was not proven a table", ""]
    for cause, n in w["by_cause"].items():
        out.append(f"- **{n}** — {cause}")
    out += ["", "## By region kind", ""]
    for kind, n in w["by_region_kind"].items():
        out.append(f"- **{n}** — `{kind}`")
    out += ["", "## Worklist (biggest preserved regions first)", "",
            "| # | section | pages | vol | regions | lines | pdf tokens | causes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n, i in enumerate(w["items"], 1):
        causes = ", ".join(sorted({g["cause"].split(":")[0] for g in i["regions"]}))
        out.append(f"| {n} | {i['section']} | {i['pages']} | {i['volume']} | "
                   f"{i['region_count']} | {i['preserved_lines']} | {i['pdf_tokens']} | {causes} |")
    out += ["", "## Detail", ""]
    for n, i in enumerate(w["items"], 1):
        out.append(f"### {n}. {i['section']}  (pp. {i['pages']}, {i['volume']}, "
                   f"{i['preserved_lines']} preserved lines)")
        out.append(f"`{i['path']}` — {i['tables']} proven table(s), "
                   f"{i['pdf_tokens']} PDF tokens")
        for g in i["regions"]:
            out.append(f"- p.{g['page']} `{g['kind']}` {g['lines']} line(s): {g['reason']}")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True, help="directory (outside the repo)")
    a = ap.parse_args()

    w = build(json.loads(Path(a.report).expanduser().read_text()))
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "worklist.json").write_text(json.dumps(w, indent=2, ensure_ascii=False) + "\n")
    (out / "worklist.md").write_text(to_md(w))
    print(f"{w['sections_needing_review']} section(s), {w['regions_total']} region(s), "
          f"{w['preserved_lines_total']} preserved line(s) -> {out}")
    for cause, n in w["by_cause"].items():
        print(f"  {n:5d}  {cause}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
