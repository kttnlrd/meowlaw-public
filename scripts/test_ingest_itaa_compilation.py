#!/usr/bin/env python3
"""Tests for scripts/ingest_itaa_compilation.py  (stdlib unittest; no pytest here).

Run:  /usr/bin/python3.12 scripts/test_ingest_itaa_compilation.py

Every fixture is either the real comp-266 PDF (read-only) or a temp dir.
Nothing under data/ is read as a fixture and nothing is written there.

The cases are the failure modes CDN-0193 is actually made of:
  1. the real 82-150 formula region      -> no pipe table, no lost tokens, the
                                            'where:' prose not glued to it
  2. a genuine well-formed table         -> pipe table, separator, gate G1 passes
  3. a glyph-bearing rendered formula    -> glyphs never reach table syntax and
                                            the section is flagged, not shipped
  4. determinism                         -> two runs byte-identical
  5. footer compilation != --comp        -> ABORT
"""
from __future__ import annotations
import os

import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ingest_itaa_compilation as IG          # noqa: E402
import table_rebuild_gate as GATE             # noqa: E402

PDF_DIR = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data", "itaa-1997", "raw", "comp266"))
VOL = "vol03.pdf"
COMP, COMP_DATE = 266, "2026-07-01"
# 82-150  the formula that shipped as a 3-cell pipe block (the CDN-0193 signature)
# 70-45   a real table that rebuilds cleanly (gate G1 passes on it)
# 104-245 a rendered formula carrying the G5 glyph set
SECTIONS = "82-150,70-45,104-245"


def args(out: Path, sections=SECTIONS, comp=COMP, volume=VOL) -> Namespace:
    return Namespace(act="itaa-1997", act_name="ITAA 1997", comp=comp,
                     comp_date=COMP_DATE, pdf_dir=str(PDF_DIR), out=str(out),
                     sections=sections, volume=volume, limit=None,
                     report=None, stage=False)


def pipe_blocks(text: str) -> list[list[str]]:
    blocks, cur = [], []
    for ln in text.splitlines():
        if ln.startswith("|"):
            cur.append(ln)
        elif cur:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    return blocks


@unittest.skipUnless(PDF_DIR.is_dir(), f"comp-266 PDFs not present at {PDF_DIR}")
class IngestRealPDF(unittest.TestCase):
    """One ingest run shared by the cases; each asserts on its own section."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ingest-test-"))
        cls.report = IG.run(args(cls.tmp / "run1"))
        cls.by = {r["section"]: r for r in cls.report["results"]}
        cls.text = {s: (cls.tmp / "run1" / r["path"]).read_text()
                    for s, r in cls.by.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ── 1. the 82-150 formula region ───────────────────────────────────────
    def test_formula_region_keeps_every_token_and_is_not_a_table(self):
        r, text = self.by["82-150"], self.text["82-150"]
        self.assertEqual(r["unrepresented_count"], 0, r["unrepresented"])

        # every token the PDF put in the formula is in the output as text
        for tok in ("Days", "to", "retirement", "Amount", "of", "employment",
                    "termination", "payment", "Employment", "days"):
            self.assertIn(tok, text, f"formula token {tok!r} lost")
        self.assertIn("´", text, "the formula's multiplication glyph was dropped")

        # ... and NOT as a pipe table
        self.assertEqual(pipe_blocks(text), [],
                         "the formula region was emitted as pipe syntax again")
        self.assertIn("```ingest-formula", text)

        # the 'where:' definitions must not be glued into the formula region
        fence = text.split("```ingest-formula")[1].split("```")[0]
        self.assertNotIn("where:", fence)
        self.assertNotIn("days to retirement is the number of days", fence)
        where = text.split("```", 2)[2]
        self.assertIn("where: days to retirement is the number of days", where)
        self.assertEqual(r["status"], "NEEDS_REVIEW")
        self.assertEqual([p["kind"] for p in r["preserved_regions"]], ["formula"])

    # ── 2. a genuine table ─────────────────────────────────────────────────
    def test_real_table_is_a_well_formed_pipe_table_passing_G1(self):
        r, text = self.by["70-45"], self.text["70-45"]
        self.assertEqual(r["unrepresented_count"], 0)
        self.assertEqual(r["tables"], 1)
        blocks = pipe_blocks(text)
        self.assertEqual(len(blocks), 1)
        rows = blocks[0]
        self.assertRegex(rows[1], r"^\|( --- \|)+$", "no header separator line")
        self.assertGreaterEqual(len(rows) - 2, 2, "fewer than 2 data rows")
        widths = {ln.count("|") for ln in rows}
        self.assertEqual(len(widths), 1, f"inconsistent cell counts: {widths}")

        # G1, from the gate, against the PDF's own table region
        table = GATE.extract_table(PDF_DIR / VOL, "70-45")
        self.assertIsNotNone(table)
        g1 = GATE.g1_token_provenance(table["region_words"], text.splitlines())
        self.assertTrue(g1["pass"], g1["reason"])
        self.assertTrue(GATE.g5_glyph_ban(text.splitlines())["pass"])

    # ── 3. a rendered formula with glyphs ──────────────────────────────────
    def test_formula_glyphs_never_reach_table_syntax(self):
        r, text = self.by["104-245"], self.text["104-245"]
        self.assertEqual(r["unrepresented_count"], 0)
        glyphy = [ln for ln in text.splitlines()
                  if ln.startswith("|") and IG._glyph_hits(ln)]
        self.assertEqual(glyphy, [], "formula glyphs ended up inside a table row")
        self.assertTrue(GATE.g5_glyph_ban(text.splitlines())["pass"])
        # flagged, not silently shipped
        self.assertEqual(r["status"], "NEEDS_REVIEW")
        self.assertTrue(any(p["kind"] == "formula" for p in r["preserved_regions"]))
        self.assertIn("```ingest-formula", text)

    # ── 4. determinism ─────────────────────────────────────────────────────
    def test_two_runs_are_byte_identical(self):
        IG.run(args(self.tmp / "run2"))
        a = sorted((self.tmp / "run1").rglob("*.md"))
        b = sorted((self.tmp / "run2").rglob("*.md"))
        self.assertEqual([p.relative_to(self.tmp / "run1") for p in a],
                         [p.relative_to(self.tmp / "run2") for p in b])
        self.assertTrue(a)
        for x, y in zip(a, b):
            self.assertEqual(x.read_bytes(), y.read_bytes(), f"{x.name} differs between runs")


@unittest.skipUnless(PDF_DIR.is_dir(), f"comp-266 PDFs not present at {PDF_DIR}")
class CompilationFooter(unittest.TestCase):
    # ── 5. footer disagrees with --comp ────────────────────────────────────
    def test_wrong_compilation_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(IG.IngestAbort) as cm:
                IG.run(args(Path(td) / "out", comp=265))
            self.assertIn("Compilation No. 266", str(cm.exception))
            self.assertIn("--comp is 265", str(cm.exception))
            self.assertFalse(list(Path(td).rglob("*.md")), "wrote output despite aborting")

    def test_stale_comp263_volumes_are_refused(self):
        stale = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "source", "itaa-1997"))
        if not stale.is_dir():
            self.skipTest("stale comp-263 tree not present")
        with tempfile.TemporaryDirectory() as td:
            a = args(Path(td) / "out", volume=None)
            a.pdf_dir = str(stale)
            with self.assertRaises(IG.IngestAbort) as cm:
                IG.run(a)
            self.assertIn("STALE", str(cm.exception))


class OutputLocation(unittest.TestCase):
    def test_out_inside_the_worktree_is_refused(self):
        with self.assertRaises(IG.IngestAbort) as cm:
            IG.run(args(IG.REPO / "data" / "scratch"))
        self.assertIn("worktree", str(cm.exception))


class PureUnits(unittest.TestCase):
    """The classifiers, without fitz."""

    def test_bullet_list_is_not_a_table(self):
        self.assertTrue(IG.PROSE_MARKER_RE.match("•"))
        self.assertTrue(IG.PROSE_MARKER_RE.match("Note 1:"))
        self.assertTrue(IG.PROSE_MARKER_RE.match("(a)"))
        self.assertIsNone(IG.PROSE_MARKER_RE.match("1"))
        self.assertIsNone(IG.PROSE_MARKER_RE.match("Item"))

    def test_superscript_asterisk_glues_to_its_term(self):
        self.assertEqual(IG._join(["being", "*", "gainfully", "employed;"]),
                         "being *gainfully employed;")

    def test_formula_band_is_sub_leading_baselines(self):
        mk = lambda ys: [{"y": y, "words": [{"t": "x", "x0": 1.0, "x1": 2.0}]} for y in ys]
        self.assertEqual(IG.formula_bands(mk([100.0, 112.7, 125.4])), [])   # prose
        self.assertEqual(IG.formula_bands(mk([100.0, 101.5, 103.0])),
                         [(100.0, 103.0)])                                  # formula

    def test_preserved_region_is_never_pipe_syntax(self):
        lines = [{"y": 1.0, "words": [{"t": "a", "x0": 1.0, "x1": 2.0}]},
                 {"y": 2.0, "words": [{"t": "´", "x0": 1.0, "x1": 2.0}]}]
        md, kind = IG.emit_preserved(lines, "vol03.pdf", 9)
        self.assertEqual(kind, "formula")
        self.assertFalse(any(l.startswith("|") for l in md))
        self.assertEqual(md[0], "```ingest-formula source=vol03.pdf page=9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
