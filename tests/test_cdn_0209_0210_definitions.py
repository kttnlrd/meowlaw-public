"""CDN-0209 / CDN-0210 — defined terms with "&"/apostrophes, and term boundaries.

CDN-0209: `scripts/build_definitions_index.py` term classes had no "&" and no
left boundary, so the regex restarted after the ampersand and "R&D entity"
became "d entity" (5 R&D terms lost in itaa-1997 s 995-1, plus the R&D family
in itaa-1936 and nz-it-2007).  A curly apostrophe (U+2019) cut captures the
same way ("arm’s length profits" -> "s length profits"); the scan body is now
normalised to straight quotes.

CDN-0210: nothing checked where a term started or ended, so the term field
carried prose, list fragments ("a) of a depreciating asset,") and section
headings ("57a meaning of corporation (1)").

These checks are written to fail against the pre-fix script — run them with
`DEFS_INDEX_MODULE=<path to the old script>` to reproduce:

    FAILED test_995_1_paragraph_terms_exact - got ['d entity', 's length
    profits', 'rba surplus']
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "austlii_995_1_terms.txt"
ITA_995_1 = (ROOT / "data" / "itaa-1997" / "sections" / "part-6-5"
             / "division-995" / "995-1.md")
ITA_1936_79A = ROOT / "data" / "itaa-1936" / "sections" / "part-iii" / "division-3" / "79A.md"
ITA_1936_90 = ROOT / "data" / "itaa-1936" / "sections" / "part-iii" / "division-5" / "90.md"
ITA_1936_6 = ROOT / "data" / "itaa-1936" / "sections" / "part-i" / "division-unknown" / "6.md"
CORPS_57A = (ROOT / "data" / "corporations-act-2001" / "sections" / "part-1"
             / "division-1.2" / "57A.md")
CORPS_1493 = (ROOT / "data" / "corporations-act-2001" / "sections" / "part-10"
              / "division-10.12" / "1493.md")

# The floor the plan sets for 995-1 recall (CDN-0209).
RECALL_FLOOR = 0.98

# A 995-1-style dictionary paragraph.  The apostrophe in "arm’s" is U+2019 —
# it is what the corpus carried between a014aefff and db1e4cddf and is what cut
# the capture before clean_scan_body() normalised it.
PARAGRAPH = (
    "In this Act, except so far as the contrary intention appears:\n"
    "R&D entity has the meaning given by section 355-35. "
    "arm\u2019s length profits has the meaning given by section 355-40. "
    "RBA surplus has the same meaning as in Part IIB of the Taxation "
    "Administration Act 1953."
)

# A list fragment: the capture starts at the list marker "a)".
LIST_FRAGMENT = "(a) of a depreciating asset, has the meaning given by section 40-130."

# No extracted term may look like a list/subsection marker.
FRAGMENT_RE = re.compile(r"^\(?[0-9a-z]{1,4}\)\s")
# No extracted term may be the lowercase tail of a longer word.
MIDWORD_RE = re.compile(r"^[a-z]\s")

VERBS = (r"(?:has (?:the|a) meaning(?: given by| affected by)?|"
         r"has the same meaning as(?: in)?|means|includes)\b")


def _module():
    """The definitions-index builder under test (overridable for fail-first runs)."""
    path = Path(os.environ.get("DEFS_INDEX_MODULE",
                               ROOT / "scripts" / "build_definitions_index.py"))
    spec = importlib.util.spec_from_file_location("build_definitions_index_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BDI = _module()


def _scan(body: str, act: str = "itaa-1997", section: str = "995-1") -> list[dict]:
    """Definition records for one section body, from the builder under test.

    Uses the builder's own per-body scanner when present.  The fallback
    reproduces the pre-CDN-0209 scan loop so this check can be pointed at the
    old script (DEFS_INDEX_MODULE) and shown failing for the right reason
    instead of erroring on a missing attribute.
    """
    if hasattr(BDI, "scan_section_body"):
        return BDI.scan_section_body(body, act, "display", section)

    clean = body.replace("*", "")
    out = []
    for m in BDI.STD_DEF_RE.finditer(clean):
        pos = m.start(1)
        if pos and re.match(r"[A-Za-z0-9]", clean[pos - 1]):
            continue
        term = BDI.normalize(m.group(1))
        if not term or not BDI.is_valid_def_term(term):
            continue
        out.append({"term": term, "act": act, "section": section,
                    "definition": clean[pos:pos + 60], "source": "std"})
    return out


def _terms(body: str, **kw) -> set[str]:
    return {r["term"] for r in _scan(body, **kw)}


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        m = re.search(r"\n---\s*\n", text)
        return text[m.end():] if m else text
    return text


def _normalise_space(text: str) -> str:
    return re.sub(r"[\u00a0\u2007\u202f]", " ", text)


def fixture_terms() -> list[str]:
    """The 1,927 distinct terms AustLII prints for ITAA 1997 s 995-1."""
    pat = re.compile(r'^"(.+?)"\s')
    terms = set()
    for line in FIXTURE.read_text(encoding="utf-8").split("\n"):
        m = pat.match(line.strip())
        if m:
            terms.add(_normalise_space(re.sub(r"\s+", " ", m.group(1))).strip())
    return sorted(terms)


def reference_terms() -> set[str]:
    """Fixture terms that ARE definition starts in the served 995-1 body.

    A fixture term counts when the section text carries it at a definition
    boundary — start of text, after a sentence/paragraph/list break — with only
    an optional ", <qualifier>," between the term and its verb.  This is the
    set a correct scanner must recover; the plan's "1,484" is the ticket's own
    count and is not reproducible from this fixture (docs/bug-plan-2026-09-25.md
    line 618 records the same finding for its 1,404 parse).
    """
    body = _normalise_space(_body(ITA_995_1))
    ref = set()
    for t in fixture_terms():
        pattern = (r"(?:^|[.;:\n>])\s*" + re.escape(t)
                   + r"\s*(?:,\s[^.;\n]{0,80}?,)?\s*" + VERBS)
        if re.search(pattern, body, re.I):
            ref.add(t)
    return ref


# ── CDN-0209 ───────────────────────────────────────────────────────────

def test_995_1_paragraph_terms_exact():
    """R&D / arm's-length / RBA terms come out whole, nothing mid-word."""
    got = _terms(PARAGRAPH)
    assert got == {"r&d entity", "arm's length profits", "rba surplus"}, got
    bad = sorted(t for t in got if MIDWORD_RE.match(t))
    assert not bad, f"terms starting mid-word (CDN-0209): {bad}"


def test_995_1_paragraph_no_curly_apostrophe_left():
    """Curly U+2019 is normalised to straight in the emitted term."""
    got = _terms(PARAGRAPH)
    assert "arm's length profits" in got
    assert all("\u2019" not in t for t in got), got


def test_recall_against_austlii_fixture():
    """At least 98% of the 995-1 definition starts in the fixture are found."""
    ref = reference_terms()
    assert len(ref) > 1400, f"fixture reference set looks wrong: {len(ref)}"

    found = _terms(_body(ITA_995_1))
    hit = {t for t in ref if t.lower() in found}
    pct = len(hit) / len(ref)
    print(f"995-1 recall: {len(hit)}/{len(ref)} = {pct:.2%} "
          f"(of all {len(fixture_terms())} fixture terms)")
    missing = sorted(t for t in ref if t.lower() not in found)
    assert pct >= RECALL_FLOOR, (
        f"recall {pct:.2%} < {RECALL_FLOOR:.0%}: {len(missing)} missing, "
        f"e.g. {missing[:10]}"
    )


def test_r_and_d_terms_present_in_995_1_scan():
    """The 5 R&D terms in 995-1 (the ticket's blast radius) are all recovered."""
    found = _terms(_body(ITA_995_1))
    expected = {"r&d activities", "r&d entity", "r&d partnership",
                "core r&d activities", "supporting r&d activities"}
    missing = sorted(expected - found)
    assert not missing, f"missing R&D terms: {missing}"


# ── CDN-0210 ───────────────────────────────────────────────────────────

def test_list_fragment_yields_no_term():
    """A capture that starts at a list marker is not a term (CDN-0210)."""
    assert _terms(LIST_FRAGMENT) == set()


def test_no_terms_at_all_are_list_fragments():
    """No term extracted from 995-1 or itaa-1936 looks like a list marker."""
    offenders = []
    for body, act, section in ((_body(ITA_995_1), "itaa-1997", "995-1"),
                               (_body(ITA_1936_79A), "itaa-1936", "79A"),
                               (_body(ITA_1936_90), "itaa-1936", "90")):
        for r in _scan(body, act=act, section=section):
            if FRAGMENT_RE.match(r["term"]):
                offenders.append((section, r["term"]))
    assert not offenders, f"{len(offenders)} list-fragment terms, e.g. {offenders[:10]}"


def test_no_term_begins_with_a_section_number():
    """Headings such as "57a meaning of corporation (1)" are rejected.

    corps-act s 57A ("57a meaning of corporation (1) subject to this section")
    and s 1493 ("1493 definitions amending schedule") both leak a heading as a
    term without the ^\\d+[a-z]?\\s gate — 65 corps captures in the index built
    from the pre-batch working copy.
    """
    cases = [(_body(ITA_995_1), "itaa-1997", "995-1"),
             (_body(ITA_1936_90), "itaa-1936", "90"),
             (_body(CORPS_57A), "corporations-act-2001", "57A"),
             (_body(CORPS_1493), "corporations-act-2001", "1493"),
             ("57a meaning of corporation (1) has the meaning given by subsection 57A(1).",
              "corporations-act-2001", "57A")]
    offenders = []
    for body, act, section in cases:
        for r in _scan(body, act=act, section=section):
            if re.match(r"^\d+[a-z]?\s", r["term"]):
                offenders.append((section, r["term"][:60]))
    assert not offenders, f"section-number terms: {offenders[:10]}"


def test_zone_a_survives():
    """"Zone A" (itaa-1936 s 79A) is a real term ending in an article."""
    got = _terms(_body(ITA_1936_79A), act="itaa-1936", section="79A")
    assert "zone a" in got, sorted(got)
    assert "zone b" in got, sorted(got)


def test_scoped_definition_keeps_its_own_section():
    """"exempt income, in relation to a partnership" (s 90) is not folded away.

    itaa-1936 defines the plain "exempt income" in s 6 and three scoped
    variants in s 90/95/102AAB.  Keying duplicates on term+act drops the s 90
    records; a stripped scope qualifier must key on term+act+section.
    """
    s90 = [(r["term"], r["section"]) for r in
           _scan(_body(ITA_1936_90), act="itaa-1936", section="90")
           if r["term"] == "exempt income"]
    assert s90 == [("exempt income", "90")], s90

    plain = [r for r in _scan(_body(ITA_1936_6), act="itaa-1936", section="6")
             if r["term"] == "exempt income"]
    assert plain, "s 6 must still define the plain 'exempt income'"

    merged = BDI.dedupe_records(plain + [r for r in
                                         _scan(_body(ITA_1936_90), act="itaa-1936", section="90")
                                         if r["term"] == "exempt income"])
    sections = sorted(r["section"] for r in merged)
    assert sections == ["6", "90"], (
        "the scoped s 90 definition was folded into another section's entry: "
        f"{sections}"
    )


def test_built_index_records_provenance():
    """The published index names its generator and the commit it was built from."""
    path = ROOT / "data" / "definitions_comprehensive.json"
    if not path.exists():
        pytest.skip("data/definitions_comprehensive.json not built in this tree")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("generator") == "scripts/build_definitions_index.py", data.get("generator")
    sha = str(data.get("source_commit") or "")
    assert re.fullmatch(r"[0-9a-f]{40}", sha), sha
