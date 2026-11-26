#!/usr/bin/env python3.12
"""Put REAL corpus text to Laya and check it against controls.

Laya is a System-1 decision model: text + typed questions in, calibrated probabilities out, no
generation.  It cannot do the definitions diff or the citation count - it can only answer a typed
question about a passage.  So the honest test is whether it separates the real case from a control
on the two jobs Harry wants a second reader for.

Batch of 4 items, 2 questions each, one call.  Both questions carry a control that must go the
other way, otherwise a model that always says "true" would look correct.
"""
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYA = os.environ.get("LAYA_URL", "http://127.0.0.1:11436/classify_batch")


def first_chunk(path: Path, needle: str, words: int = 190) -> str:
    """A ~190-word window around `needle` (the service's window is 512 tokens)."""
    text = path.read_text(errors="replace")
    i = text.find(needle)
    if i < 0:
        raise SystemExit(f"needle not found in {path}")
    start = max(0, i - words * 3)
    return " ".join(text[start:start + words * 6].split())[:1100]


def find(pat: str) -> Path:
    hits = sorted(ROOT.glob(pat))
    if not hits:
        raise SystemExit(f"no file matched {pat}")
    return hits[0]


def find_section(cites_81: bool, skip: str = "") -> Path:
    """A section that refers to s 8-1, and one that does not (the control)."""
    for p in sorted(ROOT.glob("data/itaa-1997/sections/**/*.md")):
        if skip and skip in str(p):
            continue
        t = p.read_text(errors="replace")
        if len(t) < 1200 or "Dictionary" in t[:200]:
            continue
        has = "section 8-1" in t
        if has == cites_81 and p.name != "8-1.md" and p.name != "995-1.md":
            return p
    raise SystemExit("no section matched")


items = [
    {"id": "DEF_real", "text": first_chunk(find("data/itaa-1997/sections/**/995-1.md"), "means")},
    {"id": "DEF_control", "text": first_chunk(find("data/itaa-1997/sections/**/8-1.md"), "general deduction")},
    {"id": "CITE_real", "text": first_chunk(find_section(True), "section 8-1")},
    {"id": "CITE_control", "text": first_chunk(find_section(False), "the")},
]
for it in items:
    print(f"  {it['id']:14s} <- {it.pop('_src', '')}")

questions = {
    "defines_term": {
        "type": "noul",
        "instructions": "The passage states the legal definition or meaning of a defined term, "
                        "using wording such as 'means', 'includes' or 'has the meaning'.",
    },
    "cites_s81": {
        "type": "noul",
        "instructions": "The passage cites or refers to section 8-1 of the Income Tax "
                        "Assessment Act 1997, or to the general deduction provision.",
    },
}

print(f"  sending {len(items)} items to Laya ...")
req = urllib.request.Request(
    LAYA, data=json.dumps({"items": items, "questions": questions}).encode(),
    headers={"Content-Type": "application/json"})
out = json.loads(urllib.request.urlopen(req, timeout=300).read())
print(f"  round trip: {out.get('ms')} ms for {len(items)} items")
print()
for r in out["results"]:
    a = r["answers"]
    print(f"  {r['id']:14s} defines_term={a['defines_term']['noul']:.3f} "
          f"(conf {a['defines_term']['confidence']:.2f})   "
          f"cites_s81={a['cites_s81']['noul']:.3f} (conf {a['cites_s81']['confidence']:.2f})")
print()
print("  expectation: DEF_real and CITE_real high, DEF_control and CITE_control low.")
