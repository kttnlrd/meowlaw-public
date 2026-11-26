#!/usr/bin/env python3
"""Build data/private_rulings_outcomes.json from the private ruling corpus.

For each of the ~57.6k PBRs this extracts the QA pairs (the ATO's question
and answer), the ruling name/date/subject, and a coarse outcome label:

  yes    — every QA answer starts with "Yes"
  no     — every QA answer starts with "No"
  mixed  — answers are a mix (or none of the answers is a clean yes/no)
  ""     — no QA pairs present

Outcome is a search/ranking aid only — it is NOT a legal characterisation.
The label simply mirrors how the ATO answered the taxpayer's own question.

Usage:
  python3 scripts/build_private_ruling_outcomes.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

RULINGS_DIR = os.environ.get(
    "HERMES_RULINGS_DIR", os.path.join(os.path.expanduser(os.environ.get("HERMES_DIR", "~/.hermes")), "private_rulings"))
JSON_DIR = os.path.join(RULINGS_DIR, "data", "json")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "data", "private_rulings_outcomes.json")

QA_Q_MAX = 220
QA_A_MAX = 400
MAX_QAS = 5

_YES = re.compile(r"^\s*yes\b", re.IGNORECASE)
_NO = re.compile(r"^\s*no\b", re.IGNORECASE)


def classify(qa_pairs: list[dict]) -> str:
    """Coarse outcome: yes / no / mixed / '' (no QAs)."""
    if not qa_pairs:
        return ""
    flags = set()
    for qa in qa_pairs:
        ans = (qa.get("answer") or "").strip()
        if _YES.match(ans):
            flags.add("yes")
        elif _NO.match(ans):
            flags.add("no")
        else:
            flags.add("other")
    if flags == {"yes"}:
        return "yes"
    if flags == {"no"}:
        return "no"
    return "mixed"


def main() -> None:
    files = sorted(glob.glob(os.path.join(JSON_DIR, "*.json")))
    out: dict[str, dict] = {}
    counts = {"yes": 0, "no": 0, "mixed": 0, "": 0}
    errors = 0
    t0 = time.time()
    for i, f in enumerate(files):
        auth = os.path.splitext(os.path.basename(f))[0]
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            errors += 1
            continue
        qas = d.get("qa_pairs") or []
        out[auth] = {
            "name": d.get("name") or d.get("subject") or "",
            "date_of_advice": d.get("date_of_advice") or "",
            "subject": d.get("subject") or "",
            "outcome": classify(qas),
            "qa": [{
                "q": (q.get("question") or "")[:QA_Q_MAX],
                "a": (q.get("answer") or "")[:QA_A_MAX],
            } for q in qas[:MAX_QAS]],
        }
        counts[out[auth]["outcome"]] += 1
        if i and i % 10000 == 0:
            print(f"  {i}/{len(files)} processed", flush=True)
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    print(f"Wrote {len(out)} rulings to {OUTPUT} in {time.time()-t0:.0f}s")
    print(f"Outcomes: {counts}")
    print(f"Unreadable files: {errors}")


if __name__ == "__main__":
    main()
