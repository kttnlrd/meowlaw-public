#!/usr/bin/env python3.12
"""Pin the behaviour of the C6_cut_token check so a correction cannot blind it.

C6 (CDN-0081) looks for a heading severed at a cut point, leaving its trailing token stranded as
the next heading. It previously fired on a different, legitimate shape and produced 15 findings,
all in nz-master-tax-guide, all false: the guide prints a heading carrying its paragraph number and
then the short topic heading underneath it, so H2 repeats the START of H1 rather than its end.

A detector that no longer fires is not a fix, it is a blind spot with a quieter name. These cases
therefore include the damage shape as well as the legitimate one, and use the real corpus pairs
rather than invented ones so the convention side is evidence rather than assertion.

Run: /usr/bin/python3.12 scripts/test_c6_cut_token.py      (exit 1 on any failure)
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts.scan_corpus_error_classes import cut_token_shape  # noqa: E402

# (h1, h2, expected) - the legitimate pairs are verbatim from the 15 findings this class produced.
CASES = [
    # --- the act's own heading + topic-heading convention (must NOT be reported as damage) ---
    ("Fishing gear and quotas ¶27-371", "Fishing gear", "convention"),
    ("Opting out of KiwiSaver ¶29-225", "Opting out", "convention"),
    ("Interest and penalties for GST ¶32-225", "Interest", "convention"),
    ("Assessment of a civil penalty ¶14-021", "Assessment", "convention"),
    ("Refund and transfer of overpaid provisional tax ¶22-140", "Refund", "convention"),
    ("Asset Categories from 2005–06 income year ¶50-162", "Asset Category", "convention"),
    ("Industry Categories rates prior to 2005–06 income year ¶50-163", "Industry Category",
     "convention"),
    ("Deductions, tax credits and trust losses ¶25-160", "Deductions", "convention"),
    ("Restrictive covenants and exit inducement payments ¶5-297", "Restrictive covenants",
     "convention"),
    ("Advantages and disadvantages of being a qualifying company ¶19-015", "Advantages",
     "convention"),
    ("Correcting errors and refunds of deductions of RWT ¶15-180", "Correcting errors",
     "convention"),
    ("Surrender or revocation of RWT-exempt status ¶15-140", "Surrender", "convention"),
    ("Deductions available to trustee of deceased estate ¶25-670", "Deductions available",
     "convention"),
    ("Industry categories from 2005–06 income year ¶50-161", "Industry Categories", "convention"),
    ("Asset categories rates prior to 2005–06 income year ¶50-164", "Asset categories",
     "convention"),
    # the shared word sits at the BACK here, or shares only a stem - still the same convention
    ("Introduction to tax credits ¶11-010", "Credits", "convention"),
    ("Resident withholding tax on interest ¶15-040", "Interest", "convention"),
    ("Tax rates for trusts ¶25-500", "Trustee income", "convention"),

    # --- the damage shape CDN-0081 describes (must STILL be reported) ---
    ("Deductions for prepaid expenditure payments", "payments", "cut"),
    ("Substantiation of losses and outgoings deductions", "deductions", "cut"),
    ("Application of the requirements of Division", "Division", "cut"),
    ("payments", "payments", "cut"),

    # --- neither shape ---
    ("Substantiation requirement", "How to work out your deduction", "none"),
    ("Loss carry back", "Loss carry forward", "none"),
    ("Fishing gear and quotas", "Fishing gear", "none"),   # no ¶ reference: not the convention
]

failures = []
for h1, h2, expected in CASES:
    got = cut_token_shape(h1, h2)
    flag = "ok " if got == expected else "FAIL"
    if got != expected:
        failures.append((h1, h2, expected, got))
    print(f"  {flag} {got:11s} (want {expected:11s}) H1={h1[:46]!r} H2={h2[:24]!r}")

print()
cut_expected = [c for c in CASES if c[2] == "cut"]
convention_expected = [c for c in CASES if c[2] == "convention"]
print(f"  damage shapes still caught : {len(cut_expected)}/{len(cut_expected)}")
print(f"  legitimate pairs cleared   : "
      f"{sum(1 for h1, h2, e in convention_expected if cut_token_shape(h1, h2) == 'convention')}"
      f"/{len(convention_expected)}")

if failures:
    print(f"\nFAILED {len(failures)}/{len(CASES)}")
    for h1, h2, expected, got in failures:
        print(f"  H1={h1!r} H2={h2!r}: got {got}, expected {expected}")
    sys.exit(1)
print(f"\nall {len(CASES)} C6 cases pass - the convention is ignored and the damage shape is caught")
