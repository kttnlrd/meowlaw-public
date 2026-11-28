#!/usr/bin/env python3
"""CDN-0170 repair: word-duplication artifacts in section .md files.

Usage:
  python3 dup_repair.py --dry-run   # list changes without writing
  python3 dup_repair.py --apply     # apply changes

Only replaces consecutive-duplicate words in the artifact set, with context
guards for legit legal English. Writes are idempotent; re-run dry-run after
apply must show 0 changes.
"""
import argparse
import re
import sys
from pathlib import Path

DATA = Path(f"{Path(__file__).resolve().parent.parent}/data")

# Words that are extraction artifacts when doubled (from the corpus scan).
# NOT in this set: that, had, would, will, may, shall, is, as, in, of, the,
# to, it, or, and, be, can, was, were, not, no, if, when, whether (legal English).
ARTIFACT_WORDS = {
    "yes", "exempt", "property", "purposes", "subdivision", "regulations",
    "nil", "payment", "income", "company", "entity", "duty", "asic", "gst",
    "dividends", "test", "interest", "amount", "entities", "land", "trusts",
    "credits", "lease", "shares", "professional", "expenditure", "been",
    "distribution", "employees", "trustee", "division", "circumstances",
    "day", "base", "child", "tax", "disabilities", "accounts", "number",
    "taxation", "recommendations", "member", "institution", "fixtures",
    "fittings", "plant", "machinery", "premises", "goods", "stock", "fund",
    "scheme", "arrangement", "market", "value", "cost", "gain", "loss",
}

# Words preceded by these are legit (e.g. "had had", "that that" handled by
# exclusion from ARTIFACT set; this is an extra guard for hyphenated forms)
# Requires SINGLE-SPACE gap + lowercase prose continuation — this distinguishes
# real extraction artifacts ("property property of a company") from CCH
# space-aligned table column boundaries ("Basic amount              amount Advance").
DUP_RE = re.compile(r"\b([A-Za-z]{3,}) (\1)(\s+[a-z])", re.IGNORECASE)


def is_artifact(word: str) -> bool:
    return word.lower() in ARTIFACT_WORDS


def collect_changes() -> list[tuple[str, int, str, str]]:
    """Return [(relpath, line_no, original, fixed)]."""
    changes = []
    for act_dir in sorted(DATA.iterdir()):
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines, 1):
                for m in DUP_RE.finditer(line):
                    w = m.group(1)
                    if not is_artifact(w):
                        continue
                    # context guard: skip inside code/table delimiters (| or ---)
                    # (table cells legitimately repeat column headers in some acts)
                    stripped = line.strip()
                    if stripped.startswith("|") and "|" in stripped[1:]:
                        continue
                    # context guard (CDN-0170 audit finding): skip flattened
                    # space-aligned table rows (2+ gaps of 3+ spaces) — the
                    # doubled word may be two column headers colliding, not a
                    # duplicate typo. Those are repaired manually, per-file.
                    if len(re.findall(r" {3,}", line)) >= 2:
                        continue
                    # context guard: NZ YA-1 style mega-definition lines where
                    # adjacent defined terms run together ("tax credit credit
                    # transfer notice" = term1 ends + term2 starts). Not an
                    # artifact — the second occurrence starts the next term.
                    if act_dir.name == "nz-it-2007":
                        continue
                    # Capitalized second occurrence = likely heading-bleed or
                    # list-item structure ("Land Land is capable..." = heading
                    # remnant + body; "Hire of goods duty Duty on rental..." =
                    # list item + description). Needs per-instance judgment —
                    # report, don't auto-fix.
                    if m.group(2)[0].isupper():
                        continue
                    # fix: drop second occurrence (keep first)
                    # groups: 1=word, 2=dup word, 3=following whitespace+lowercase
                    fixed_line = line[: m.start(1)] + line[m.end(1):]
                    if fixed_line != line:
                        changes.append((str(p.relative_to(DATA)), i, line, fixed_line))
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    changes = collect_changes()
    print(f"TOTAL CHANGES: {len(changes)}")
    if args.limit:
        changes = changes[: args.limit]

    for rel, line_no, old, new in changes:
        print(f"  {rel}:{line_no}: {old.strip()[:60]!r} -> {new.strip()[:60]!r}")

    if args.apply:
        # group by file, apply in reverse line order per file
        by_file: dict[str, list] = {}
        for rel, line_no, old, new in changes:
            by_file.setdefault(rel, []).append((line_no, old, new))
        for rel, edits in by_file.items():
            p = DATA / rel
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            for line_no, old, new in sorted(edits, reverse=True):
                lines[line_no - 1] = new
            p.write_text("".join(lines), encoding="utf-8")
        print(f"\nAPPLIED to {len(by_file)} files")
    else:
        print("\n(dry-run; use --apply to write)")


if __name__ == "__main__":
    main()
