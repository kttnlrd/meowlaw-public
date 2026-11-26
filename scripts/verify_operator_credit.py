#!/usr/bin/env python3.12
"""Prove the operator credit on REAL corpus sections, in-process, before anything is written.

For every section with a change-needed fence it builds the proposal's text into a scratch directory
(never the corpus) and gates it three ways:

  A. proposed text + evidence   -> must be ACCEPTED   (the credit is spendable)
  B. proposed text, no evidence -> must be REJECTED   (R2: the operator would be an invention)
  C. current text  + evidence   -> must be REJECTED   (R1: the page demands the operator)

If A is not ACCEPTED the proposal is not applicable to that section.  If B or C is ACCEPTED the
credit proves nothing about it - either the operator was never missing, or a gap is being tolerated.

The bands are opened through the gate's own cached index, so each volume is parsed once instead of
once per invocation: same verdicts, minutes instead of hours.
"""
from __future__ import annotations
import os

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/gate-test")
PROPOSAL = Path("/tmp/opres2/proposal.json")
sys.path.insert(0, str(ROOT / "scripts"))

import ingest_reingest_gate as G  # noqa: E402


def resolve_pdf(act: str, source: str) -> Path | None:
    nn = source[3:5] if source.startswith("vol") else ""
    for cand in (Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data")) / act / "raw" / "comp266" / source,
                 ROOT / "source" / act / source,
                 ROOT / "source" / act / f"C2026C00324VOL{nn}.pdf"):
        if cand.exists():
            return cand
    return None


def main() -> int:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    doc = json.loads(PROPOSAL.read_text())
    by_section: dict[str, list[dict]] = {}
    for f in doc["fences"]:
        if f.get("status") == "change-needed":
            by_section.setdefault(f["section"], []).append(f)

    only = set(sys.argv[1:]) or None
    results: list[dict] = []
    for section, fences in sorted(by_section.items()):
        if only and section not in only:
            continue
        src = Path(fences[0]["file"])
        if not src.is_absolute():
            src = ROOT / src
        act = src.parts[src.parts.index("data") + 1]
        pdf = resolve_pdf(act, fences[0]["source"])
        if pdf is None:
            results.append({"section": section, "status": "no-pdf"})
            print(f"  [SKIP] {act} {section}: {fences[0]['source']} not found")
            continue

        current_text = src.read_text(encoding="utf-8")
        lines = current_text.splitlines(keepends=True)
        applied = 0
        for fence in fences:
            for ln in fence.get("lines", []):
                if ln.get("status") != "change-needed" or ln.get("proposed") == ln.get("current"):
                    continue
                for i, line in enumerate(lines):
                    if line.rstrip("\n") == ln["current"]:
                        lines[i] = ln["proposed"] + "\n"
                        applied += 1
                        break
        proposed_text = "".join(lines)
        out = SCRATCH / act / f"{section}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(proposed_text, encoding="utf-8")

        _doc, band = G.open_band(pdf, section)
        if band is None:
            results.append({"section": section, "status": "not-located", "pdf": pdf.name})
            print(f"  [SKIP] {act} {section}: not located in {pdf.name}")
            continue

        G.load_operator_evidence(PROPOSAL)                 # A and C: credited
        repA = G.gate_ingested(act, section, proposed_text, band)
        credited = dict(repA.get("operator_credit") or {})
        G.OPERATOR_EVIDENCE.clear()                        # B: uncredited
        repB = G.gate_ingested(act, section, proposed_text, band)
        G.load_operator_evidence(PROPOSAL)                 # C
        repC = G.gate_ingested(act, section, current_text, band)
        G.OPERATOR_EVIDENCE.clear()

        def fails(rep):
            return [k for k, v in rep["gates"].items() if not v["pass"]]

        rec = {"section": section, "act": act, "lines": applied, "credit": credited,
               "A": repA["verdict"], "B": repB["verdict"], "C": repC["verdict"],
               "failA": fails(repA), "failB": fails(repB), "failC": fails(repC)}
        rec["ok"] = rec["A"] == "ACCEPTED" and rec["B"] == "REJECTED" and rec["C"] == "REJECTED"
        results.append(rec)
        print(f"  {'PASS' if rec['ok'] else 'FAIL'} {act} {section:10s} lines={applied} "
              f"credit={credited} A={rec['A']} B={rec['B']} C={rec['C']}"
              + ("" if rec["ok"] else f"  failA={rec['failA']} failB={rec['failB']} failC={rec['failC']}"))

    outjson = Path("/tmp/apply-full/credit_verification.json")
    outjson.write_text(json.dumps(results, indent=1))
    ok = [r for r in results if r.get("ok")]
    print()
    print(f"{len(ok)}/{len(results)} sections proven  (A accepted, B rejected, C rejected)")
    print(f"detail: {outjson}")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
