#!/usr/bin/env python3.12
"""Why do 104-95, 118-185, 296-60, 83-170 and 86-25 fail the gate?

They fail R1/R2/R4/R6 with the operators credited, without them, and on the untouched current
text, which says the disagreement is structural - a declared region the PDF does not corroborate.
This prints the actual token difference rather than the verdict.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ingest_reingest_gate as G  # noqa: E402

SECTIONS = ["118-185", "86-25", "83-170", "296-60", "104-95"]


def resolve_pdf(act: str, source: str) -> Path | None:
    nn = source[3:5] if source.startswith("vol") else ""
    for c in (Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data")) / act / "raw" / "comp266" / source,
              ROOT / "source" / act / source,
              ROOT / "source" / act / f"C2026C00324VOL{nn}.pdf"):
        if c.exists():
            return c
    return None


for sec in SECTIONS:
    f = next((ROOT / "data" / "itaa-1997" / "sections").rglob(f"{sec}.md"))
    text = f.read_text(encoding="utf-8")
    m = re.search(r'source_pdf: "([^"]+)"', text)
    pdf = resolve_pdf("itaa-1997", m.group(1)) if m else None
    print(f"=== {sec}   {f.relative_to(ROOT)}   pdf={pdf.name if pdf else 'MISSING'}")
    if pdf is None:
        continue
    opened = G.open_band(pdf, sec)
    if not opened or opened[1] is None:
        print("    could not open a band for this section")
        continue
    doc, band = opened
    parsed = G.parse_output(text)
    got = G.output_tokens(parsed)
    r1 = G.r1_pdf_token_recall(band, got)
    r2 = G.r2_no_invention(band, got)
    r4 = G.r4_preserved_regions(parsed, band)
    r6 = G.r6_multi_unit(parsed, band)
    print(f"    band={r1['pdf_tokens']} tokens  output={r1['output_tokens']} tokens")
    print(f"    R1 missing  : {r1['missing'][:24]}")
    print(f"    R2 invented : {r2['invented'][:24]}")
    print(f"    R4 {r4['reason'][:160]}")
    print(f"    R6 {r6['reason'][:160]}")
    print(f"    declared regions: {[r.get('kind') or r.get('name') for r in (parsed.get('fences') or [])][:8]}")
    print()
