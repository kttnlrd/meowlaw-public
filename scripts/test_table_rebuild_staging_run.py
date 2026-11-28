#!/usr/bin/env python3
"""CDN-0193 — the whole-section re-ingest staging path (`stage-run`).

The per-table path is covered by table_rebuild_staging.py --selfcheck and by
test_staging_gate_integration.py.  This file covers the path a compilation
update actually takes: an ingest_reingest_gate certificate (R* gates) standing
in for a table gate on the ~4,100 sections that hold no table at all.

Run: python3.12 scripts/test_table_rebuild_staging_run.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import table_rebuild_staging as ST  # noqa: E402

ACT = "itaa-1997"

# A table-less section: prose only, exactly what ~4,100 of the 4,649 look like.
CORPUS_MD = """---
act: "ITAA 1997"
section: "8-1"
---
# 8-1  General deductions

You can deduct from your assessable income any loss or outgoing.
"""
INGESTED_MD = """---
act: "ITAA 1997"
section: "8-1"
compilation_no: "266"
---
# 8-1  General deductions

You can deduct from your assessable income any loss or outgoing to the
extent that it is incurred in gaining or producing your assessable income.

---
*Last updated: 2026-07-01 (Compilation 266)*
"""


def gate_report(section: str, ingested: Path, verdict="ACCEPTED",
                needs_review=False) -> dict:
    return {
        "act": ACT, "section": section, "pdf_pages": "12-13",
        "verdict": verdict, "needs_review": needs_review,
        "ingested": str(ingested),
        "gates": {"R1_pdf_token_recall": {"pass": verdict == "ACCEPTED", "reason": ""},
                  "R2_no_invention": {"pass": verdict == "ACCEPTED", "reason": ""},
                  "R3_table_wellformedness": {"pass": True, "reason": ""},
                  "R4_preserved_region_accounting": {"pass": True, "reason": ""},
                  "R5_coherence": {"pass": True, "reason": ""},
                  "R6_multi_table_multi_region": {"pass": True, "reason": "", "units": []}},
    }


class StageRunTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "stage"
        self.corpus = self.tmp / "corpus" / ACT
        self.corpus.mkdir(parents=True)
        (self.corpus / "8-1.md").write_text(CORPUS_MD)
        self.ingested = self.tmp / "ingested" / "8-1.md"
        self.ingested.parent.mkdir(parents=True)
        self.ingested.write_text(INGESTED_MD)
        self.batch = self.tmp / "gate-batch.json"
        self.d = self.root / ACT / "8-1"

    def tearDown(self):
        self._td.cleanup()

    def run_stage(self, *reports) -> dict:
        self.batch.write_text(json.dumps(list(reports)))
        return ST.stage_run(ACT, self.batch, corpus_root=self.corpus, root=self.root)

    # 1. table-less section, ACCEPTED, no preserved region -> READY
    def test_tableless_accepted_is_ready(self):
        res = self.run_stage(gate_report("8-1", self.ingested))
        self.assertEqual(res["histogram"], {"READY": 1})
        self.assertEqual(ST.apply_list(self.root), [f"{ACT}/8-1"])
        for name in (*ST.ARTIFACTS, "report.json", "gate.json"):
            self.assertTrue((self.d / name).is_file(), name)
        rep = json.loads((self.d / "report.json").read_text())
        self.assertEqual(rep["gate"]["kind"], "reingest")
        self.assertEqual(rep["gate"]["sha256"], ST._sha(self.d / "gate.json"))
        for name in ST.ARTIFACTS:  # artifacts are hash-pinned
            self.assertEqual(rep["artifacts"][name]["sha256"], ST._sha(self.d / name))
        self.assertNotIn(str(ST.REPO), str(self.d))  # outside the worktree

    # 2. needs_review -> BLOCKED('preserved_regions'), review -> READY,
    #    re-stage drops the approval.
    def test_needs_review_blocks_until_reviewed(self):
        res = self.run_stage(gate_report("8-1", self.ingested, needs_review=True))
        self.assertEqual(res["histogram"], {"BLOCKED": 1})
        self.assertEqual(res["results"][0]["risks"], ["preserved_regions"])
        self.assertEqual(res["reasons"], {"preserved_regions": 1})
        with self.assertRaises(ST.StagingError):
            ST.assert_ready(ACT, "8-1", self.root)

        ST.review(ACT, "8-1", by="unittest", note="read the region", root=self.root)
        self.assertEqual(ST._inspect(self.d)["status"], "READY")
        # the approval is pinned to the output hash
        self.assertEqual(json.loads((self.d / "review.json").read_text())["output_sha256"],
                         ST._sha(self.d / "output.md"))

        res = self.run_stage(gate_report("8-1", self.ingested, needs_review=True))
        self.assertFalse((self.d / "review.json").exists())
        self.assertEqual(res["histogram"], {"BLOCKED": 1})

    # 3. tampering with a staged artifact after the fact -> FAIL
    def test_tampered_output_fails(self):
        self.run_stage(gate_report("8-1", self.ingested))
        (self.d / "output.md").write_text(INGESTED_MD + "\ninserted by hand\n")
        res = ST._inspect(self.d)
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("tampered", " ".join(res["reasons"]))
        self.assertEqual(ST.apply_list(self.root), [])

    # 4. a REJECTED certificate never reaches READY
    def test_rejected_gate_fails(self):
        res = self.run_stage(gate_report("8-1", self.ingested, verdict="REJECTED"))
        self.assertEqual(res["histogram"], {"FAIL": 1})
        self.assertEqual(ST.apply_list(self.root), [])

    # 5. a section nobody staged is refused
    def test_unstaged_section_refused(self):
        self.run_stage(gate_report("8-1", self.ingested))
        with self.assertRaises(ST.StagingError) as cm:
            ST.assert_ready(ACT, "999-99", self.root)
        self.assertIn("not staged", str(cm.exception))

    # 6. wrong gate family is refused, and nothing is staged
    def test_wrong_gate_family_refused(self):
        table_rep = gate_report("8-1", self.ingested)
        table_rep["gates"] = {"G1_prose_preserved": {"pass": True}}
        with self.assertRaises(ST.StagingError) as cm:
            self.run_stage(table_rep)
        self.assertIn("table gate report", str(cm.exception))

        alien = gate_report("8-1", self.ingested)
        alien["gates"] = {"X9_whatever": {"pass": True}}
        with self.assertRaises(ST.StagingError) as cm:
            self.run_stage(alien)
        self.assertIn("cannot tell which gate", str(cm.exception))

    # 7. a staging root inside the worktree is refused even in bulk
    def test_in_repo_root_refused(self):
        self.batch.write_text(json.dumps([gate_report("8-1", self.ingested)]))
        with self.assertRaises(ST.StagingError) as cm:
            ST.stage_run(ACT, self.batch, corpus_root=self.corpus,
                         root=ST.REPO / "data" / "scratch")
        self.assertIn("worktree", str(cm.exception))

    # 8. a section with no live corpus counterpart still stages (empty source)
    def test_new_section_stages_against_empty_source(self):
        new = self.tmp / "ingested" / "8-5.md"
        new.write_text(INGESTED_MD.replace("8-1", "8-5"))
        res = self.run_stage(gate_report("8-1", self.ingested),
                             gate_report("8-5", new))
        self.assertEqual(res["histogram"], {"READY": 2})
        self.assertEqual(res["new_sections"], 1)
        self.assertEqual((self.root / ACT / "8-5" / "source.md").read_text(), "")

    # 9. a certificate whose gated file has vanished is refused
    def test_missing_candidate_refused(self):
        self.ingested.unlink()
        with self.assertRaises(ST.StagingError) as cm:
            self.run_stage(gate_report("8-1", self.ingested))
        self.assertIn("no longer describes", str(cm.exception))

    # 10. the bulk summary JSON is written and machine-readable
    def test_summary_json(self):
        out = self.tmp / "summary.json"
        self.batch.write_text(json.dumps([gate_report("8-1", self.ingested)]))
        ST.stage_run(ACT, self.batch, corpus_root=self.corpus, root=self.root,
                     summary=out)
        data = json.loads(out.read_text())
        self.assertEqual(data["histogram"], {"READY": 1})
        self.assertEqual(data["sections"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
