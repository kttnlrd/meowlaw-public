#!/usr/bin/env python3.12
"""C19 (CDN-0204) must flag the three fragment shapes and clear a whole title.

The ticket's population is the SERVED title list (data_loader.load_rulings), so the decision is
split into title_fragment_reason() and this test drives that same function with planted titles;
scan_ruling_title_fragments(titles=...) is the seam the live scan uses with the served list.

At HEAD the live scan reports 26 fragments - the plan's TARGET, not the 273 it measured, because
the 0204 extractor (_wrapped_title / _best_authoritative / _is_title_truncation) landed in
3b715b745. This test therefore does NOT assert a corpus count (that would fail the day the last
26 are repaired); it asserts the rule, and the live count is reported by the nightly.

Run: /usr/bin/python3.12 scripts/test_c19_ruling_titles.py      (exit 1 on any failure)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

# (citation, title, expected_to_fire) - the fragments are verbatim from the 26 the live scan
# reports at HEAD; the whole titles are served titles that must never be flagged.
CASES = [
    ("ATOID_2002_942", "ogroup ratio test", True),
    ("ATOID_2003_973", "ogroup ratio test", True),
    ("ATOID_2006_258", "(b)any net capital loss that is made in an income year", True),
    ("ATOID_2004_967", "Interaction between Division 7A of the", True),
    ("TR_2023_2", "Income tax: application of paragraph 8-1(2)(a) of the Income Tax Assessment "
                  "Act 1997 to labour costs related to the construction or creation of capital assets", False),
    ("TD_2019_6", "Deductions for prepaid expenditure payments", False),
    ("CR_2017_74", "Private rulings: whether a payment is deductible under section 8-1", False),
    ("GSTR_2000_20W", "GST and cancellation of supplies", False),
]

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


for citation, title, expect in CASES:
    why = S.title_fragment_reason(title)
    fired = why is not None
    check(fired == expect,
          f"{citation:16s} fired={fired} expected={expect} :: {why or 'whole title'}")

S.findings.clear()
S.scan_ruling_title_fragments(titles=[(c, t) for c, t, _ in CASES])
flagged = {f["path"].rsplit("/", 1)[-1] for f in S.findings}
expected_flagged = {c for c, _, e in CASES if e}
check(flagged == expected_flagged,
      f"the detector flags exactly the planted fragments -> {sorted(flagged)}")
check(all("data/rulings/" in f["path"] for f in S.findings),
      "findings are located by citation under data/rulings/")
check(len(S.findings) == len(expected_flagged),
      f"one finding per fragment, no duplicates (got {len(S.findings)})")

# CDN-0216 — the curated title is the tie-breaker on the shape rules. A brand-name title that
# starts lower case or with a digit is a WHOLE title; the sidecar says so, so it is not a
# fragment. A title that differs from its curated counterpart really is cut and still fires.
CDN_0216 = [
    # (citation, served title, curated title, expected_to_fire)
    ("CR_2022_1", "rhipe Limited - scheme of arrangement and special dividend",
     "rhipe Limited - scheme of arrangement and special dividend", False),
    ("CR_2022_4", "1300 Smiles Limited - scheme of arrangement and special dividend",
     "1300 Smiles Limited - scheme of arrangement and special dividend", False),
    ("PR_2025_12", "eFleetPass Tolling - toll road gift cards",
     "eFleetPass Tolling - toll road gift cards", False),
    ("ATOID_2007_50", "Consolidation - retained cost base assets - identification of",
     "Consolidation - retained cost base assets - identification of", False),
    ("ATOID_2002_942", "ogroup ratio test", "Thin Capitalisation", True),
    ("PSLA_2009_4", "history",
     "Decisions made by the Commissioner in the general administration of the taxation laws", True),
    ("TR_2012_2", "is the authorised version of this withdrawal notice.",
     "Income tax: effective life of depreciating assets (applicable from 1 July 2012)", True),
]
S.findings.clear()
S.scan_ruling_title_fragments(titles=[(c, t, k) for c, t, k, _ in CDN_0216])
flagged = {f["path"].rsplit("/", 1)[-1] for f in S.findings}
check(flagged == {c for c, _, _, e in CDN_0216 if e},
      f"the curated title suppresses brand-name shapes only -> {sorted(flagged)}")
# with no curated counterpart the shape rule stands alone
S.findings.clear()
S.scan_ruling_title_fragments(titles=[(c, t) for c, t, _, _ in CDN_0216])
check({f["path"].rsplit("/", 1)[-1] for f in S.findings}
      == {c for c, t, _, _ in CDN_0216 if S.title_fragment_reason(t)},
      "without a curated title the shape rule is unchanged (no silent suppression)")

# the extractor uses the same decision: a fragment candidate loses to the curated title
sys.path.insert(0, str(ROOT))
from backend.services.data_loader import _is_fragment_title, _reads_as_fragment  # noqa: E402
check(_is_fragment_title("ogroup ratio test", "Thin Capitalisation") is True,
      "extractor: a cut candidate is replaced by the curated title")
check(_is_fragment_title("1300 Smiles Limited - x", "1300 Smiles Limited - x") is False,
      "extractor: a brand-name title that equals the curated title is kept as-is")
check(_reads_as_fragment("history") and not _reads_as_fragment("Decisions made by the Commissioner"),
      "extractor: the fragment shape is the lower-case/digit/connector rule")
S.findings.clear()
S.scan_ruling_title_fragments()
check(not S.findings,
      f"the live served list has no uncorrected fragment (got {len(S.findings)})")

# an empty title is a finding too: it is the shape a failed extraction leaves
check(S.title_fragment_reason("") == "empty title" and S.title_fragment_reason(None) == "empty title",
      "an empty/None title is reported rather than silently passed")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
