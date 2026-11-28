#!/usr/bin/env python3.12
"""Prove the Laya window is LIVE (mechanical) and that it HELPS (behavioural), in one run.

Laya truncates silently, so "max_len is set" proves nothing about what reached the forward pass.
Two proofs, as the skill requires:

  mechanical - tokenize without truncation, then show the sequence the model actually receives
               under each config.
  behavioural - one item whose evidence sits PAST the old 512 cut, and a control whose evidence
               sits INSIDE it and must not move.  Same run, both configs: cross-run comparisons
               understate the difference because baseline timings vary between runs.
"""
import json
import time
from pathlib import Path

import laya
from laya.common import build_sequence

SNAP = sorted(Path.home().glob(
    ".cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*"))[0]
CFG_512 = str(SNAP)
CFG_1024 = "/home/harri/models/laya-en-1024"

QUESTIONS = {"refs_81": {
    "type": "noul",
    "instructions": "The passage refers to section 8-1 of the Income Tax Assessment Act 1997, "
                    "or to the general deduction provision.", }}

base = Path("/home/harri/laya_ab_base.txt").read_text()
TARGET = ("For the avoidance of doubt, the operation of this Subdivision is subject to "
          "section 8-1 of the Income Tax Assessment Act 1997.\n\n")

# ~850 tokens of evidence, then the target sentence: past the 512 cut, inside 1024.
deep = base[:3400] + "\n\n" + TARGET
# the control: same sentence near the start (~300 tokens) - inside the old window either way.
control = base[:1150] + "\n\n" + TARGET + base[1150:2250] + "\n"

items = [{"id": "evidence_past_512", "text": deep},
         {"id": "control_inside_512", "text": control}]

print("=== MECHANICAL ===")
agents = {}
for label, path in (("512", CFG_512), ("1024", CFG_1024)):
    a = laya.load(path, device="cpu")
    agents[label] = a
    print(f"  cfg max_len={a.cfg.get('max_len')}  ({label})")

tok = agents["1024"].tok
ids = tok(deep, truncation=False)["input_ids"]
print(f"  the deep item tokenizes to {len(ids)} tokens (untruncated)")
for label in ("512", "1024"):
    a = agents[label]
    q = {"t": "noul", "ins": QUESTIONS["refs_81"]["instructions"], "crit": None}
    seq, markers = build_sequence(a.tok, deep, q, a.cfg.get("max_len", 512),
                                  a.cfg.get("head_max_len", 192))
    print(f"  {label:>4}: sequence the model receives = {len(seq)} tokens "
          f"(of {len(ids)} available)")

print("=== BEHAVIOURAL (same run, both configs) ===")
results = {}
for label in ("512", "1024"):
    a = agents[label]
    per = {}
    for it in items:
        t1 = time.time()
        ans = a.predict({"text": it["text"]}, QUESTIONS)
        ans = ans.get("answers", ans)
        per[it["id"]] = (ans["refs_81"]["noul"], round((time.time() - t1) * 1000))
    results[label] = per
    print(f"  {label}: " + "  ".join(f"{k}={v[0]:.3f} ({v[1]}ms)" for k, v in per.items()))

d512 = results["512"]["evidence_past_512"][0]
d1024 = results["1024"]["evidence_past_512"][0]
c512 = results["512"]["control_inside_512"][0]
c1024 = results["1024"]["control_inside_512"][0]
print()
print(f"  deep evidence  512 -> 1024: {d512:.3f} -> {d1024:.3f}  ({'toward' if d1024 > d512 else 'away from'} the target)")
print(f"  control        512 -> 1024: {c512:.3f} -> {c1024:.3f}  (must not move)")
print(f"  cost per item: 512={results['512']['evidence_past_512'][1]}ms  "
      f"1024={results['1024']['evidence_past_512'][1]}ms")
Path("/tmp/laya_ab_result.json").write_text(json.dumps(results, indent=1))
