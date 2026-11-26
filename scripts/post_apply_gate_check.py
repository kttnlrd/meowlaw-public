#!/usr/bin/env python3.12
"""After applying, gate the APPLIED corpus files themselves, with and without the credit.

The scratch-file proof (verify_operator_credit.py) shows the proposal is applicable.  This shows the
bytes that are now in the corpus pass the gate - and that the same bytes still fail it without the
credit, so the credit is what is carrying them, not a loosened gate.
"""
from __future__ import annotations
import os

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROPOSAL = Path("/tmp/opres2/proposal.json")
RECORD = Path("/tmp/apply-full/operator_apply_record.json")
sys.path.insert(0, str(ROOT / "scripts"))

import ingest_reingest_gate as G  # noqa: E402

STAGING = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data"))


def resolve_pdf(act: str, source: str) -> Path | None:
    nn = source[3:5] if source.startswith("vol") else ""
    for cand in (STAGING / act / "raw" / "comp266" / source,
                 ROOT / "source" / act / source,
                 ROOT / "source" / act / f"C2026C00324VOL{nn}.pdf"):
        if cand.exists():
            return cand
    return None


def main() -> int:
    todo = json.loads(RECORD.read_text())["todo"]
    by_section: dict[str, dict] = {}
    for t in todo:
        by_section.setdefault(t["section"], t)

    doc = json.loads(PROPOSAL.read_text())
    fence_src = {f["section"]: f for f in doc["fences"] if f.get("status") == "change-needed"}

    results = []
    for section, item in sorted(by_section.items()):
        fence = fence_src[section]
        act = item["file"].split("/")[1]
        pdf = resolve_pdf(act, fence["source"])
        path = ROOT / item["file"]
        text = path.read_text(encoding="utf-8")
        if pdf is None:
            results.append({"section": section, "status": "no-pdf"})
            continue
        _d, band = G.open_band(pdf, section)
        if band is None:
            results.append({"section": section, "status": "not-located"})
            continue
        G.load_operator_evidence(PROPOSAL)
        with_credit = G.gate_ingested(act, section, text, band)
        G.OPERATOR_EVIDENCE.clear()
        without = G.gate_ingested(act, section, text, band)
        G.OPERATOR_EVIDENCE.clear()
        ok = with_credit["verdict"] == "ACCEPTED" and without["verdict"] == "REJECTED"
        results.append({"section": section, "act": act, "with_credit": with_credit["verdict"],
                        "without_credit": without["verdict"], "ok": ok,
                        "fail_with": [k for k, v in with_credit["gates"].items() if not v["pass"]]})
        print(f"  {'PASS' if ok else 'FAIL'} {act} {section:10s} "
              f"applied+credit={with_credit['verdict']:9s} applied-no-credit={without['verdict']}"
              + ("" if ok else f"  fail_with={results[-1]['fail_with']}"))

    out = Path("/tmp/apply-full/post_apply_gate.json")
    out.write_text(json.dumps(results, indent=1))
    passed = sum(1 for r in results if r.get("ok"))
    print(f"\n{passed}/{len(results)} applied section(s): ACCEPTED with the credit, REJECTED without")
    print(f"detail: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
