#!/usr/bin/env python3.12
"""C18 / E-d at the parser: parse_nz_it must ignore endnote parts and strip U+FEFF.

The plant is a miniature of the real 31 MB document: a consolidated `div.part` (lettered, the
Act), then the same three containers the endnote copies of the amending Acts live in
(`div.end > div.skeletons > div.skeleton-act`, `div.schedule-amendments`, `div.amend`), each
carrying its own `div.part` with the same ids the consolidated Act does not use ("1", "2", "3B")
- which is how 445 amendment-history sections and the TAA 1994 text were served as law from
`data/nz-it-2007/` (CDN-0173). A U+FEFF between subsection brackets exercises E-d.

`PARSE_NZ_MODULE` points the check at another copy of the parser so it can be shown failing
against HEAD (`git show HEAD:pipeline/parse_nz_it.py > /tmp/parse_nz_it_head.py`); without it the
working tree's parser is used.

Run: /usr/bin/python3.12 scripts/test_c18_parse_nz_it.py      (exit 1 on any failure)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOM = "\ufeff"

ACT_HTML = f"""<html><body>
<div class="part">
  <h2 class="part"><span class="label">Part A</span> Purpose and interpretation</h2>
  <div class="subpart">
    <h3 class="subpart"><span class="label">Subpart AA</span>&mdash;Purpose and interpretation</h3>
    <div class="prov">
      <h5 class="prov"><span class="label">AA 1</span> Purpose of Act</h5>
      <div class="prov-body">
        <div class="subprov">
          <p class="subprov"><span class="label">(1)</span></p>
          <div class="para">The main purposes are{BOM}(a) to define income and{BOM}(b) to impose tax.</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="end">
 <div class="skeletons">
  <div class="skeleton-act">
   <h2 class="skeleton-act">Income Tax Amendment Act 2019</h2>
   <div class="part">
     <h2 class="part"><span class="label">Part 1</span> Amendments to principal Act</h2>
     <div class="prov">
       <h5 class="prov"><span class="label">3</span> Application</h5>
       <div class="prov-body"><div class="subprov">
         <p class="subprov"><span class="label">(1)</span></p>
         <div class="para">This section amends the principal Act.</div>
       </div></div>
     </div>
   </div>
  </div>
 </div>
</div>

<div class="schedule-amendments">
  <div class="part">
    <h2 class="part"><span class="label">Part 3B</span> Tax Administration Act 1994</h2>
    <div class="prov">
      <h5 class="prov"><span class="label">RA 1</span> What this Part does</h5>
      <div class="prov-body"><div class="subprov">
        <p class="subprov"><span class="label">(1)</span></p>
        <div class="para">The TAA text, schedule-amendments container.</div>
      </div></div>
    </div>
  </div>
  <div class="amend">
    <div class="part">
      <h2 class="part"><span class="label">Part 2</span> Amendments</h2>
      <div class="prov">
        <h5 class="prov"><span class="label">9</span> Section replaced</h5>
        <div class="prov-body"><div class="subprov">
          <p class="subprov"><span class="label">(1)</span></p>
          <div class="para">Section 130 replaced using the TAA text.</div>
        </div></div>
      </div>
    </div>
  </div>
</div>
</body></html>
"""

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def load_parser():
    override = os.environ.get("PARSE_NZ_MODULE")
    path = Path(override) if override else ROOT / "pipeline" / "parse_nz_it.py"
    spec = importlib.util.spec_from_file_location("parse_nz_it_under_test", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load parser from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def main() -> int:
    mod, path = load_parser()
    print(f"parser under test: {path}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        html = td / "whole.html"
        html.write_text(ACT_HTML, encoding="utf-8")
        out = td / "sections"
        mod.parse_nz_income_tax(html, out, 935, "2026-06-06")

        written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))
        print(f"  written: {written}")

        check("the consolidated Part A section is written",
              "part-A/division-AA/AA-1.md" in written, str(written))
        check("no div.end / skeletons.amending part is written",
              not any(w.startswith("part-1/") for w in written), str(written))
        check("no schedule-amendments part is written",
              not any(w.startswith("part-3B/") for w in written), str(written))
        check("no div.amend part is written",
              not any(w.startswith("part-2/") for w in written), str(written))
        check("exactly one section file is written", written == ["part-A/division-AA/AA-1.md"],
              str(written))

        # E-d: the BOM between subsection brackets is gone, and the brackets stay adjacent.
        text = (out / "part-A/division-AA/AA-1.md").read_text(encoding="utf-8")
        check("no U+FEFF in the served text", BOM not in text,
              repr([l for l in text.splitlines() if BOM in l][:2]))
        check("the bracket pair is still adjacent: '(a) to define' survives with no BOM",
              "(a) to define income" in text and "(b) to impose tax" in text,
              repr([l for l in text.splitlines() if "to define" in l]))

    print("PASS" if not failures else f"{len(failures)} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
