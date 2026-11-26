#!/usr/bin/env python3.12
"""Prove the local embedding pipeline can only delete rows it was responsible for.

The damage this pins: the "stale" set was `existing_files - seen_files`, where seen_files held only
the directories the run walked (data/*/sections).  Every ruling and case row in embeddings.db was
therefore classified stale - 260,297 rows deleted - because those paths are not under
data/*/sections.  Nothing was missing from disk; "not in my scope" was read as "gone".

The cases below are the ones that matter, and the last one asserts the OLD rule would have deleted
the ruling rows, so this test fails if the scoping is removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.embed_legislation import _stale_rows  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def old_rule(existing: set[str], seen: set[str]) -> set[str]:
    """The rule as it was, kept here only to prove the new one is stricter."""
    return existing - seen


def main() -> int:
    walked = {"itaa-1997/sections/", "fbt-1986/sections/"}
    existing = {
        "itaa-1997/sections/part-1/1.md",          # in scope, walked, fine
        "itaa-1997/sections/part-1/2.md",          # in scope, walked, fine
        "fbt-1986/sections/part-i/2a.md",          # in scope, walked, fine
        "fbt-1986/sections/old-gone.md",           # in scope, file deleted from corpus
        "private_rulings/1011261243735.json",      # ANOTHER producer
        "rulings/TR2020-1.json",                   # ANOTHER producer
        "summaries/case-1.json",                   # ANOTHER producer
        "nz-it-2007/sections/EX-35.md",            # another act's sections, not walked this run
    }
    seen = {
        "itaa-1997/sections/part-1/1.md",
        "itaa-1997/sections/part-1/2.md",
        "fbt-1986/sections/part-i/2a.md",
    }

    print("== in-scope deletion still works ==")
    stale, in_scope = _stale_rows(existing, seen, walked)
    check("a walked, now-missing file is stale", "fbt-1986/sections/old-gone.md" in stale, str(sorted(stale)))
    check("files that were walked are never stale",
          not (seen & stale))

    print("== other producers' rows are untouchable ==")
    for foreign in ("private_rulings/1011261243735.json", "rulings/TR2020-1.json",
                    "summaries/case-1.json", "nz-it-2007/sections/EX-35.md"):
        check(f"survives: {foreign}", foreign not in stale)

    print("== a run that walked nothing may delete nothing ==")
    stale_empty, in_scope_empty = _stale_rows(existing, set(), set())
    check("empty walked_roots -> empty stale set", stale_empty == set())
    check("empty walked_roots -> empty scope", in_scope_empty == set())

    print("== negative control: the old rule would have deleted the ruling rows ==")
    old = old_rule(existing, seen)
    check("old rule deleted a private ruling", "private_rulings/1011261243735.json" in old)
    check("old rule deleted a ruling", "rulings/TR2020-1.json" in old)
    check("old rule deleted a case summary", "summaries/case-1.json" in old)
    check("new rule deletes strictly less than the old rule", stale < old,
          f"new={len(stale)} old={len(old)}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(f"all checks passed ({len(existing)} stored files, {len(stale)} deletable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
