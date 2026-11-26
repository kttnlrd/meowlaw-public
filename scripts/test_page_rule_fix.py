#!/usr/bin/env python3.12
"""The page-rule fixer must fix these, and the guard that stops it over-merging must be load-bearing.

Case 4 is the regression that shipped first: a rule at the end of the CONTINUATION line rode into
the merged sentence and survived, leaving 21 runs on index rows.  If someone simplifies the join
back to stripping only the starting line, this test fails.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from fix_page_rule_artifacts import rewrite  # noqa: E402

RUN = "_" * 37
failures = 0


def check(name: str, cond: bool, detail: str = ""):
    global failures
    failures += 0 if cond else 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" :: {detail}" if detail and not cond else ""))


# 1. a sentence split by a rule, with the following Note swallowed into the paragraph
src1 = f"**(3)** For the effect of the GST, see Division 27. Note If you receive an amount {RUN}\n\namount may be included in your assessable income: see Subdivision 20-A.\n"
out1, ch1 = rewrite(src1)
kinds1 = {c["kind"] for c in ch1}
check("split sentence is rejoined", "Div 27. Note" not in out1 and "see Subdivision 20-A" in out1, out1[:120])
check("the swallowed Note becomes its own block", "> **Note:**" in out1, out1[:160])
check("no rule remains", RUN not in out1 and "______" not in out1)

# 2. an index row: rule stripped, rows NOT merged
src2 = f"capital gains ..................... 102-5 {RUN}\nfranked dividends ................. 207-20 {RUN}\n"
out2, ch2 = rewrite(src2)
check("index row rule stripped", "______" not in out2, out2[:120])
check("index rows are not merged",
      len([l for l in out2.split("\n") if l.strip()]) == 2, repr(out2[:160]))

# 3. a rule inside a formula fence is a fraction bar and must survive untouched
src3 = f"```ingest-formula source=vol03.pdf page=511\n   CG amount   ×   {RUN}\n```\n"
out3, ch3 = rewrite(src3)
check("fence content is untouched", out3 == src3 and not ch3)

# 4. THE REGRESSION: the continuation line carries its own rule
src4 = f"The amount is worked out on the following basis {RUN}\n\nin accordance with the table {RUN}\n"
out4, ch4 = rewrite(src4)
check("a rule on the continuation line is stripped too", "______" not in out4, out4[:200])
check("both rules are recorded",
      sum(1 for c in ch4 if c["kind"] == "page_rule_removed") == 2, str(ch4))
check("the sentence is joined once", out4.count("in accordance") == 1)

# 5. a page rule rendered as a TABLE ROW must take its row with it, not leave "| | | |"
src5 = "| 18 | An interest payment to an overseas person | 12-245 |\n| " + RUN + " |  |  |\n| prose follows |  |  |\n"
out5, ch5 = rewrite(src5)
check("a rule-only table row is dropped, not blanked",
      "| |  |  |" not in out5 and out5.count("\n") == 2, repr(out5))
check("the other rows survive", "12-245" in out5 and "prose follows" in out5)
check("it is recorded as a dropped row",
      any(c["kind"] == "page_rule_row_dropped" for c in ch5), str(ch5))

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
