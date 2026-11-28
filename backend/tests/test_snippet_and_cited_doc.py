"""Snippets carry no embedding header; section and case citations resolve exactly (real corpus)."""
import pytest

from backend.routes.search import _ANY_TAG, _body_text, _cited_doc, _finish_snippets


def test_header_stripped_body_kept():
    t = "[Ruling]: LCG 2018/6 — long title\n[Paragraph]: 5\n\nThe objects of the DPT"
    assert _body_text(t) == "The objects of the DPT"
    assert _body_text("[Case] [1905] HCA 57\n{Court} HCA\n{Facts} The company") == "The company"
    assert _body_text("[Ruling] LCG 2018/6\n[Title] Law Companion Ruling\n[Summary] true\n[References] ITAA") == ""


@pytest.mark.parametrize("row", [
    {"act": "rulings", "section": "LCG 2018/6", "embedding_id": 607218, "snippet": "[Ruling] LCG 2018/6"},
    {"act": "rulings", "section": "AID 2002/754", "embedding_id": None, "snippet": ""},
    {"act": "tax-cases", "section": "[1932] HCA 55", "embedding_id": 590188, "snippet": "[Case] [1932] HCA 55",
     "source_type": "case", "title": "Federal Commissioner of Taxation v Austin"},
])
def test_snippet_is_body(row):
    s = _finish_snippets([row])[0]["snippet"]
    assert len(s) > 60 and not _ANY_TAG.search(s) and s != row.get("title")


@pytest.mark.parametrize("q,key", [
    ("s 134-125 TAA 1953", ("taa-1953", "134-125")),
    ("s 326-20 ITAA 1936", ("itaa-1936", "326-20")),
    ("s RA-12 NZ Income Tax Act 2007", ("nz-it-2007", "RA-12")),
    ("s 6(1) ITAA 1936", ("itaa-1936", "6")),
    ("s 5.2.3 AML/CTF Rules 2007", ("aml-ctf-rules-2007", "5.2.3")),
    ("[1905] HCA 57", ("tax-cases", "[1905] HCA 57")),
    ("[1970] HCA 23", ("tax-cases", "[1970] HCA 23")),
])
def test_cited_doc(q, key):
    d = _cited_doc(q)
    assert (d["act"], d["section"]) == key


@pytest.mark.parametrize("q", ["26AH", "s 8-1", "section of the ITAA 1997", "ATO ID 2001/1", "1905 HCA"])
def test_cited_doc_lookalikes(q):
    assert _cited_doc(q) is None


@pytest.mark.parametrize("row", [  # extraction furniture and '[Omitted.]' stubs (2026-10-10 audit)
    {"act": "rulings", "section": "TA 2016/8", "citation": "TA_2016_8", "embedding_id": None,
     "snippet": "HEAD NOTE ======== Title: Taxpayer Alert Sections Referenced: • Division 165 ========"},
    {"act": "rulings", "section": "LCG 2015/14", "citation": "LCG_2015_14", "embedding_id": None,
     "snippet": "[3] Paragraph 275-20(4)(i). References Legislative References: ITAA 1997 ITAA 1997 275-1"},
    {"act": "rulings", "section": "GSTR 2004/6", "embedding_id": 924103, "snippet": "[Omitted]."},
])
def test_snippet_is_content_not_furniture(row):
    from backend.routes.search import _has_furniture, _is_content
    r = _finish_snippets([row])[0]
    assert _is_content(r["snippet"]) and not _has_furniture(r["snippet"]) and not _ANY_TAG.search(r["snippet"])
    if row["embedding_id"]:  # the pinpoint follows the chunk the text came from
        assert r["paragraph"] != "214" and f"[Paragraph]: {r['paragraph']}" in __import__("sqlite3").connect(
            "data/embeddings.db").execute("SELECT group_concat(embedding_text) FROM embeddings WHERE act='ato' "
                                         "AND section='GSTR 2004/6'").fetchone()[0]


def test_short_section_kept_twins_dropped_paragraphs_kept():
    from backend.routes.search import _drop_twins
    t = "2A Application of the Criminal Code"
    rows = [{"act": "taa-1953", "section": "2A", "snippet": t}, {"act": "fbt-1986", "section": "2A", "snippet": t},
            {"act": "itaa-1936", "section": "6", "snippet": "6 Interpretation"},
            {"act": "sis-1993", "section": "6", "snippet": "6 Prudential regulation"}] + \
           [{"act": "rulings", "section": "TR 2024/1", "paragraph": p, "snippet": "same"} for p in ("1", "2", "3")]
    assert [(r["act"], r["section"], r.get("paragraph")) for r in _drop_twins(rows)] == [
        ("taa-1953", "2A", None), ("itaa-1936", "6", None), ("sis-1993", "6", None),
        ("rulings", "TR 2024/1", "1"), ("rulings", "TR 2024/1", "2"), ("rulings", "TR 2024/1", "3")]
    assert _finish_snippets([{"act": "itaa-1936", "section": "1", "embedding_id": None, "snippet": ""}])[0][
        "snippet"] == "1 Short title"


@pytest.mark.parametrize("stored,printed", [
    ("PS_LA_2004_11", "PS LA 2004/11"), ("PSLA_2005_24", "PS LA 2005/24"), ("TD_2026_D1", "TD 2026/D1"),
    ("TR_98_17", "TR 98/17"), ("TR_2012_1", "TR 2012/1"), ("IT_2167", "IT 2167"), ("AID_2001_1", "AID 2001/1"),
])
def test_ruling_display_is_printed_form(stored, printed):
    from backend.services.search_service import _citation_to_display
    assert _citation_to_display(stored) == printed


@pytest.mark.parametrize("q,ref", [("PS LA 2004/11", "PS LA 2004/11"), ("PSLA 2004/11", "PS LA 2004/11"),
                                   ("PS LA 2004 11", "PS LA 2004/11"), ("TD 2026/D1", "TD 2026/D1")])
def test_cited_ruling_printed_form(q, ref):
    from backend.routes.search import _cited_ruling
    assert _cited_ruling(q)[1]["section"] == ref


def test_section_of_act_without_vectors_gets_body():
    from backend.routes.search import _finish_snippets, _is_content
    s = _finish_snippets([{"act": "aml-ctf-2006", "section": "75", "title": "Remittance Sector Register",
                           "snippet": "", "embedding_id": None, "source_type": "section"}])[0]["snippet"]
    assert _is_content(s) and s.startswith("75 Remittance Sector Register") and "Last updated" not in s
