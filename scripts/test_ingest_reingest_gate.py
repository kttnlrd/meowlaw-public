#!/usr/bin/env python3
"""Tests for scripts/ingest_reingest_gate.py.  Stdlib unittest — no pytest here.

Every fixture is produced by running the real ingester over the real
compilation-266 PDF into a TEMP tree, then mutated in memory.  Nothing is read
from or written to data/.

    /usr/bin/python3.12 scripts/test_ingest_reingest_gate.py
"""
from __future__ import annotations
import os

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ingest_itaa_compilation as INGEST   # noqa: E402
import ingest_reingest_gate as G           # noqa: E402

PDF_DIR = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data", "itaa-1997", "raw", "comp266"))
VOL = PDF_DIR / "vol03.pdf"
# 82-150 is the CDN-0193 corruption section: its legal formula shipped as a
# fake pipe table.  70-10 is an ordinary prose section on one page.
FORMULA_SECTION = "82-150"
CLEAN_SECTION = "70-10"
# a multi-page section, so the gate's furniture drop-list actually fires
MULTIPAGE_SECTION = "82-10"


@unittest.skipUnless(VOL.is_file(), f"{VOL} not present")
class ReingestGateTest(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "ingested"
        report = INGEST.run(SimpleNamespace(
            act="itaa-1997", act_name="ITAA 1997", comp=266, comp_date=None,
            pdf_dir=str(PDF_DIR), volume=VOL.name, out=str(out),
            sections=f"{FORMULA_SECTION},{CLEAN_SECTION},{MULTIPAGE_SECTION}",
            limit=None))
        cls.text = {}
        for r in report["results"]:
            cls.text[r["section"]] = (out / r["path"]).read_text(encoding="utf-8")
        cls.band = {s: G.open_band(VOL, s)[1] for s in cls.text}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def gate(self, section: str, text: str | None = None) -> dict:
        return G.gate_ingested("itaa-1997", section,
                               self.text[section] if text is None else text,
                               self.band[section])

    def assert_rejected_on(self, rep: dict, gate: str, needle: str = "") -> None:
        self.assertEqual(rep["verdict"], "REJECTED",
                         msg=f"expected REJECTED, gates: "
                             f"{[(k, g['pass']) for k, g in rep['gates'].items()]}")
        self.assertFalse(rep["gates"][gate]["pass"], msg=f"{gate} passed: {rep['gates'][gate]}")
        if needle:
            self.assertIn(needle, rep["gates"][gate]["reason"])

    # 1 ────────────────────────────────────────────────────────────────────
    def test_correct_reingest_is_accepted_and_needs_review(self):
        rep = self.gate(FORMULA_SECTION)
        self.assertEqual(rep["verdict"], "ACCEPTED",
                         msg=str({k: g["reason"] for k, g in rep["gates"].items()
                                  if not g["pass"]}))
        self.assertTrue(rep["needs_review"])
        for name, g in rep["gates"].items():
            self.assertTrue(g["pass"], msg=f"{name}: {g['reason']}")
        units = rep["gates"]["R6_multi_table_multi_region"]["units"]
        self.assertEqual([u["kind"] for u in units], ["region:formula"])
        self.assertEqual(units[0]["pages"], [69])

    # 2 ────────────────────────────────────────────────────────────────────
    def test_deleted_prose_token_is_rejected_on_r1(self):
        text = self.text[FORMULA_SECTION].replace(
            "suffered from ill-health", "suffered from", 1)
        rep = self.gate(FORMULA_SECTION, text)
        self.assert_rejected_on(rep, "R1_pdf_token_recall", "ill-health")
        self.assertIn("ill-health", rep["gates"]["R1_pdf_token_recall"]["missing"])

    # 3 ────────────────────────────────────────────────────────────────────
    def test_invented_word_is_rejected_on_r2(self):
        text = self.text[FORMULA_SECTION].replace(
            "the payment was made", "the fabricated payment was made", 1)
        rep = self.gate(FORMULA_SECTION, text)
        self.assert_rejected_on(rep, "R2_no_invention", "fabricated")

    def test_invented_empty_header_cell_is_rejected(self):
        """table_rebuild_gate's G1 caught a fabricated empty 'Column 2' once."""
        text = self.text[FORMULA_SECTION].replace(
            "where: days to retirement",
            "| Item | Column 2 |\n| --- | --- |\n| 1 | a |\n| 2 | b |\n\n"
            "where: days to retirement", 1)
        rep = self.gate(FORMULA_SECTION, text)
        self.assert_rejected_on(rep, "R2_no_invention", "Column")

    # 4 ────────────────────────────────────────────────────────────────────
    def test_formula_rewritten_as_pipe_table_is_rejected(self):
        """The CDN-0193 bug itself: the formula emitted as a fake table."""
        src = self.text[FORMULA_SECTION]
        start = src.index("```ingest-formula")
        end = src.index("```", src.index("\n", start)) + 3
        body = [l for l in src[start:end].splitlines()[1:-1] if l.strip()]
        fake = ["| " + " | ".join(body[:3]) + " |",
                "| --- | --- | --- |"]
        fake += ["| " + " | ".join(body[i:i + 3]) + " |"
                 for i in range(3, len(body) - 2, 3)]
        text = src[:start] + "\n".join(fake) + src[end:]
        rep = self.gate(FORMULA_SECTION, text)
        self.assertEqual(rep["verdict"], "REJECTED")
        failed = [k for k, g in rep["gates"].items() if not g["pass"]]
        self.assertIn("R3_table_wellformedness", failed)
        self.assertIn("R5_coherence", failed)
        self.assertIn("(G5)", rep["gates"]["R3_table_wellformedness"]["reason"])

    # 5 ────────────────────────────────────────────────────────────────────
    def test_preserved_region_without_marker_is_rejected_on_r4(self):
        text = self.text[FORMULA_SECTION].replace(
            "```ingest-formula source=vol03.pdf page=69", "```", 1)
        rep = self.gate(FORMULA_SECTION, text)
        self.assert_rejected_on(rep, "R4_preserved_region_accounting", "without an 'ingest-")

    def test_preserved_region_on_the_wrong_page_is_rejected_on_r4(self):
        text = self.text[FORMULA_SECTION].replace("page=69", "page=70", 1)
        rep = self.gate(FORMULA_SECTION, text)
        self.assert_rejected_on(rep, "R4_preserved_region_accounting")

    # 6 ────────────────────────────────────────────────────────────────────
    def test_clean_section_is_accepted_without_review(self):
        rep = self.gate(CLEAN_SECTION)
        self.assertEqual(rep["verdict"], "ACCEPTED",
                         msg=str({k: g["reason"] for k, g in rep["gates"].items()
                                  if not g["pass"]}))
        self.assertFalse(rep["needs_review"])
        self.assertEqual(rep["gates"]["R6_multi_table_multi_region"]["units"], [])

    # the drop-list is a floor, so prove it is enumerated and applied ──────
    def test_drop_list_is_reported(self):
        rep = self.gate(MULTIPAGE_SECTION)
        self.assertEqual(rep["verdict"], "ACCEPTED",
                         msg=str({k: g["reason"] for k, g in rep["gates"].items()
                                  if not g["pass"]}))
        applied = rep["gates"]["R1_pdf_token_recall"]["drop_list_applied"]
        self.assertTrue(set(applied) <= {n for n, _, _ in G.DROP_PATS}
                        | {"wrapped_furniture_line"},
                        msg=f"undeclared drop reason in {applied}")
        for expect in ("compilation_no", "authorised_version", "page_number",
                       "running_header_section", "asterisked_definitions"):
            self.assertIn(expect, applied)

    def test_allow_list_is_reported(self):
        rep = self.gate(FORMULA_SECTION)
        self.assertEqual(rep["gates"]["R2_no_invention"]["allow_list"], G.ALLOW_LIST)


class SelfcheckTest(unittest.TestCase):
    def test_selfcheck_passes(self):
        self.assertEqual(G.selfcheck(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
