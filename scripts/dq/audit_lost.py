"""Show what the language model chose for each lost character (U+FFFD) in a run's repaired files."""
import json
import random
import sys
from pathlib import Path

run = Path(sys.argv[1])
docs = {json.loads(l)["id"]: json.loads(l) for l in open(run / "docs.jsonl", encoding="utf-8")}
M = [json.loads(l) for l in open(run / "manifest.jsonl", encoding="utf-8")]
picked = []
for m in M:
    if m["status"] != "repaired" or "lost_character" not in m.get("fixes", []):
        continue
    d = docs[m["id"]]
    a = Path(d["abs"]).read_text(encoding="utf-8", errors="replace")
    b = (run / "files" / d["rel"]).read_text(encoding="utf-8")
    # walk both texts in step: they differ only where a U+FFFD became one character
    i = j = 0
    while i < len(a) and j < len(b) and len(picked) < 4000:
        if a[i] == "�":
            picked.append((m["id"], a[max(0, i - 25):i], b[j], a[i + 1:i + 25]))
        i += 1
        j += 1
from collections import Counter
print("chosen:", Counter(p[2] for p in picked).most_common())
for p in random.Random(4).sample(picked, min(40, len(picked))):
    print(f"  {p[1]!r:>30} [{p[2]}] {p[3]!r}")
