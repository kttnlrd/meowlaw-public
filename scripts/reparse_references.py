#!/usr/bin/env python3
"""One-off deterministic enrichment: top up cases_referenced / related_rulings
on already-generated ruling summaries from the source text's References section
(LK-03 cheaper path — no LLM calls). Additive only; never removes entries.

Reuses parse_references_section() from generate_ruling_summaries.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_ruling_summaries import (  # noqa: E402
    ALL_TYPES,
    OUTPUT_DIR,
    get_ruling_files,
    parse_citation,
    parse_references_section,
)


def norm(x: str) -> str:
    return re.sub(r"\s+", " ", str(x)).strip().lower()


def main() -> None:
    total = 0
    changed = 0
    missing_summary = 0
    read_error = 0
    for rtype in sorted(ALL_TYPES):
        for f in get_ruling_files(rtype):
            total += 1
            citation = parse_citation(f.name, rtype)
            out_path = OUTPUT_DIR / f"{citation.replace(' ', '_').replace('/', '_')}.json"
            if not out_path.exists():
                missing_summary += 1
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                read_error += 1
                continue
            refs = parse_references_section(text)
            if not refs["cases_referenced"] and not refs["related_rulings"]:
                continue
            try:
                summary = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            dirty = False
            for key in ("cases_referenced", "related_rulings"):
                original = list(summary.get(key) or [])
                merged = list(original)
                have = {norm(x) for x in merged}
                for item in refs[key]:
                    if norm(item) not in have:
                        merged.append(item)
                        have.add(norm(item))
                if merged != original:
                    summary[key] = merged
                    dirty = True
            if dirty:
                out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
                changed += 1
    print(
        f"total_sources={total} changed={changed} "
        f"missing_summary={missing_summary} read_error={read_error}"
    )


if __name__ == "__main__":
    main()
