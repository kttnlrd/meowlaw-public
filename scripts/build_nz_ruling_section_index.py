#!/usr/bin/env python3
"""Build data/nz_ruling_section_index.json — NZ ruling -> (act, section) refs.

Feeds pipeline/graph_etl.load_nz_rulings(), the NZ counterpart of
ruling_section_index.json (ATO rulings -> AU acts). Without it the NZ acts have
no graph presence at all.

Seven NZ Acts are ingested under data/<act>/ (each with tree.json + sections/):

    nz-it-2007        Income Tax Act 2007        "s YA 1"       -> YA-1
    nz-taa-1994       Tax Administration Act 1994 "s 91F"       -> 91F
    nz-gst-1985       Goods and Services Tax Act 1985 "s 20(3)" -> 20
    nz-egda-1968      Estate and Gift Duties Act 1968
    nz-scd-1971       Stamp and Cheque Duties Act 1971
    nz-kiwisaver-2006 KiwiSaver Act 2006
    nz-sl-2011        Student Loan Scheme Act 2011

The known section ids are read from each act's tree.json (plus the section
filenames), never hardcoded: nz-it-2007 keys its sections with the letter-block
form ("LJ-5") while the six numeric acts key sections as they are numbered
("91F", "20", "108B"). Subsection detail ("20(3)") is dropped — graph nodes are
sections, not subsections.

Every reference is ATTRIBUTED to an act before it is emitted:

  * the act named in the citing sentence, else the nearest preceding act name;
  * for the letter-block form (used only by nz-it-2007 among the ingested acts)
    only an Income Tax Act mention can change the default, so a sentence citing
    "sections BD 2, DA 1 ... of the Income Tax Act 2004" is SKIPPED rather than
    linked to ITA 2007;
  * if the nearest act name is one we have not ingested (Companies Act 1993,
    Income Tax Act 1994, ...), or no act can be established, the ref is skipped.

EVERY emitted ref is then validated against the attributed act's own section
ids, so a regex false positive cannot invent a section.

    python3.12 scripts/build_nz_ruling_section_index.py --selfcheck
    python3.12 scripts/build_nz_ruling_section_index.py --report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NZ_DIR = ROOT / "data" / "nz_rulings"
OUT = ROOT / "data" / "nz_ruling_section_index.json"

# Ingested NZ acts live in data/nz-<name>-<year>/ — the year suffix is what makes
# a directory an Act (data/nz-master-tax-guide is a guide, not an Act).
ACT_DIR_RE = re.compile(r"^nz-.+-(?:19|20)\d{2}$")

# Act names as they occur in rulings. A year-less "Tax Administration Act" etc. is
# unambiguous (only one such Act) but "Income Tax Act" is NOT: six Income Tax Acts
# exist, so the year-less form is deliberately absent here and the text "Income Tax
# Act 2004" is treated as an other (non-ingested) Act.
NAMED_ACTS = [
    ("nz-it-2007", r"Income\s+Tax\s+Act\s+2007|\bITA\s+2007\b"),
    ("nz-taa-1994", r"Tax\s+Administration\s+Act\s+1994"
                    r"|Tax\s+Administration\s+Act(?!\s+(?:19|20)\d\d)|\bTAA\s+1994\b"),
    ("nz-gst-1985", r"Goods\s+and\s+Services\s+Tax\s+Act\s+1985"
                    r"|Goods\s+and\s+Services\s+Tax\s+Act(?!\s+(?:19|20)\d\d)"
                    r"|\bGST\s*Act\b|\bGSTA\b"),
    ("nz-egda-1968", r"Estate\s+and\s+Gift\s+Duties\s+Act\s+1968"
                     r"|Estate\s+and\s+Gift\s+Duties\s+Act(?!\s+(?:19|20)\d\d)|\bEGDA\b"),
    ("nz-scd-1971", r"Stamp\s+and\s+Cheque\s+Duties\s+Act\s+1971"
                    r"|Stamp\s+and\s+Cheque\s+Duties\s+Act(?!\s+(?:19|20)\d\d)|\bSCDA\b"),
    ("nz-kiwisaver-2006", r"KiwiSaver\s+Act\s+2006"
                          r"|KiwiSaver\s+Act(?!\s+(?:19|20)\d\d)"),
    ("nz-sl-2011", r"Student\s+Loan\s+Scheme\s+Act\s+2011"
                   r"|Student\s+Loan\s+Scheme\s+Act(?!\s+(?:19|20)\d\d)|\bSLS\s+Act\b"),
]
NAMED_RE = [(act, re.compile(pat, re.I)) for act, pat in NAMED_ACTS]

# Any "<Proper Noun ...> Act [year]" title — including parenthesised words, as in
# "Local Government (Rating) Act 2002". Lets us see that a sentence is about an act
# we have NOT ingested, which must suppress a link rather than guess one.
ACT_TITLE_RE = re.compile(
    r"(?:(?:[A-Z][A-Za-z’'\-]*|\([A-Za-z][A-Za-z’'\-]*\)|and|of|the|for|to|on|in)"
    r"\s+){1,9}Act\b(?:\s+(?:19|20)\d\d)?")

# words that carry no act identity of their own ("the Act", "the principal Act")
_NON_TITLE = {
    "the", "this", "that", "these", "those", "an", "a", "any", "said", "such",
    "its", "his", "her", "of", "and", "in", "to", "for", "on", "under", "with",
    "by", "principal", "relevant", "same", "new", "other", "whole", "another",
}
_BARE_ITA_RE = re.compile(r"Income\s+Tax\s+Act\s*$", re.I)
_ITA_RE = re.compile(r"Income\s+Tax", re.I)

# "s LJ 5", "ss YA 2", "section 91F", "s 20(3)", "ss 108 and 108B", "sections 75A".
# Letter block: nz-it-2007. Plain / numeric: the other six acts.
# A trailing letter suffix is an uppercase run of up to 4 ("91F", "108B", "183ABAB").
# "(3)" is a subsection (dropped). "-65" (Australian "70-65") and "(A)" (ambiguous:
# could be a suffix or a paragraph) are refused outright.
_SECTION = (r"(?:(?P<lblk>[A-Z]{1,3})\s*-?\s*(?P<lnum>\d{1,4})(?P<lsuf>[A-Z]{0,4})"
            r"|(?P<num>\d{1,4})(?P<nsuf>[A-Z]{0,4})(?!\s*-\s*\d)(?!\([A-Z]\)))"
            r"(?:\(\d+[a-z]?\))?")
# a cite opens with a keyword and one section ...
_REF_RE = re.compile(r"\b(?:ss|s|sections|section|sec|sect)\.?\s+" + _SECTION)
# ... and "ss 108 and 108B" / "sections 5, 6 and 8" / "ss 5(11D)-(11I) and 8"
# continue with a run of separators (commas, "and", ranges, "(11I)") then a section.
_NEXT_RE = re.compile(r"\s*(?:(?:,|and|or|to)\s+|[–—-]\s*|\(\d+[a-z]?\)\s*)+" + _SECTION)

# A cite can name its act by acronym instead of title: "s 10 of the LGRA",
# "under the RMA, s 122". Such an acronym is an act we know only by name, so it must
# suppress the link — but only when it is not one of our acts' own abbreviations.
ACT_ALIASES = {"GST", "GSTA", "TAA", "EGDA", "SCDA", "ITA", "SLS"}
_TAIL_ACT_RE = re.compile(r"\s+of\s+the\s+([A-Z]{2,7})\b")
_PRE_ACT_RE = re.compile(r"([A-Z]{2,7})\s+(?:ss|s|sections|section)\.?\s*$")

# sentence terminators, but not after a citation abbreviation ("s.", "no.", "cf.")
_ABBR_RE = re.compile(r"\b(s|ss|sec|sect|no|cf|eg|ie|etc|v|para|art|cl|pt|subs"
                      r"|vol|inc|co|ltd|mr|dr|hon|fig|p)\.$", re.I)
_SENT_END_RE = re.compile(r"([.!?])(\s+)(?=[A-Z(“\"'(])")


# --------------------------------------------------------------------------- data
def known_sections(act: str) -> set[str]:
    """The act's real section ids, straight from its own tree.json + filenames."""
    tree = json.loads((ROOT / "data" / act / "tree.json").read_text())
    ids: set[str] = set()

    def walk(n) -> None:
        if isinstance(n, dict):
            for s in n.get("sections", []) or []:
                sid = s.get("id") or s.get("section") or s.get("path")
                if sid:
                    ids.add(str(sid).strip().upper().replace(" ", "-"))
            for k in ("parts", "divisions", "subparts", "subdivisions", "chapters"):
                for c in n.get(k, []) or []:
                    walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(tree)
    # filenames are the second opinion on the same set
    ids |= {p.stem.upper().replace(" ", "-")
            for p in (ROOT / "data" / act / "sections").rglob("*.md")}
    return ids


def ingested_acts() -> list[str]:
    """Every nz-<name>-<year> directory that carries a tree.json."""
    return sorted(d.name for d in (ROOT / "data").iterdir()
                  if d.is_dir() and ACT_DIR_RE.match(d.name)
                  and (d / "tree.json").exists())


# ------------------------------------------------------------------- normalising
def normalize(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space, with a map back to source offsets.

    Rulings hard-wrap their lines, so act names arrive as "Tax\\n\\nAdministration
    Act 1994" and cites as "section\\n\\n91F". Everything downstream (act mentions,
    refs, sentences) runs on the normalized text and is mapped back for quoting.
    """
    out: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            idx.append(i)
            i = j
        else:
            out.append(text[i])
            idx.append(i)
            i += 1
    return "".join(out), idx


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END_RE.finditer(text):
        if _ABBR_RE.search(text[max(0, m.start() - 5):m.start() + 1]):
            continue
        spans.append((start, m.start() + 1))
        start = m.start() + 1
    spans.append((start, len(text)))
    return spans


def _title_core(span: str) -> str:
    """'of the Goods and Services Tax Act 1985' -> 'Goods and Services Tax'."""
    core = re.sub(r"\s+Act(?:s)?(?:\s+(?:19|20)\d\d)?$", "", span.strip())
    words = core.split()
    while words and words[0].lower() in _NON_TITLE:
        words.pop(0)
    return " ".join(words)


def _classify(span: str) -> str | None:
    for act, rx in NAMED_RE:
        if rx.search(span):
            return act
    return None


def act_mentions(norm: str) -> list[tuple[int, str | None, str]]:
    """(position, act-id-or-None, text) for every Act named in `norm`.

    act-id is None for an Act we have not ingested; a determiner + "Act"
    ("the Act", "this Act") names nothing and is dropped entirely.
    """
    raw: list[tuple[int, str | None, str]] = []
    for m in ACT_TITLE_RE.finditer(norm):
        span = " ".join(m.group(0).split())
        if not _title_core(m.group(0)):
            continue                      # "the Act"
        raw.append((m.start(), _classify(span), span))
    for act, rx in NAMED_RE:              # acronym-only mentions ("EGDA", "TAA 1994")
        for m in rx.finditer(norm):
            if re.search(r"\bAct\b", m.group(0)):
                continue                  # already covered by ACT_TITLE_RE
            raw.append((m.start(), act, " ".join(m.group(0).split())))
    raw.sort()
    mentions: list[tuple[int, str | None, str]] = []
    last_end = -1
    for pos, act, text in raw:
        if pos < last_end:                # overlapping spans: keep the first
            continue
        mentions.append((pos, act, text))
        last_end = pos + len(text)
    return mentions


def _is_other_income_tax_act(text: str) -> bool:
    """An Income Tax Act that is not the ingested one ("Income Tax Act 2004").

    A bare year-less "the Income Tax Act" is ambiguous, not a different Act.
    """
    return bool(_ITA_RE.search(text)) and not _BARE_ITA_RE.search(text)


# --------------------------------------------------------------------- extraction
class _Refs:
    """Ordered, deduped refs plus the counts of what was refused and why."""

    def __init__(self) -> None:
        self.by_act: list[tuple[str, str]] = []
        self._seen: set[tuple[str, str]] = set()
        self.skipped = 0     # no act could be established
        self.dropped = 0     # act established, but the id is not a section of it

    def add(self, act: str, sid: str) -> None:
        if (act, sid) not in self._seen:
            self._seen.add((act, sid))
            self.by_act.append((act, sid))


def _named_by_other_acronym(norm: str, start: int, end: int) -> bool:
    """True when the cite names its act by a non-ingested Act's acronym.

    "s 10 of the LGRA", "under the RMA, s 122", "42 USC section 401" cite an act we
    know only by acronym; we cannot place them, so they are refused. An acronym that
    introduces one of our acts ("the NZ Goods and Services Tax Act 1985") does not.
    """
    tail = _TAIL_ACT_RE.match(norm, end)
    if tail and tail.group(1) not in ACT_ALIASES:
        if not _classify(norm[tail.start():tail.start() + 60]):
            return True
    pre = _PRE_ACT_RE.search(norm, max(0, start - 20), start)
    if pre and pre.group(1) not in ACT_ALIASES:
        return True
    return False


def extract(text: str, known: dict[str, set[str]]) -> _Refs:
    """Resolve every section cited in `text` to (act, section), or refuse it."""
    norm, _ = normalize(text)
    mentions = act_mentions(norm)
    spans = sentence_spans(norm)
    starts = [s for s, _ in spans]
    mpos = [p for p, _, _ in mentions]

    def nearest(cands: list[tuple[int, str | None, str]], p: int):
        """The act named in p's sentence, else the nearest preceding one."""
        if not cands:
            return None
        i = bisect_right(starts, p) - 1
        s0, s1 = spans[i]
        lo = bisect_left([c[0] for c in cands], s0)
        hi = bisect_right([c[0] for c in cands], s1 - 1)
        ins = cands[lo:hi]
        if ins:
            return min(ins, key=lambda c: abs(c[0] - p))
        return cands[lo - 1] if lo > 0 else None

    res = _Refs()
    for m in _REF_RE.finditer(norm):
        # one cite can list several sections: "ss 108 and 108B"
        cites = [m]
        pos = m.end()
        while True:
            nxt = _NEXT_RE.match(norm, pos)
            if not nxt or len(cites) >= 20:
                break
            cites.append(nxt)
            pos = nxt.end()

        for c in cites:
            if _named_by_other_acronym(norm, c.start(), c.end()):
                res.skipped += 1
                continue
            letter = bool(c.group("lblk"))
            if letter:
                sid = f"{c.group('lblk')}-{c.group('lnum')}{c.group('lsuf') or ''}".upper()
                # The letter-block form belongs to an Income Tax Act. Only an explicit
                # one can displace the nz-it-2007 default; ITA 1976/1994/2004 means this
                # ref is not ours, so it is refused rather than guessed.
                cands = [x for x in mentions
                         if x[1] == "nz-it-2007"
                         or (x[1] is None and _is_other_income_tax_act(x[2]))]
                hit = nearest(cands, c.start())
                act = "nz-it-2007" if (hit is None or hit[1] == "nz-it-2007") else None
            else:
                sid = f"{c.group('num')}{c.group('nsuf') or ''}".upper()
                hit = nearest(mentions, c.start())
                act = None if (hit is None or hit[1] is None) else hit[1]

            if act is None:
                res.skipped += 1
                continue
            if sid in known.get(act, set()):
                res.add(act, sid)
            else:
                res.dropped += 1
    return res


# ---------------------------------------------------------------------- selfcheck
def selfcheck(known: dict[str, set[str]]) -> bool:
    ok = True

    def case(text: str, want_ids: list[str], label: str, act: str = "nz-it-2007") -> None:
        """Legacy view: compare the resolved section ids only."""
        nonlocal ok
        got = [s for a, s in extract(text, known).by_act if a == act]
        good = got == want_ids
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {label:44s} got={got} want={want_ids}")

    def case_act(text: str, want: list[tuple[str, str]], label: str,
                 skipped: int | None = None, dropped: int | None = None) -> None:
        """Act-aware view: compare (act, section) pairs and the refusal counts."""
        nonlocal ok
        res = extract(text, known)
        good = res.by_act == want
        if skipped is not None:
            good = good and res.skipped == skipped
        if dropped is not None:
            good = good and res.dropped == dropped
        ok = ok and good
        extra = ""
        if skipped is not None or dropped is not None:
            extra = f" skip={res.skipped} drop={res.dropped}"
        print(f"  {'ok  ' if good else 'FAIL'} {label:44s} "
              f"got={res.by_act}{extra} want={want}")

    # --- letter-number (nz-it-2007) behaviour: unchanged from before -------------
    real = sorted(known["nz-it-2007"])[0]          # e.g. AA-1
    letters, num = real.split("-")[:2]
    case(f"see s {letters} {num} of the Act", [real], "s <block> <n>")
    case(f"sections {letters} {num} and {letters} {num}",
         [real], "same ref twice dedupes")
    case(f"section {letters} {num}(2)(a) applies",
         [real], "subsection detail dropped")
    case("see section 8-1 of the ITAA", [], "AU form is not an NZ ref")
    case("the amount is s 300 and s 40", [], "bare numbered section not matched")
    case("", [], "empty text")

    # --- numeric cites, explicit act name -> that act ---------------------------
    case_act("see section 91F of the Tax Administration Act 1994",
             [("nz-taa-1994", "91F")], "s 91F + TAA 1994 named")
    case_act("in section 20(3) of the Goods and Services Tax Act 1985",
             [("nz-gst-1985", "20")], "s 20(3) + GST Act 1985 named")
    case_act("ss 108 and 108B of the Tax Administration Act 1994",
             [("nz-taa-1994", "108"), ("nz-taa-1994", "108B")], "ss 108 and 108B")
    case_act("section 11A of the Goods and Services Tax Act 1985",
             [("nz-gst-1985", "11A")], "s 11A + GST Act 1985 named")
    case_act("s 79(1) of the Estate and Gift Duties Act 1968 applies",
             [("nz-egda-1968", "79")], "s 79(1) + EGDA 1968 named")
    case_act("under s 215 of the KiwiSaver Act 2006",
             [("nz-kiwisaver-2006", "215")], "s 215 + KiwiSaver Act 2006 named")
    # nearest preceding act name governs later cites in the same text
    case_act("Legislative references are to the Goods and Services Tax Act 1985 "
             "unless stated. Under s 8 tax is charged.",
             [("nz-gst-1985", "8")], "act named earlier governs later cites")

    # --- numeric cite with no act name -> skipped, never guessed ----------------
    case_act("section 20(3) applies to the supply",
             [], "bare s 20(3) is skipped", skipped=1)
    case_act("The company was registered under the Companies Act 1993 and "
             "s 8 does not apply",
             [], "other act named -> skipped", skipped=1)

    # --- validation: bad id cannot invent a section ----------------------------
    case_act("section 9999 of the Goods and Services Tax Act 1985",
             [], "unknown id is dropped", dropped=1)

    # --- not NZ -----------------------------------------------------------------
    case_act("section 70-65 of the Income Tax Assessment Act 1997",
             [], "AU 70-65 is not an NZ ref")
    case_act("section OB 1 of the Income Tax Act 2004",
             [], "ITA 2004 is not ITA 2007", skipped=1)
    case_act("rates are payable by the ratepayer (section 10 of the LGRA)",
             [], "another act by acronym -> skipped", skipped=1)
    case_act("GST is charged on the supply under s 8 of the GSTA",
             [("nz-gst-1985", "8")], "GSTA is the GST Act")
    return ok


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print drop/coverage counts")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()

    acts = ingested_acts()
    if "nz-it-2007" not in acts:
        print("FAIL: data/nz-it-2007/tree.json is missing — cannot build the index")
        return 1
    known = {act: known_sections(act) for act in acts}
    empty = [act for act, ids in known.items() if not ids]
    if empty:
        print(f"FAIL: no section ids found for {empty} — tree.json unreadable")
        return 1
    print("ingested NZ acts:")
    for act in acts:
        print(f"  {act:20s} {len(known[act]):5d} known section ids")

    if a.selfcheck:
        print("selfcheck:")
        return 0 if selfcheck(known) else 1

    texts = sorted(NZ_DIR.glob("*.txt"))
    index: dict[str, list[dict]] = {}
    no_ref: list[str] = []
    per_act: Counter = Counter()
    per_act_rulings: Counter = Counter()
    per_prefix: Counter = Counter()
    ref_total = skipped_total = dropped_total = 0
    for p in texts:
        res = extract(p.read_text(errors="replace"), known)
        skipped_total += res.skipped
        dropped_total += res.dropped
        if res.by_act:
            index[p.stem] = [{"act": act, "section": sid} for act, sid in res.by_act]
            per_prefix[p.stem.split("_")[0]] += 1
            ref_total += len(res.by_act)
            for act in {act for act, _ in res.by_act}:
                per_act_rulings[act] += 1
            per_act.update(act for act, _ in res.by_act)
        else:
            no_ref.append(p.stem)

    OUT.write_text(json.dumps(index, indent=0, sort_keys=True) + "\n")

    print(f"rulings scanned          : {len(texts)}")
    print(f"rulings with >=1 NZ ref  : {len(index)}")
    print(f"total refs               : {ref_total}")
    print(f"rulings with no NZ ref   : {len(no_ref)}")
    print(f"refs skipped (no act)    : {skipped_total}")
    print(f"refs dropped (bad id)    : {dropped_total}")
    print(f"wrote {OUT.relative_to(ROOT)}")
    if a.report:
        print("  refs per act / rulings naming that act:")
        for act, n in per_act.most_common():
            print(f"    {act:20s} {n:5d} refs in {per_act_rulings[act]:4d} rulings")
        print(f"  rulings with refs by type: {dict(per_prefix.most_common())}")
        print(f"  sample no-ref: {no_ref[:10]}")

    # selfcheck always runs: extraction with no selfcheck is decoration
    print("selfcheck:")
    return 0 if selfcheck(known) else 1


if __name__ == "__main__":
    sys.exit(main())
