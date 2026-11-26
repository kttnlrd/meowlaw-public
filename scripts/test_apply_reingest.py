#!/usr/bin/env python3
"""Tests for scripts/apply_reingest.py — stdlib unittest, temp dirs only.

Nothing here touches data/** or the real staging tree.  Every fixture builds a
throwaway corpus + a throwaway staging root and stages through the real
table_rebuild_staging module, so the verdicts under test are the live ones.

  /usr/bin/python3.12 scripts/test_apply_reingest.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AR = _load("apply_reingest")
STAGING = _load("table_rebuild_staging")
GUARD = _load("corpus_change_guard")

SRC_MD = """---
section: "82-150"
compilation_no: "266"
---

# 82-150  Limit on tax free amount

The amount is worked out under the formula.

| Amount | Years | Base |
| --- | --- | --- |
| 1 | 2 | 3 |
"""
OUT_MD = SRC_MD.replace("| 1 | 2 | 3 |", "| 1 | 2 | 3 |\n| 4 | 5 | 6 |")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Fixture:
    """A temp corpus + temp staging root with one staged section."""

    def __init__(self, tmp: Path, section="82-150", needs_review=False, out_text=OUT_MD,
                 src_text=SRC_MD):
        self.tmp = tmp
        self.act = "itaa-1997"
        self.section = section
        self.root = tmp / "stage"
        self.corpus = tmp / "data" / self.act / "sections" / "division-82"
        self.corpus.mkdir(parents=True, exist_ok=True)
        self.corpus_file = self.corpus / f"{section}.md"
        self.corpus_file.write_text(src_text)
        cand = tmp / "cand" / f"{section}.md"
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(out_text)
        gate = tmp / f"gate-{section}.json"
        gate.write_text(json.dumps({"verdict": "ACCEPTED", "section": section,
                                    "needs_review": needs_review,
                                    "gates": {"R1_pdf_token_recall": {"pass": True}}}))
        self.report = STAGING.stage(self.act, section, self.corpus_file, cand,
                                    gate_report=gate, root=self.root, force=True)
        self.d = self.root / self.act / section

    def run(self, **kw):
        kw.setdefault("guard", False)
        kw.setdefault("keep_going", True)
        # The fixture's corpus is a throwaway tree, so say so explicitly. The applier
        # resolves the write target by section identity under the corpus root and
        # refuses any recorded source_path that is not inside it; without this the
        # fixture's temp path looks exactly like the /tmp staging copies that caused
        # a silent no-op apply in production.
        kw.setdefault("corpus_root", self.tmp / "data")
        return AR.apply_run(self.act, self.root, **kw)


class ApplyReingestTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    # 1
    def test_dry_run_writes_nothing(self):
        f = Fixture(self.tmp)
        before = self.corpus_snapshot(f)
        s = f.run(dry_run=True, log=self.tmp / "log.jsonl")
        self.assertEqual(s["applied"], 1)
        self.assertEqual(before, self.corpus_snapshot(f))
        self.assertFalse((self.tmp / "log.jsonl").exists())

    # 2
    def test_ready_applies_byte_identical(self):
        f = Fixture(self.tmp)
        s = f.run(log=self.tmp / "log.jsonl")
        self.assertEqual(s["applied"], 1, s)
        self.assertEqual(f.corpus_file.read_bytes(), (f.d / "output.md").read_bytes())

    # 3
    def test_blocked_preserved_regions_refused(self):
        f = Fixture(self.tmp, needs_review=True)
        before = f.corpus_file.read_bytes()
        s = f.run(only=[f.section])
        self.assertEqual(s["applied"], 0)
        self.assertIn("preserved_regions", s["refusals"][0])
        self.assertEqual(before, f.corpus_file.read_bytes())

    # 4
    def test_tampered_staged_output_refused(self):
        f = Fixture(self.tmp)
        (f.d / "output.md").write_text(OUT_MD + "\nsmuggled\n")
        s = f.run(only=[f.section])
        self.assertEqual(s["applied"], 0)
        # staging itself calls this FAIL (tampered); either way it must not land
        self.assertRegex(s["refusals"][0], "tamper")
        self.assertEqual(f.corpus_file.read_text(), SRC_MD)

    # 5
    def test_corpus_moved_since_staging_refused(self):
        f = Fixture(self.tmp)
        f.corpus_file.write_text(SRC_MD + "\nsomeone edited this\n")
        s = f.run(only=[f.section])
        self.assertEqual(s["applied"], 0)
        self.assertIn("changed since staging", s["refusals"][0])

    # 6
    def test_review_pinned_to_other_hash_refused(self):
        f = Fixture(self.tmp, needs_review=True)          # risky -> needs review
        STAGING.review(f.act, f.section, by="test", root=f.root)
        self.assertEqual(STAGING.verify(f.root)[f"{f.act}/{f.section}"]["status"], "READY")
        rev = json.loads((f.d / "review.json").read_text())
        rev["output_sha256"] = "0" * 64
        (f.d / "review.json").write_text(json.dumps(rev))
        s = f.run(only=[f.section])
        self.assertEqual(s["applied"], 0)
        self.assertEqual(f.corpus_file.read_text(), SRC_MD)

    # 7
    def test_log_hashes_and_resume(self):
        f = Fixture(self.tmp)
        log = self.tmp / "log.jsonl"
        old = sha(f.corpus_file)
        new = sha(f.d / "output.md")
        f.run(log=log)
        rec = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(rec["old_sha256"], old)
        self.assertEqual(rec["new_sha256"], new)
        self.assertEqual(rec["bytes"], (f.d / "output.md").stat().st_size)
        self.assertTrue(rec["changed"])
        s2 = f.run(log=log)
        self.assertEqual((s2["applied"], s2["skipped_already_applied"]), (0, 1))
        self.assertEqual(len(log.read_text().splitlines()), 1)

    # 8
    def test_interrupted_write_leaves_original_intact(self):
        f = Fixture(self.tmp)
        orig = f.corpus_file.read_bytes()
        real_replace = os.replace

        def boom(src, dst):
            raise KeyboardInterrupt("crash between fsync and replace")

        AR.os.replace = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                AR.atomic_write(f.corpus_file, b"half a file")
        finally:
            AR.os.replace = real_replace
        self.assertEqual(f.corpus_file.read_bytes(), orig)
        leftovers = [p.name for p in f.corpus.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_new_section_without_corpus_file_refused(self):
        f = Fixture(self.tmp)
        rep = json.loads((f.d / "report.json").read_text())
        rep["source_path"] = None
        (f.d / "report.json").write_text(json.dumps(rep))
        s = f.run(only=[f.section])
        self.assertEqual(s["applied"], 0)
        self.assertIn("new section", s["refusals"][0])

    # the applier's own tamper/review checks, independent of the staging
    # snapshot (which may be stale by the time we write)
    def test_plan_section_rechecks_tamper_and_review_pin(self):
        f = Fixture(self.tmp)
        ready = {"status": "READY", "risks": [], "gate": "ACCEPTED"}
        (f.d / "output.md").write_text(OUT_MD + "\nsmuggled\n")
        with self.assertRaisesRegex(AR.ApplyError, "tampered after staging"):
            AR.plan_section(f.act, f.section, ready, f.root, corpus_root=f.tmp / "data")
        (f.d / "output.md").write_text(OUT_MD)
        (f.d / "review.json").write_text(json.dumps({"output_sha256": "0" * 64}))
        with self.assertRaisesRegex(AR.ApplyError, "stale approval"):
            AR.plan_section(f.act, f.section, ready, f.root, corpus_root=f.tmp / "data")
        self.assertEqual(f.corpus_file.read_text(), SRC_MD)

    def corpus_snapshot(self, f) -> dict:
        return {str(p): sha(p) for p in sorted(f.corpus.rglob("*")) if p.is_file()}


# ── the G-B prose-collapse adjudication ─────────────────────────────────────
# The corpus stored this finding table as one run of prose; the re-ingest
# rebuilt it from the PDF.  The guard's G-B rule calls that "prose converted to
# rows" and BLOCKs — correct for a per-table rebuild, wrong as a pass/fail for a
# whole-section re-ingest.  These fixtures are real: the BLOCK under test comes
# from corpus_change_guard.check_file itself, never from a hand-written string.
GB_FM = '''---
section: "112-77"
source_pdf: "vol03.pdf"
---
'''
GB_SRC = GB_FM + """# 112-77  Exchangeable interests

Exchangeable interests

Item In this situation Element affected See section 1 You acquire shares in a company in exchange for the disposal of an exchangeable interest and the disposal of the exchangeable interest was to the issuer of the exchangeable interest First element of cost base and reduced cost base 130-105 2 You acquire shares in a company in exchange for the redemption of an exchangeable interest First element of cost base and reduced cost base 130-105
"""
GB_OUT = GB_FM + """# 112-77  Exchangeable interests

Exchangeable interests

| Item | In this situation | Element affected | See section |
| --- | --- | --- | --- |
| 1 | You acquire shares in a company in exchange for the disposal of an exchangeable interest and the disposal of the exchangeable interest was to the issuer of the exchangeable interest | First element of cost base and reduced cost base | 130-105 |
| 2 | You acquire shares in a company in exchange for the redemption of an exchangeable interest | First element of cost base and reduced cost base | 130-105 |
"""
# The source PDF says everything the prose said (this is what the re-ingest read).
GB_PDF = ("112-77  Exchangeable interests\n"
          "Item In this situation: Element affected: See section:\n"
          "1 You acquire shares in a company in exchange for the disposal of an "
          "exchangeable interest and the disposal of the exchangeable interest was to "
          "the issuer of the exchangeable interest First element of cost base and "
          "reduced cost base 130-105\n"
          "2 You acquire shares in a company in exchange for the redemption of an "
          "exchangeable interest First element of cost base and reduced cost base 130-105\n")


class AdjudicationTest(unittest.TestCase):
    """Deliverable 1: a G-B block is adjudicated per file, never relaxed."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._real_guard, self._real_pdf = AR.run_guard, AR.section_pdf_text
        self.addCleanup(self._restore)
        self.pdf_text = GB_PDF

    def _restore(self):
        AR.run_guard, AR.section_pdf_text = self._real_guard, self._real_pdf

    def fixture(self, src=GB_SRC, out=GB_OUT):
        f = Fixture(self.tmp, section="112-77", src_text=src, out_text=out)
        self.old = src
        # The guard runs for real — only its git plumbing (HEAD side, repo-relative
        # paths) is replaced, because the fixture corpus is a temp dir, not the repo.
        def fake_run_guard(paths):
            res = [GUARD.check_file(self.old, Path(p).read_text(), str(p)) for p in paths]
            rc = 1 if any(r["status"] == "BLOCK" for r in res) else 0
            return rc, "\n".join(f"{r['status']} {r['path']}" for r in res), res
        AR.run_guard = fake_run_guard
        AR.section_pdf_text = lambda *a, **k: (self.pdf_text, "vol03.pdf pp.361-362")
        return f

    def run_batch(self, f, **kw):
        buf = io.StringIO()
        return f.run(guard=True, out=buf, **kw), buf.getvalue()

    def only_block(self, s):
        self.assertEqual(s["guard"]["output"].count("BLOCK"), 1, s["guard"]["output"])

    # 11 — the real case: tokens are in the PDF and now sit in table rows
    def test_gb_collapse_with_tokens_in_pdf_and_in_rows_is_adjudicated(self):
        f = self.fixture()
        s, text = self.run_batch(f)
        adj = s["adjudication"]
        self.assertEqual((adj["adjudicated"], adj["fatal"]), (1, 0), text)
        ev = adj["files"][0]
        self.assertEqual(ev["verdict"], "ADJUDICATED")
        self.assertTrue(ev["tokens"], "no token evidence recorded")
        self.assertTrue(all(t["in_pdf"] for t in ev["tokens"]))
        self.assertTrue(all("row" in t["now_in"] for t in ev["tokens"]))
        self.assertTrue(all(t["at"] for t in ev["tokens"]))
        self.assertEqual(s["guard"]["returncode"], 0)
        self.assertEqual(s["guard"]["verdict"], "ADJUDICATED")
        self.assertIn("WARN ADJUDICATED", text)
        self.assertIn("G-B prose collapsed", "\n".join(ev["reasons"]))

    # 12 — a token the PDF does not have is a real loss: stays fatal
    def test_token_absent_from_pdf_stays_fatal(self):
        f = self.fixture()
        self.pdf_text = GB_PDF.replace("redemption", "")
        s, text = self.run_batch(f)
        ev = s["adjudication"]["files"][0]
        self.assertEqual(ev["verdict"], "FATAL")
        self.assertIn("not in the source PDF", ev["why"])
        self.assertIn("redemption", ev["why"])
        self.assertEqual(s["guard"]["returncode"], 1)
        self.assertIn("NOT safe to keep", text)

    # 13 — in the PDF but nowhere in the output: still a loss, stays fatal
    def test_token_in_pdf_but_missing_from_output_stays_fatal(self):
        src = GB_SRC.replace("See section 1 You acquire",
                             "See section Zeugma 1 You acquire")
        f = self.fixture(src=src)
        self.pdf_text = GB_PDF + "\nZeugma\n"
        s, _ = self.run_batch(f)
        ev = s["adjudication"]["files"][0]
        self.assertEqual(ev["verdict"], "FATAL")
        self.assertIn("appear nowhere in a table row", ev["why"])
        tok = next(t for t in ev["tokens"] if t["token"] == "Zeugma")
        self.assertTrue(tok["in_pdf"])
        self.assertEqual(tok["now_in"], [])
        self.assertEqual(s["guard"]["returncode"], 1)

    # 14 — a block carrying any other rule is never adjudicated
    def test_other_rule_in_block_never_adjudicated(self):
        # a second pipe block with no separator row: G-A fires as well as G-B
        out = GB_OUT + "\n| stray | block |\n| with | no separator |\n"
        f = self.fixture(out=out)
        s, _ = self.run_batch(f)
        ev = s["adjudication"]["files"][0]
        self.assertTrue(any("G-A" in r for r in ev["reasons"]), ev["reasons"])
        self.assertEqual(ev["verdict"], "FATAL")
        self.assertIn("other than G-B prose-collapse", ev["why"])
        self.assertEqual(ev["tokens"], [])
        self.assertEqual(s["guard"]["returncode"], 1)

    # 15b — an anchor id is markup and exempt; the same word outside a tag is not
    def test_markup_only_tokens_exempt_but_real_words_still_proven(self):
        src = GB_SRC.replace("Exchangeable interests\n\nItem",
                             'Exchangeable interests\n\n<a id="s112-77-a"></a>\n\nItem')
        f = self.fixture(src=src)
        s, _ = self.run_batch(f)
        ev = s["adjudication"]["files"][0]
        self.assertEqual(ev["verdict"], "ADJUDICATED", ev["why"])
        marked = {t["token"] for t in ev["tokens"] if t["markup"]}
        # 'a' is the tag name, but 'a' also occurs in real prose -> NOT exempt
        self.assertEqual(marked, {"id", "s112-77-a"})
        self.assertTrue(all(not t["in_pdf"] for t in ev["tokens"] if t["markup"]))

        # the same anchor, but 'interest' also lives inside the tag AND in real
        # prose: it is NOT markup-only, so it still has to be in the PDF
        self.pdf_text = GB_PDF.replace("interest", "x")
        f2 = self.fixture(src=src)
        s2, _ = self.run_batch(f2)
        ev2 = s2["adjudication"]["files"][0]
        self.assertEqual(ev2["verdict"], "FATAL")
        self.assertIn("interest", ev2["why"])

    # 15 — --no-adjudicate is the raw guard: a block is always fatal
    def test_no_adjudicate_keeps_the_block_fatal(self):
        f = self.fixture()
        s, text = self.run_batch(f, adjudicate=False)
        self.assertNotIn("adjudication", s)
        self.assertEqual(s["guard"]["returncode"], 1)
        self.assertEqual(s["guard"]["verdict"], "BLOCK")
        self.assertIn("NOT safe to keep", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
