"""Build data/private_rulings_index.json from the private ruling corpus.

Index shape: {authnum: {name, date_of_advice, year}} — feeds the
/api/private-rulings/tree and /api/private-rulings list endpoints.
Run after backfill_private_ruling_dates.py so years are populated.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

RULINGS_DIR = os.environ.get(
    "PRIVATE_RULINGS_ROOT", os.path.join(os.path.expanduser(os.environ.get("LAWKITTY_HOME", "~/.lawkitty")), "private_rulings"))
JSON_DIR = os.path.join(RULINGS_DIR, "data", "json")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "data", "private_rulings_index.json")


def main() -> None:
    files = sorted(glob.glob(os.path.join(JSON_DIR, "*.json")))
    index: dict[str, dict] = {}
    t0 = time.time()
    for i, f in enumerate(files):
        auth = os.path.splitext(os.path.basename(f))[0]
        try:
            d = json.load(open(f))
        except (json.JSONDecodeError, OSError):
            continue
        date_str = (d.get("date_of_advice") or "").strip()
        year = None
        if len(date_str) >= 4 and date_str[:4].isdigit():
            year = int(date_str[:4])
        index[auth] = {
            "name": d.get("name") or d.get("subject") or "",
            "date_of_advice": date_str,
            "year": year,
        }
        if (i + 1) % 10000 == 0:
            print(f"{i+1}/{len(files)} in {time.time()-t0:.0f}s", flush=True)

    with open(OUTPUT, "w") as f:
        json.dump(index, f, indent=1)
    dated = sum(1 for m in index.values() if m["year"])
    print(f"DONE {len(index)} rulings, {dated} dated ({dated/len(index)*100:.0f}%) "
          f"-> {OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
