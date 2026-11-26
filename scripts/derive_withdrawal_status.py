#!/usr/bin/env python3
"""Derive withdrawal status + the document's own citation for every ruling series.

Read-only over data/rulings/*.txt. Writes ONE new file
(data/rulings/withdrawal_status.json); it touches no paragraph JSON, so it cannot
collide with an embedder that is reading those files.

Why this matters: the splitter derives each ruling's reference from the filename,
so a withdrawal-version document loses its trailing 'W' (CR 2002/71W -> CR 2002/71)
and the corpus presents a withdrawn authority as the live one. Citing a withdrawn
authority is the professional risk this data exists to prevent.

WHAT THE SOURCE SHOWED (measured, not assumed):
  * The 'W' in a citation is the ATO's marker for the WITHDRAWN VERSION and is
    authoritative on its own: TD_1993_12's own cover sheet reads "This cover sheet
    is provided for information only. It does not form part of TD 93/12W ...", while
    its body never says "withdrawn". 424 of 1,034 TD documents are W versions: 291
    say so in the text, 133 do not. Both are withdrawn versions.
  * A withdrawal notice inside a document does NOT mean that document is withdrawn.
    TD_2015_9 and TD_2016_9 carry the notice for TD 2014/2; PR_2011_13 carries the
    notice for PR 2000/30. False positives unless the notice names this document.
  * A relative clause about someone else's withdrawal is not this document's:
    TR_2005_6 says "This Ruling replaces Taxation Ruling TR 1999/18, which was
    withdrawn".

Classification (conservative: a wrong badge is worse than no badge — it either
hides a live authority or gives false comfort about a dead one):
  withdrawn   the document's own citation carries the W marker, or the text
              withdraws THIS document by name, or a withdrawal notice names THIS
              document, or the title block carries "(Withdrawn)" / the placeholder.
  mentions    the word appears, but about something else.
  current     no mention and no W marker.

Usage:
  /usr/bin/python3.12 scripts/derive_withdrawal_status.py            # write the file
  /usr/bin/python3.12 scripts/derive_withdrawal_status.py --selftest # regression check
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
RULINGS = DATA / "rulings"
OUT = RULINGS / "withdrawal_status.json"

SERIES = ("AID", "CR", "PR", "GSTR", "SGR", "PS", "PCG", "LCG", "MT", "TA", "TR", "TD", "IT")

# The document's own citation, by series. Longer forms must come first.
CITE_PATTERNS: dict[str, re.Pattern[str]] = {
    "AID": re.compile(r"\bATO\s+ID\s+(\d{4})/(\d+)(W?)\b", re.I),
    "CR": re.compile(r"\bCR\s+(\d{4})/(\d+)(W?)\b", re.I),
    "PR": re.compile(r"\bPR\s+(\d{4})/(\d+)(W?)\b", re.I),
    "GSTR": re.compile(r"\bGSTR\s+(\d{4})/(\d+)(W?)\b", re.I),
    "SGR": re.compile(r"\bSGR\s+(\d{4})/(\d+)(W?)\b", re.I),
    "PS": re.compile(r"\bPS\s*LA\s+(\d{4})/(\d+)(W?)\b", re.I),
    "PCG": re.compile(r"\bPCG\s+(\d{4})/(\d+)(W?)\b", re.I),
    "LCG": re.compile(r"\bLC[GR]\s+(\d{4})/(\d+)(W?)\b", re.I),
    "MT": re.compile(r"\bMT\s+(\d{4})/(\d+)(W?)\b", re.I),
    "TA": re.compile(r"\bTA\s+(\d{4})/(\d+)(W?)\b", re.I),
    "TR": re.compile(r"\bTR\s+(\d{2,4})/(\d+)(W?)\b", re.I),
    "TD": re.compile(r"\bTD\s+(\d{2,4})/(\d+)(W?)\b", re.I),
    "IT": re.compile(r"\bIT\s+(\d{2,4})/(\d+)(W?)\b", re.I),
}

SELF_NOUN = (
    r"(?:ATO\s*ID|ATOID|ATO\s+Interpretative\s+Decision|Interpretative\s+Decision|decision|"
    r"document|ruling|class\s+ruling|product\s+ruling|draft\s+ruling|determination|guideline|"
    r"practice\s+statement|alert|taxpayer\s+alert|GST\s+ATO\s+ID)"
)
# Includes the passive and announced forms the ATO actually uses:
#   "... and has been withdrawn", "... and is therefore withdrawn",
#   "ATO ID 2001/248 is to be withdrawn. Decision not current."
VERB = (
    r"(?:(?:is|are)\s+(?:therefore\s+|being\s+)?|has\s+been\s+|have\s+been\s+|was\s+|were\s+|"
    r"will\s+be\s+|is\s+to\s+be\s+|now\s+)withdrawn"
)
# "This ATO ID withdrawn as it is a simple restatement of the law" — the ATOID series
# sometimes drops the auxiliary verb. Applied to AID only: widening it to every series
# marked 1,324 extra CR documents withdrawn (an optional auxiliary matches any later
# "withdrawn" in the sentence), which is exactly the false-positive class to avoid.
NO_AUX = re.compile(rf"\b(?:this|the)\s+{SELF_NOUN}\b[^.,;]{{0,20}}?\bwithdrawn\b", re.I)
# "ATO ID is withdrawn because it is a straight application of the law" — the ATOID
# series sometimes drops its own number in the notice sentence. The lookahead is
# essential: "ATO ID 2011/58 was withdrawn" is a DIFFERENT document being withdrawn
# (caught by the regression test), so a numbered citation must not match here.
BARE_ATOID = re.compile(r"\bATO\s*ID\b(?!\s*\d)[^.;]{0,20}?(?:is|has\s+been|was)\s+withdrawn", re.I)

# "This ATO ID is withdrawn" / "This document has been Withdrawn" / "The alert is
# withdrawn". The gap may not contain a comma, semicolon or "which", so a relative
# clause about another instrument ("TR 1999/18, which was withdrawn") cannot mark
# this document.
SELF_SENTENCE = re.compile(rf"\b(?:this|the)\s+{SELF_NOUN}\b[^.,;]{{0,60}}?{VERB}", re.I)
TITLE_FLAG = re.compile(r"\(Withdrawn\)|\bWithdrawn\b\s*$", re.I | re.M)
REMOVED = re.compile(r"THIS\s+RULING\s+HAS\s+BEEN\s+REMOVED", re.I)
VIEW_NOTICE = re.compile(r"View\s+the\s+Withdrawal\s+notice\s+for\s+this\s+document", re.I)
NOTICE_HDR = re.compile(r"Notice\s+of\s+Withdrawal", re.I)
MENTION = re.compile(r"withdraw", re.I)


def read(stem: str) -> str:
    return (RULINGS / f"{stem}.txt").read_text(encoding="utf-8", errors="replace")


def own_citation(series: str, text: str) -> str | None:
    pat = CITE_PATTERNS.get(series)
    if not pat:
        return None
    m = pat.search(text[:4000]) or pat.search(text)
    if not m:
        return None
    year, num, flag = (m.groups() + ("", ""))[:3]
    label = "ATO ID" if series == "AID" else ("PS LA" if series == "PS" else series)
    return f"{label} {year}/{num}{flag}".strip()


def wants_withdrawn(text: str, cite: str | None) -> list[str]:
    """Evidence that THIS document is a withdrawn version. Empty = no evidence."""
    why: list[str] = []
    if cite and cite.endswith("W"):
        why.append("citation_w_marker")

    # The document naming ITSELF plus a withdrawal verb: "ATO ID 2001/186 has been
    # withdrawn from the database ...", "PS LA 1998/10 is withdrawn from 1 June 2004".
    # Safe precisely because it only ever matches this document's own citation, so the
    # relative-clause trap ("TR 1999/18, which was withdrawn") cannot trip it.
    if cite:
        label_part, _, rest = cite.partition(" ")
        year, _, num = rest.partition("/")
        base_num = num.rstrip("W")
        variants = set()
        for yr in {year, year[-2:] if len(year) == 4 else year}:
            for w in ("", "W"):
                variants.add(f"{label_part} {yr}/{base_num}{w}")
                if label_part == "ATO ID":
                    # the series spells itself both ways: "ATO ID 2002/387" and "ATOID 2002/387"
                    variants.add(f"ATOID {yr}/{base_num}{w}")
        for v in variants:
            if re.search(rf"\b{re.escape(v)}\b[^.;]{{0,60}}?{VERB}", text, re.I):
                why.append("own_citation")
                break

    title_block = "\n".join([ln.strip() for ln in text.splitlines() if ln.strip()][:6])
    if SELF_SENTENCE.search(text):
        why.append("self_sentence")
    if NO_AUX.search(text) and cite and cite.startswith("ATO ID"):
        why.append("no_aux_sentence")
    if TITLE_FLAG.search(title_block):
        why.append("title_flag")
    if REMOVED.search(text):
        why.append("removed_placeholder")
    if VIEW_NOTICE.search(text[:6000]):
        why.append("view_notice")
    if BARE_ATOID.search(text[:6000]):
        why.append("bare_atoid_notice")
    # A bare "Withdrawn" status marker in the head block, e.g. "Decision Withdrawn as of
    # 26 October 2001" or "Withdrawn pending consultation ...".
    if re.search(r"\bWithdrawn\b", title_block) or re.search(
        r"\bwithdrawn\b(?=[^.]{0,40}(?:as of|pending|from the database|with effect))", text[:6000], re.I
    ):
        why.append("head_marker")

    # A notice counts only if it names this document. Match the full citation, never a
    # bare paragraph number: a two-character number matches almost any following text
    # (TD 2015/9's "9" matched the notice about TD 2014/2, which is a different document).
    if cite:
        variants = set()
        label_part, _, rest = cite.partition(" ")
        year, _, num = rest.partition("/")
        base_num = num.rstrip("W")
        for yr in {year, year[-2:] if len(year) == 4 else year}:
            for w in ("", "W"):
                variants.add(f"{label_part} {yr}/{base_num}{w}")
        for m in NOTICE_HDR.finditer(text):
            window = text[m.end(): m.end() + 220]
            if any(re.search(rf"\b{re.escape(v)}\b", window) for v in variants):
                why.append("notice_naming_self")
                break
    return why


def classify(text: str, cite: str | None) -> tuple[str, list[str]]:
    why = wants_withdrawn(text, cite)
    if why:
        return "withdrawn", sorted(set(why))
    if MENTION.search(text[:6000]):
        return "mentions_withdrawal", []
    return "current", []


def build() -> dict[str, dict]:
    out: dict[str, dict] = {}
    para_dir = RULINGS / "paragraphs"
    for txt in sorted(RULINGS.glob("*.txt")):
        stem = txt.stem
        series = stem.split("_", 1)[0]
        if series not in SERIES:
            continue
        text = read(stem)
        cite = own_citation(series, text)
        status, reasons = classify(text, cite)

        # Second, independent derivation: the AID splitter records a status per document
        # (scoped to the text before the 'Issue' heading). Neither derivation is
        # authoritative — sampling found the splitter over-marking one document (a
        # notice about TR94/D25, not itself) and this scan under-marking others
        # ("This ATO ID withdrawn" with no auxiliary). Keep both, merge as withdrawn
        # if either says so, and record which — so the disagreement stays visible
        # instead of being silently resolved.
        f = para_dir / f"{stem}.json"
        splitter_status = None
        if f.exists():
            try:
                splitter_status = json.loads(f.read_text(encoding="utf-8")).get("status")
            except (json.JSONDecodeError, OSError):
                splitter_status = None
        if splitter_status == "withdrawn" and status != "withdrawn":
            status = "withdrawn"
            reasons = sorted({*reasons, "splitter_agreement"})

        mstem = re.match(r"^([A-Z]+(?:_LA)?)_(\d{4})_(\d+)$", stem)
        stem_ref = (
            f"{mstem.group(1).replace('_', ' ')} {mstem.group(2)}/{mstem.group(3)}"
            if mstem
            else stem.replace("_", " ")
        )
        out[stem] = {
            "status": status,
            "status_derived": classify(text, cite)[0],
            "status_splitter": splitter_status,
            "citation_alt": cite,
            "derived_ref": stem_ref,
            "citation_differs": bool(cite and cite.rstrip("W") != stem_ref),
            "withdrawal_version": bool(cite and cite.endswith("W")),
            "evidence": reasons,
        }
    return out


def selftest() -> int:
    """Labelled regression set, established by reading the source text."""
    must_withdraw = {
        "AID_2001_1": "This ATO ID is withdrawn",
        "CR_2001_10": "This Ruling is withdrawn / self-cites CR 2001/10W",
        "CR_2002_71": "withdrawal version",
        "PR_2002_1": "This Product Ruling is withdrawn",
        "PR_2010_10": "This Product Ruling has been withdrawn",
        "PS_LA_1998_2": "title: PS LA 1998/2 (Withdrawn)",
        "PS_LA_1998_10": "PS LA 1998/10 is withdrawn from 1 June 2004",
        "IT_100": "This document has been Withdrawn",
        "TA_2002_3": "superseded ... and is now withdrawn",
        "TA_2002_7": "The alert is withdrawn",
        "TD_1999_1": "Notice of Withdrawal ... TD 1999/1 is withdrawn",
        "TD_2001_8": "Notice of Withdrawal ... TD 2001/8 is withdrawn",
        "TD_1993_12": "self-cites TD 93/12W; withdrawn version, silent body",
        "TD_2015_9": "self-cites TD 2015/9W; also carries the notice for TD 2014/2",
        "TD_2016_9": "self-cites TD 2016/9W",
        "TR_1992_1": "Notice of Withdrawal ... TR 92/1 is withdrawn",
        "GSTR_1999_1": "Notice of Withdrawal ... GSTR 1999/1W",
        "MT_2000_1": "Notice of Withdrawal ... MT 2000/1",
        "SGR_2005_1": "Notice of Withdrawal ... SGR 2005/1",
        "LCG_2015_3": "Notice of Withdrawal ... LCR 2015/3",
    }
    must_not = {
        "PR_2011_13": "carries the notice for PR 2000/30, not itself",
        "TR_2005_6": "'This Ruling replaces TR 1999/18, which was withdrawn'",
        "LCG_2015_11": "refers to ATO ID 2011/58 being withdrawn",
        "PCG_2017_10": "refers to TD 2012/19 being withdrawn",
        "TD_1995_6": "'the drought declaration is withdrawn'",
        "SGR_2005_2": "refers to SGR 93/2 being withdrawn",
    }
    fails = 0
    print("MUST be withdrawn:")
    for stem, note in must_withdraw.items():
        text = read(stem)
        st, why = classify(text, own_citation(stem.split("_", 1)[0], text))
        ok = st == "withdrawn"
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {stem:16} {st:20} {','.join(why)[:30]:30} {note[:46]}")
    print("\nMUST NOT be withdrawn:")
    for stem, note in must_not.items():
        text = read(stem)
        st, why = classify(text, own_citation(stem.split("_", 1)[0], text))
        ok = st != "withdrawn"
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {stem:16} {st:20} {','.join(why)[:30]:30} {note[:46]}")
    total = len(must_withdraw) + len(must_not)
    print(f"\n{total} labelled cases, {fails} failed")
    return 1 if fails else 0


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    out = build()
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")

    counts: Counter[tuple[str, str]] = Counter()
    ev: Counter[tuple[str, str]] = Counter()
    for stem, v in out.items():
        counts[(stem.split("_", 1)[0], v["status"])] += 1
        for r in v["evidence"]:
            ev[(stem.split("_", 1)[0], r)] += 1

    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes, {len(out):,} documents)\n")
    print(f"{'series':7}{'docs':>8}{'withdrawn':>11}{'mentions':>10}{'current':>9}{'W-cites':>9}")
    tot: Counter[str] = Counter()
    for s in sorted({k[0] for k in counts}):
        n = sum(v for (ss, _), v in counts.items() if ss == s)
        w, m, c = counts[(s, "withdrawn")], counts[(s, "mentions_withdrawal")], counts[(s, "current")]
        wv = sum(1 for k, v in out.items() if k.startswith(s + "_") and v["withdrawal_version"])
        tot.update({"n": n, "w": w, "m": m, "c": c, "wv": wv})
        print(f"{s:7}{n:>8,}{w:>11,}{m:>10,}{c:>9,}{wv:>9,}")
    print(f"{'TOTAL':7}{tot['n']:>8,}{tot['w']:>11,}{tot['m']:>10,}{tot['c']:>9,}{tot['wv']:>9,}")

    print("\nevidence behind each withdrawn classification:")
    for (s, r), v in sorted(ev.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {s:6} {r:22} {v:>6,}")

    diff = [k for k, v in out.items() if v["citation_differs"]]
    print(f"\nthe document's own citation differs from the derived reference: {len(diff):,}")
    print("  by series:", dict(Counter(k.split('_', 1)[0] for k in diff)))
    for k in sorted(diff)[:6]:
        print(f"    {out[k]['derived_ref']:16} -> {out[k]['citation_alt']}")


if __name__ == "__main__":
    main()
