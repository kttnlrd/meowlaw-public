"""Audit a run: random word joins with their frequency, removed lines, and why repairs were refused."""
import collections
import json
import random
import sys

run = sys.argv[1]
M = [json.loads(l) for l in open(f"{run}/manifest.jsonl")]
print("status:", collections.Counter(m["status"] for m in M))
print("refused because:", collections.Counter(m.get("reason") for m in M if m["status"] == "rejected").most_common(6))
joins = collections.Counter(j for m in M if m["status"] == "repaired" for j in m.get("joins", []))
print(f"distinct joins: {len(joins)}, total: {sum(joins.values())}")
print("most frequent joins:")
for j, n in joins.most_common(40):
    print(f"  {n:5}  {j}")
rnd = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
print("random joins:")
for j in rnd.sample(list(joins), min(40, len(joins))):
    print("   ", j)
rm = collections.Counter(r for m in M if m["status"] == "repaired" for r in m.get("removed", []))
print("removed lines:")
for r, n in rm.most_common(25):
    print(f"  {n:5}  {r[:100]}")
rej = [m for m in M if m["status"] == "rejected"][:6]
for m in rej:
    print("REFUSED", m["id"], m.get("reason"), str(m.get("diff") or "")[:160], m.get("joins", [])[:3])
