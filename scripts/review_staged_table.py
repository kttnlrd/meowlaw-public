#!/usr/bin/env python3
"""CDN-0193 Phase 1 — evidence for the mandatory review of a risk-flagged stage.

The staging convention BLOCKS a candidate carrying a risk flag until a review is
written against its exact output hash. This script produces the evidence that
review must be based on, so "reviewed" is a measurement and not a shrug.

It answers, with counts and named rows:

  1. PROSE    — every non-'|' source line is present byte-identical in the
                output (G2 restated, independently of the gate);
  2. ROWS     — every source '|' line the output does not carry, classified:
                  * header_repeat  — verbatim repeat of the table header or its
                    separator (corrupt files repeat the header once per source
                    page; the rebuild emits it once);
                  * absorbed       — its words all still occur in the output, so
                    it is a duplicate/shard of a row that IS carried;
                  * unaccounted    — words that occur NOWHERE in the output
                    (named, with the row). Non-empty means possible text loss.
  3. MULTISET — token delta between the source and output *data* rows (header
                row and separator excluded on both sides, so a replaced
                placeholder header cannot masquerade as loss).

Text is normalised for the comparison only: the corpus writes ASCII apostrophes
where the PDF carries U+2019, and a raw token compare reports that as loss.
Nothing is normalised in the file itself.

Exit 0 = nothing unaccounted for (review can be written); 1 = named rows require
a decision; 2 = refused (not staged).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import table_rebuild_staging as STAGING  # noqa: E402

QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                        "\u00ad": "", "\u2011": "-", "\u2013": "-", "\u2014": "-"})
TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def norm(text: str) -> str:
    return text.translate(QUOTES)


def toks(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(norm(text))]


def pipe_lines(text: str) -> list[str]:
    return [l for l in text.splitlines()
            if l.strip().startswith("|") and l.strip().count("|") >= 2]


def is_headerish(row: str) -> bool:
    """Header row or its separator (never data)."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    if all(set(c) <= set("-: ") for c in cells):
        return True
    head = " ".join(cells[:1]).lower()
    return head in {"item", "#", "no.", "ref"} and any(c.lower().startswith("column") for c in cells)


def data_rows(text: str) -> list[str]:
    return [r for r in pipe_lines(text) if not is_headerish(r)]


def prose_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if not l.strip().startswith("|")]


def review(act: str, section: str, root=None) -> dict:
    d = STAGING.stage_root(root) / act / section
    if not (d / "report.json").is_file():
        raise STAGING.StagingError(f"{act}/{section} is not staged at {d}")
    src = (d / "source.md").read_text(errors="replace")
    out = (d / "output.md").read_text(errors="replace")
    res: dict = {"act": act, "section": section, "dir": str(d)}

    # 1. prose preservation
    sp, op = prose_lines(src), prose_lines(out)
    res["prose"] = {"source_lines": len(sp), "output_lines": len(op),
                    "identical": sp == op, "missing": [l for l in sp if l not in op][:10]}

    # 2. dropped source rows, classified
    out_tokens = Counter(toks(out))
    out_rowset = Counter(r.strip() for r in pipe_lines(out))
    src_rowset = Counter(r.strip() for r in pipe_lines(src))
    dropped = [r for r, n in (src_rowset - out_rowset).items() for _ in range(n)]
    header_repeat, absorbed, unaccounted = [], [], []
    for row in dropped:
        if is_headerish(row):
            header_repeat.append(row)
            continue
        missing = Counter(toks(row)) - out_tokens
        if missing:
            unaccounted.append({"row": row[:140], "tokens": len(toks(row)),
                                "missing_tokens": sorted(missing)[:12],
                                "missing_count": sum(missing.values())})
        else:
            absorbed.append(row)
    res["rows"] = {
        "source_rows": len(src_rowset), "output_rows": len(out_rowset),
        "dropped": len(dropped), "header_repeat": len(header_repeat),
        "absorbed": len(absorbed), "unaccounted": unaccounted,
        "absorbed_sample": sorted(absorbed)[:3],
    }

    # 3. token multiset over data rows only
    st = Counter(toks("\n".join(data_rows(src))))
    ot = Counter(toks("\n".join(data_rows(out))))
    res["data_multiset"] = {
        "source_tokens": sum(st.values()), "output_tokens": sum(ot.values()),
        "source_data_rows": len(set(data_rows(src))), "output_data_rows": len(set(data_rows(out))),
        "only_in_source": sorted(st - ot)[:25], "only_in_output": sorted(ot - st)[:25],
        "missing_total": sum((st - ot).values()),
    }
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--section", required=True)
    ap.add_argument("--root")
    ap.add_argument("--json-out")
    a = ap.parse_args()
    try:
        res = review(a.act, a.section, a.root)
    except STAGING.StagingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(res, indent=2, ensure_ascii=False))

    p, r, m = res["prose"], res["rows"], res["data_multiset"]
    print(f"== {res['act']}/{res['section']}   ({res['dir']})")
    print(f"1. PROSE     {p['source_lines']} source / {p['output_lines']} output lines, "
          f"byte-identical={p['identical']}")
    for x in p["missing"]:
        print(f"     MISSING PROSE: {x[:110]}")
    print(f"2. ROWS      source {r['source_rows']} / output {r['output_rows']} unique pipe rows; "
          f"dropped {r['dropped']} = header_repeat {r['header_repeat']} + absorbed "
          f"{r['absorbed']} + UNACCOUNTED {len(r['unaccounted'])}")
    for u in r["unaccounted"][:15]:
        print(f"     UNACCOUNTED ({u['missing_count']} tokens of {u['tokens']}): {u['row'][:100]}")
        print(f"       missing: {', '.join(u['missing_tokens'])}")
    print(f"3. DATA      source {m['source_data_rows']} rows / {m['source_tokens']} tokens -> "
          f"output {m['output_data_rows']} rows / {m['output_tokens']} tokens")
    print(f"     tokens only in source: {', '.join(m['only_in_source']) or '(none)'}")
    print(f"     tokens only in output: {', '.join(m['only_in_output'][:15]) or '(none)'}")
    clean = p["identical"] and not r["unaccounted"] and m["missing_total"] == 0
    print("review:", "clean — nothing unaccounted for" if clean
          else "NAMED ITEMS REQUIRE A DECISION")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
