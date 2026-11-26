#!/usr/bin/env python3
"""
Scan ALL legislation section files for garbled/corrupted text, full scan
(no sampling). Catches the corruption Harry flagged in ITAA 1936 s 170(7):
two-column tables flattened into a single line with columns interleaved.

Signatures checked per file:
  T1  "In this case: ... the position is:" on one line (table flattened)
  T2  "Item ... In this case:" interleave remnants
  T3  Multi-word run-ons: two definition verbs on one line with no newline
      between term paragraphs ("... means ... . nextterm means ...")
  T4  Heading glued to preceding sentence ("text. Heading" on same line)
  T5  Lowercase sentence starting mid-line after a full stop (scrape gluing)
  T6  Table cell interleave pattern: >40% whitespace-heavy runs

Output: data/garbled_scan_report.json + console summary
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
OUT_PATH = DATA_DIR / "garbled_scan_report.json"

ACTS = [
    "itaa-1997", "itaa-1936", "gst-1999", "taa-1953", "fbt-1986",
    "sis-1993", "corporations-act-2001", "aml-ctf-2006", "nz-it-2007",
]

# T1: "In this case:" and "the position is:" on the same line,
#     NOT as a markdown table header row (which has | pipes)
T1_RE = re.compile(r"In this case:.*the position is:", re.IGNORECASE)

# T2: "Item" + "In this case:" on one line (flattened item table)
T2_RE = re.compile(r"\bItem\b[^\n]{0,20}In this case:", re.IGNORECASE)

# T3: run-on definitions — two "means" definitional verbs on one line
#     separated only by a sentence (the classic 1936 run-on).
#     e.g. "scheme benefit has the meaning given by section 284-150...
#          scheme has the meaning given by subsection 995-1(1)"
T3_RE = re.compile(
    r"(?:has the meaning given by|means)\b[^.\n]{0,120}\."
    r"[^.\n]{5,120}\b(?:has the meaning given by|means)\b",
    re.IGNORECASE,
)

# T4: sentence-ending period immediately followed by a Capitalised heading
#     word on the same line (e.g. "attributable to that decrease. Definitions")
#     — but only MID-LINE (a line legitimately starting "**Note:**" is fine).
T4_RE = re.compile(
    r"[^.\n]\.(?:[ \t]|[ \t]*Note[ \t]*\d?[A-Za-z]?:?[ \t]*)"
    r"(?:Definitions|Interpretation|Application|Transitional|"
    r"Operation|Extensions|Amendment|Notes?|Example|Examples)\b",
)

# T5: mid-line new-sentence start after ". The" style gluing with a lowercase
#     continuation (e.g. "... decrease. Definitions" or "...(1). 5. ...")
T5_RE = re.compile(r"\.\s+[a-z]{2,}\s+(?:and|or|but|the|if|where)\b")

# T6: whitespace runs suggesting a flattened table (many long space gaps
#     within a single line)
T6_RE = re.compile(r" {15,}")

SKIP_PATTERNS = [
    # markdown tables legitimately have pipes and long spaces in headers
    re.compile(r"^\|.*\|$"),
    # code-like / preformatted blocks
    re.compile(r"^```"),
    re.compile(r"^\s{4,}"),
]


def scan_file(path: Path) -> dict:
    text = path.read_text()
    lines = text.splitlines()
    issues = {"T1": [], "T2": [], "T3": [], "T4": [], "T5": [], "T6": []}
    for i, line in enumerate(lines, 1):
        if any(sk.match(line) for sk in SKIP_PATTERNS):
            continue
        if T1_RE.search(line):
            issues["T1"].append((i, line.strip()[:150]))
        if T2_RE.search(line):
            issues["T2"].append((i, line.strip()[:150]))
        if T3_RE.search(line):
            issues["T3"].append((i, line.strip()[:150]))
        if T4_RE.search(line):
            issues["T4"].append((i, line.strip()[:150]))
        if T5_RE.search(line):
            issues["T5"].append((i, line.strip()[:150]))
        # T6 only when line is long and has big whitespace gaps
        if len(line) > 300 and T6_RE.search(line):
            issues["T6"].append((i, line.strip()[:150]))
    return issues


def main():
    report = {}
    grand = {k: 0 for k in ("T1", "T2", "T3", "T4", "T5", "T6")}
    files_with_issues = 0
    for act in ACTS:
        act_dir = DATA_DIR / act / "sections"
        if not act_dir.is_dir():
            print(f"[skip] {act}: no sections dir")
            continue
        files = list(act_dir.rglob("*.md"))
        act_issues = {k: [] for k in grand}
        act_files = 0
        for f in files:
            issues = scan_file(f)
            if any(issues.values()):
                act_files += 1
                rel = str(f.relative_to(DATA_DIR))
                for k, hits in issues.items():
                    for ln, snippet in hits[:5]:
                        act_issues[k].append({"file": rel, "line": ln, "text": snippet})
        total_issues = sum(len(v) for v in act_issues.values())
        report[act] = {
            "files_scanned": len(files),
            "files_with_issues": act_files,
            "issue_count": total_issues,
            "issues": {k: v[:50] for k, v in act_issues.items() if v},
        }
        files_with_issues += act_files
        for k in grand:
            grand[k] += len(act_issues[k])
        print(f"{act:28s} {len(files):5d} files, {act_files:4d} garbled, {total_issues:5d} issues")

    print(f"\nTOTAL: {files_with_issues} files with garbled text across {len(ACTS)} acts")
    print("by signature:", dict(grand))
    with open(OUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"report: {OUT_PATH}")


if __name__ == "__main__":
    main()
