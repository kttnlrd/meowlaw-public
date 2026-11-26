from __future__ import annotations

import json
import re
import logging
import math
import os
import sqlite3
import threading
import time
import datetime
from pathlib import Path

from fastapi import HTTPException, APIRouter, Request, Response

from backend.config import DATA_DIR, RULING_DIR, SEARCH_DB
from backend.doc_ids import STATE_TAX_ACTS
from backend.services.data_loader import load_tree
from backend.services.search_service import (
    search_conn, init_search_index as build_search_index,
    search_sections as fts_search, search_rulings, search_section_ids,
    search_private_rulings_fts, search_case_index,
    expand_semantic_query, _citation_to_display,
)
from backend.services import vector_search_service
from backend.services import searchd_client
from backend.services import private_ruling_outcomes
from backend.services import search_facets
from backend.services import usage_log
from backend.services.graph_neighborhood import neighborhoods as graph_neighborhoods
from backend.services.graph_alias import lookup as alias_lookup
from backend.services.content_access import act_key, available_keys, filter_hits, hit_key, user_from
from content.registry import content_type_for, tag as _tag_content_type

logger = logging.getLogger(__name__)
router = APIRouter()

RRF_K = 60


def _web_content_type(kw: dict) -> str:
    """usage_log content_type for a web search: the one type/act it is narrowed to, else mixed."""
    t, act = (kw.get("type") or "").lower(), kw.get("act") or ""
    if t in ("legislation", "case", "ruling", "private_ruling", "rg", "afsa", "treaty"):
        return {"legislation": "section"}.get(t, t)
    return {"rulings": "ruling", "private-rulings": "private_ruling", "tax-cases": "case",
            "treaties": "treaty", "regulatory-guides": "rg", "afsa-guides": "afsa"}.get(
        act, "section" if act else "mixed")

# --- Domain scoping ---------------------------------------------------------
# Every query runs in exactly one of four non-crossing domains (search-proposal
# §2.1/D1). `act` is validated against the resolved scope's Act set; only
# au-tax has rulings, private rulings, cases and treaties.
DOMAIN_ACTS: dict[str, set[str]] = {
    # Australian tax practice includes State taxes (payroll tax, land tax, duties are
    # State revenue), so the 12 State Acts sit in this area too. Without this a default
    # search for "land tax" returns nothing from them — a silent miss. The narrower
    # `state-tax` scope still exists for searching State legislation alone.
    "au-tax": {"itaa-1997", "itaa-1936", "gst-1999", "fbt-1986", "taa-1953", "sis-1993",
               "rulings", "private-rulings", "tax-cases", "treaties"} | set(STATE_TAX_ACTS),
    # nz-tax lists every ingested NZ Act, plus the NZ rulings tab.
    "nz-tax": {"nz-it-2007", "nz-taa-1994", "nz-gst-1985", "nz-egda-1968",
               "nz-scd-1971", "nz-kiwisaver-2006", "nz-sl-2011", "nz-rulings"},
    "corporate-asic": {"corporations-act-2001", "regulatory-guides"},
    "bankruptcy": {"bankruptcy-act-1966", "afsa-guides"},
    # reformed regime: the amended Act, the Rules 2025 and the Transitional Rules 2026
    "aml-ctf": {"aml-ctf-2006", "aml-ctf-rules-2025", "aml-ctf-transitional-rules-2026",
                "aml-ctf-rules-2007"},
    "general": {"constitution", "judiciary-act-1903", "adjr-act-1977", "migration-act-1958",
                "ndis-act-2013", "evidence-act-1995", "fair-work-2009", "privacy-act-1988",
                "competition-consumer-2010", "criminal-code-1995"},
    # State taxes (NSW, QLD, TAS, SA, WA duties / land tax / payroll tax) — legislation only
    "state-tax": set(STATE_TAX_ACTS),
}
DEFAULT_SCOPE = "au-tax"


def _resolve_scope(scope: str | None, act: str | None) -> tuple[str, set[str]]:
    """Validate `scope` and, if given, that `act` belongs to it. 422, not an
    empty page, on an invalid scope or a cross-domain act (proposal D4)."""
    resolved = (scope or DEFAULT_SCOPE).strip().lower()
    if resolved not in DOMAIN_ACTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scope '{resolved}'. Valid scopes: {', '.join(sorted(DOMAIN_ACTS))}",
        )
    domain_acts = DOMAIN_ACTS[resolved]
    if act and act not in domain_acts:
        raise HTTPException(
            status_code=422,
            detail=f"act '{act}' is not in scope '{resolved}'",
        )
    return resolved, domain_acts


# --- Graph authority prior --------------------------------------------------
# A section cited/interpreted by many rulings and cases is more likely to be
# the answer than an obscure neighbour with the same text match — but the
# prior must never outweigh relevance. RRF gives a first-place lane hit
# 1/(RRF_K+1) ≈ 0.0164; the prior caps at a quarter of that, so it only
# breaks ties among similarly-ranked sections (search-proposal §3.5).
GRAPH_BOOST = float(os.getenv("GRAPH_BOOST", "0.004"))
GRAPH_DEGREE_CAP = int(os.getenv("GRAPH_DEGREE_CAP", "50"))
GRAPH_DB = Path(os.getenv("GRAPH_DB", str(Path(__file__).resolve().parents[2] / "data" / "graph.db")))

# --- Private-ruling downweighting -------------------------------------------
# Private rulings are numerous (69% of au-tax rows) and lower-signal than
# public rulings (binding only for the applicant). Their RRF contribution is
# scaled down, and on untyped queries no more than a few of the top window
# may be private rulings (search-proposal §3.3, §3.6).
PRIVATE_RULING_WEIGHT = float(os.getenv("PRIVATE_RULING_WEIGHT", "0.35"))
PRIVATE_RULING_CAP = int(os.getenv("PRIVATE_RULING_CAP", "3"))
PRIVATE_RULING_CAP_WINDOW = int(os.getenv("PRIVATE_RULING_CAP_WINDOW", "10"))

# --- Regulatory-guide downweighting ------------------------------------------
# ASIC/AFSA guide paragraphs are numerous and lower-authority than primary
# legislation; their RRF contribution is scaled so Acts rank above guides.
GUIDE_WEIGHT = float(os.getenv("GUIDE_WEIGHT", "0.6"))
# Down-weight for the semantic lane on pure section-reference queries (see the
# comment at the merge loop): that lane is noise for a lookup.
REF_QUERY_VECTOR_WEIGHT = float(os.getenv("REF_QUERY_VECTOR_WEIGHT", "0.25"))

_graph_degree: dict[str, int] | None = None


def _graph_degrees() -> dict[str, int]:
    """{'section:act:id': degree} loaded once. Empty dict if graph.db is unusable."""
    global _graph_degree
    if _graph_degree is None:
        _graph_degree = {}
        try:
            conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True)
            try:
                # ponytail: count in Python — an OR self-join over 331k edges can't
                # use either single-column index.
                deg: dict[int, int] = {}
                for s, t in conn.execute("SELECT source_id, target_id FROM graph_edges"):
                    deg[s] = deg.get(s, 0) + 1
                    deg[t] = deg.get(t, 0) + 1
                for nid, key in conn.execute("SELECT id, key FROM nodes WHERE node_type = 'section'"):
                    _graph_degree[key.lower()] = deg.get(nid, 0)
            finally:
                conn.close()
            logger.info("[graph] loaded degree for %d section nodes from %s", len(_graph_degree), GRAPH_DB)
        except Exception as e:
            logger.warning("[graph] authority boost disabled (%s: %s)", type(e).__name__, e)
    return _graph_degree
MAX_SUGGEST = 12

SECTION_NUMBER_RE = re.compile(r'^[0-9]+(-[0-9]+)?$')

# --- Ruling withdrawal status ------------------------------------------------
# 44% of ruling documents are withdrawn versions; the result has to say so.
# Derived offline by scripts/derive_withdrawal_status.py (meowlaw), keyed by
# document stem (TD_1993_12). Loaded once per process. `section` — the corpus
# reference the ruling page link and the merge key are built from — is never
# changed; the document's own citation (TD 93/12W, ATO ID 2001/1) rides along.
_WITHDRAWAL_PATH = RULING_DIR / "withdrawal_status.json"
_WITHDRAWAL: dict | None = None


def _withdrawal_index() -> dict:
    global _WITHDRAWAL
    if _WITHDRAWAL is None:
        try:
            _WITHDRAWAL = json.loads(_WITHDRAWAL_PATH.read_text())
        except Exception:
            logger.exception("Failed to load %s — ruling status will read 'unknown'", _WITHDRAWAL_PATH)
            _WITHDRAWAL = {}
    return _WITHDRAWAL


def _withdrawal_record(ref: str) -> dict | None:
    """'TD 1993/12', 'TD_1993_12', 'TD 93/12', 'ATO ID 2001/1', 'PSLA 2003/10' → its record."""
    idx = _withdrawal_index()
    s = re.sub(r"[\s/_]+", "_", (ref or "").strip().upper())
    s = re.sub(r"^ATO_?ID_", "AID_", re.sub(r"^PS_?LA_", "PS_LA_", s))
    if s in idx:
        return idx[s]
    m = re.match(r"^([A-Z]+)_(\d{2})_(\d+)$", s)  # two-digit year: TD 93/12
    return idx.get(f"{m[1]}_19{m[2]}_{m[3]}") if m else None


def _cited_ruling(q: str) -> tuple[dict, dict] | None:
    """(record, result row) when the whole query is one ruling citation the
    corpus holds — 'ATO ID 2001/1', 'TD 93/12W', 'PSLA 2003/10' — else None.
    Resolution is an exact key lookup in the withdrawal index, so a partial
    ('ATO ID 2001', 'CR 2002'), a bare number or a topic never fires."""
    rec = _withdrawal_record(re.sub(r"(?<=\d)[Ww]$", "", (q or "").strip()))
    if not rec:
        return None
    # The printed form ('PS LA 2004/11'); a few index records spell it 'TD 2026 D1' / 'TR 98 17'.
    stem = re.sub(r"[\s/]+", "_", rec["derived_ref"])
    ref = _citation_to_display(stem)
    try:
        conn = sqlite3.connect(f"file:{vector_search_service.EMBEDDINGS_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(  # stored as printed, or as the stem ('AID_2001_1')
                # act='ato' is always source_type='ruling'; dropping the redundant filter lets the
                # planner use idx_embeddings_act_section (0.001s vs 22s full-index scan).
                "SELECT id, section_title, embedding_text FROM embeddings WHERE act = 'ato' "
                "AND section IN (?, ?) ORDER BY chunk_index, id LIMIT 1",
                (ref, stem)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("Cited-ruling lookup failed")
        row = None
    if row:  # the shape the vector lane gives the same row
        return rec, {"act": "rulings", "section": ref, "title": row[1] or "", "snippet": (row[2] or "")[:300],
                     "embedding_id": row[0], "source_type": "ruling", "type": "ruling"}
    # 256 old TD/TR documents have no embedding rows; rulings_meta holds every record.
    hit = next((r for r in search_rulings(stem, limit=1) if r.get("citation") == stem), None)
    if not hit:
        return None
    return rec, {**hit, "section": ref, "embedding_id": None, "source_type": "ruling", "type": "ruling"}


# A private ruling authorisation number: 13 digits (3 legacy ids have 12).
_AUTHNUM_RE = re.compile(r"\d{12,13}")


def _cited_pbr(q: str) -> dict | None:
    """The result row when the whole query is a private ruling authorisation
    number the corpus holds ('1012101440404'), else None. Exact key lookup."""
    q = (q or "").strip()
    if not _AUTHNUM_RE.fullmatch(q):
        return None
    try:
        conn = sqlite3.connect(f"file:{vector_search_service.EMBEDDINGS_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, source_type, act, section, section_title, embedding_text FROM embeddings "
                "WHERE act = 'private' AND section = ? ORDER BY chunk_index, id LIMIT 12", (q,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("Private ruling id lookup failed")
        return None
    if not rows:
        return None
    # some first chunks carry no real body ('[Question 1] Yes.'): take the first that does
    row = next((r for r in rows if len(_body_text(r[5])) >= 30), rows[0])
    source_type, act = vector_search_service._map_type(row[1], row[2])
    # the shape the vector lane gives the same row; _finish_snippets turns the text into body
    return {"act": act, "section": row[3], "title": row[4] or f"Private ruling {row[3]}",
            "snippet": row[5] or "", "embedding_id": row[0], "source_type": source_type, "type": source_type}


def _is_public_ruling(item: dict) -> bool:
    return (item.get("act") in ("rulings", "ato") or item.get("source_type") == "ruling"
            or item.get("type") == "ruling")


def _tag_ruling(item: dict) -> dict:
    """Attach withdrawal_status (withdrawn/current/unknown) and, when it differs
    from the corpus reference, own_citation. Non-ruling rows pass through."""
    if not _is_public_ruling(item):
        return item
    rec = _withdrawal_record(item.get("section") or item.get("citation") or "")
    if not rec:
        item["withdrawal_status"] = "unknown"
        return item
    # 'mentions_withdrawal' = the word appears, about another document.
    item["withdrawal_status"] = "withdrawn" if rec["status"] == "withdrawn" else "current"
    if rec.get("citation_alt") and rec["citation_alt"] != rec.get("derived_ref"):
        item["own_citation"] = rec["citation_alt"]
    if (rec.get("derived_ref") or "").startswith("AID "):
        # ATOIDs are one chunk per document with no paragraph numbers — drop the
        # embedder's '[Paragraph]: 1' label so nothing reads as a pinpoint.
        item.pop("paragraph", None)
    return item


# embedding_text opens with metadata sentinels ('[Ruling]: X — title',
# '[Paragraph]: 5', '[Act]: ITAA 1997', '{Court} ...'); some rows (ruling
# summaries) are nothing else. A snippet is body text only. Header lines go
# whole; body tags ('{Facts} The taxpayer...') lose just the tag.
_HEADER_TAGS = r"Ruling|Paragraph|Title|Act|Section|Guide|Case|Summary|References|Court|Defined terms"
_HEADER_LINE = re.compile(rf"^[ \t]*[\[{{](?:{_HEADER_TAGS})[\]}}]:?.*(?:\n|$)", re.M | re.I)
_ANY_TAG = re.compile(rf"[\[{{](?:{_HEADER_TAGS}|Facts|Held|Outcome|Reasoning|Reasons|(?:Question|Answer) \d+)[\]}}]:?[ \t]*",
                      re.I)  # '[Question 1]' / '[Answer 1]': private ruling body tags
SNIPPET_LEN = 300


def _body_text(text: str) -> str:
    return _ANY_TAG.sub("", _HEADER_LINE.sub("", text or "")).strip()


# Extraction furniture around the body: a ruling's HEAD NOTE block (between two
# '=' rules), bare '=' / '-' / '_' rules, a references list (to the next rule or
# the end) and the '[Omitted.]' placeholder of a paragraph the ATO left out.
_HEAD_NOTE = re.compile(r"HEAD\s*NOTE\s*(?:=+.*?=+)?", re.S | re.I)
_REFS_BLOCK = re.compile(r"(?:\bReferences\s+)?(?:Legislative References|Sections? Referenced|Related Rulings)"
                         r"\s*:.*?(?=[=]{3,}|$)", re.S | re.I)
_OMITTED = re.compile(r"\[\s*Omitted\.?\s*\]\.?", re.I)
_RULE = re.compile(r"={3,}|-{3,}|_{3,}|Last updated:\s*[\d-]+(?:\s*\(Compilation \d+\))?")  # + sections_fts footer
_PARA_TAG = re.compile(r"\[Paragraph\]:\s*([\w.]+)")
MIN_SNIPPET = 30  # shorter reads as a stub, not content


def _clean(text: str) -> str:
    t = _RULE.sub(" ", _OMITTED.sub(" ", _REFS_BLOCK.sub(" ", _HEAD_NOTE.sub(" ", _body_text(text)))))
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def _is_content(text: str) -> bool:
    return len(re.sub(r"</?mark>", "", text or "").strip(" .\n")) >= MIN_SNIPPET


def _has_furniture(text: str) -> bool:
    return any(p.search(text) for p in (_HEAD_NOTE, _REFS_BLOCK, _OMITTED, _RULE))


def _finish_snippets(rows: list[dict]) -> list[dict]:
    """Give every row a body-text snippet. A clean snippet (FTS highlight) is
    kept; an empty, title-only or sentinel-bearing one is rebuilt from the row's
    full embedding text, then the document's other chunks, then the ruling's
    rulings_fts content. searchd snippets are the first ~150 chars of
    embedding_text, so a long header line fills them completely — the full
    text has to be re-read, not just stripped."""
    todo = [r for r in rows if not (r.get("snippet") or "").strip() or _ANY_TAG.search(r["snippet"])
            or r["snippet"] == r.get("title")  # a case name alone is not body text
            or _has_furniture(r["snippet"]) or not _is_content(r["snippet"])]
    if not todo:
        return rows
    try:
        conn = sqlite3.connect(f"file:{vector_search_service.EMBEDDINGS_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        logger.exception("Snippet lookup failed")
        conn = None
    try:
        for r in todo:
            r["snippet"] = _body_snippet(conn, r) or r.get("title") or ""
    finally:
        if conn:
            conn.close()
    return rows


def _body_snippet(conn, r: dict) -> str:
    """The first text that reads as content (_is_content), in order: the row's
    own text; the nearest other chunk of the same document (the row's paragraph
    follows it); the ruling's rulings_fts text, head note stripped, so its first
    body sentence; the case summary. Failing all, the row's own text however
    short ('1 Short title' is a whole section), else '' (the caller uses the title)."""
    own = r.get("snippet") or ""
    fallback = _clean(own) if own != r.get("title") else ""
    # a window cut from a references list keeps no prose worth showing: rebuild it
    if (not r.get("embedding_id") and _is_content(fallback) and not _REFS_BLOCK.search(own)) or conn is None:
        return fallback[:SNIPPET_LEN]
    act, section, ci = ("ato" if r.get("act") == "rulings" else r.get("act")), r.get("section"), 0
    if r.get("embedding_id"):
        row = conn.execute("SELECT act, section, embedding_text, chunk_index FROM embeddings WHERE id = ?",
                           (r["embedding_id"],)).fetchone()
        if row:
            act, section, own, ci = row
            fallback = _clean(own)
            # the pinpoint the header carried survives as a field ('AID ' rows drop it in _tag_ruling)
            if (m := _PARA_TAG.search(own)):
                r.setdefault("paragraph", m[1])
            if _is_content(fallback):
                return fallback[:SNIPPET_LEN]
    if act and section:
        for (text,) in conn.execute(
                "SELECT embedding_text FROM embeddings WHERE act = ? AND section = ? "
                "ORDER BY abs(chunk_index - ?), id LIMIT 40", (act, section, ci or 0)):
            body = _clean(text)
            if _is_content(body):
                m = _PARA_TAG.search(text)
                if m:
                    r["paragraph"] = m[1]
                else:
                    r.pop("paragraph", None)
                return body[:SNIPPET_LEN]
            fallback = fallback or body
    if r.get("source_type") == "section" and act and section:  # Acts with no vectors (AML/CTF, Corporations)
        with search_conn() as sconn:
            for (text,) in sconn.execute(
                    "SELECT substr(content, 1, 1500) FROM sections_fts WHERE sections_fts MATCH ? "
                    "AND act = ? AND section = ? LIMIT 1", (f'section : "{section}"', act, section)):
                body = _clean(text)
                if _is_content(body):
                    return body[:SNIPPET_LEN]
                fallback = fallback or body
    if _is_public_ruling(r):
        stem = re.sub(r"[\s/]+", "_", r.get("citation") or section or "")
        with search_conn() as sconn:
            for (text,) in sconn.execute(
                    "SELECT substr(content, 1, 3000) FROM rulings_fts WHERE rulings_fts MATCH ? LIMIT 1",
                    (f'citation : "{stem}"',)):
                body = _clean(text)
                if _is_content(body):
                    return body[:SNIPPET_LEN]
                fallback = fallback or body
    if r.get("source_type") == "case" or r.get("act") == "tax-cases":
        cite = _normalize_citation(r.get("section") or "")
        with search_conn() as sconn:
            for c, text in sconn.execute(
                    "SELECT citation, substr(text, 1, 2000) FROM case_summaries_fts "
                    "WHERE case_summaries_fts MATCH ? LIMIT 5",
                    ('citation : "' + re.sub(r"[^\w ]", "", cite) + '"',)):
                body = _clean(text) if _normalize_citation(c) == cite else ""
                if _is_content(body):
                    return body[:SNIPPET_LEN]
                fallback = fallback or body
    return fallback[:SNIPPET_LEN]


def _drop_twins(rows: list[dict]) -> list[dict]:
    """Show once what a reader cannot tell apart: rows with the same section
    number, paragraph and text ('2A Application of the Criminal Code' in TAA 1953
    and FBTAA 1986, '1 Short title' in every Act). The higher-ranked row stays."""
    seen, out = set(), []
    for r in rows:
        key = (str(r.get("section") or ""), str(r.get("paragraph") or ""),
               re.sub(r"</?mark>|\s+", "", r.get("snippet") or ""))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# "s 326-20 ITAA 1936", "section 8-1 of the ITAA 1997", "s RA-12 NZ Income Tax Act 2007",
# "s 5.2.3 AML/CTF Rules 2007", "s 6(1) ITAA 1936" -> section 6. The act must be named: a bare number is ambiguous.
_SECTION_CITE_RE = re.compile(
    r"^\s*(?:ss?|sec|section)\.?\s*((?=[\w.-]*\d)[0-9A-Za-z]+(?:[-.][0-9A-Za-z]+)*)(?:\(\w+\))*"
    r"\s+(?:of\s+)?(?:the\s+)?(\S.*?)\s*$", re.I)
# The same citation written Act-first — "Income Tax Assessment Act 1997 s 8-1",
# "Duties Act 2001 (Qld), s 10" — which is the order the app itself displays a citation in.
# Anchored at both ends, so a sentence that merely contains a citation is not mistaken for one:
# only the act name plus the section token, nothing else.
_ACT_FIRST_CITE_RE = re.compile(
    r"^\s*(\S.{2,80}?)[,]?\s+(?:ss?|sec|section)\.?\s*((?=[\w.-]*\d)[0-9A-Za-z]+(?:[-.][0-9A-Za-z]+)*)"
    r"(?:\(\w+\))*\s*$", re.I)
_CASE_CITE_RE = re.compile(r"^\s*\[(\d{4})\]\s*([A-Za-z]+)\s+(\d+)\s*$")


def _cited_doc(q: str) -> dict | None:
    """The result row when the whole query is one act-section or case citation
    the corpus holds, else None. Cases match on citation alone — 1,098 case
    rows carry act 'unknown' in embeddings.db, so nothing may depend on it."""
    from backend.resolve_ref import _normalise_act_hint
    from backend.doc_ids import court_name_from_citation
    q = (q or "").strip()
    section, act = None, None
    m = _SECTION_CITE_RE.match(q)
    if m:
        section, act = m[1], _normalise_act_hint(m[2])
    else:
        m2 = _ACT_FIRST_CITE_RE.match(q)
        if m2:
            act, section = _normalise_act_hint(m2[1]), m2[2]
    with search_conn() as conn:
        if act and section:
            row = conn.execute(
                "SELECT act, section, title, part, division FROM sections_meta "
                "WHERE act = ? AND section = ? COLLATE NOCASE", (act, section)).fetchone()
            return {**dict(row), "snippet": "", "embedding_id": None,
                    "source_type": "section", "type": "section"} if row else None
        m = _CASE_CITE_RE.match(q)
        if not m:
            return None
        cite = f"[{m[1]}] {m[2].upper()} {m[3]}"
        row = next((r for r in conn.execute(
            "SELECT citation, case_name, substr(text, 1, 2000) FROM case_summaries_fts "
            "WHERE case_summaries_fts MATCH ? LIMIT 5", (f'citation : "{m[1]} {m[2]} {m[3]}"',))
            if _normalize_citation(r[0]) == cite), None)  # 3 summaries store '1970_HCA_23'
        if not row:  # full FCA/FCAFC/HCA lists: cases without a summary
            row = conn.execute("SELECT citation, case_name, catchwords FROM case_index_meta "
                               "WHERE citation = ?", (cite,)).fetchone()
    if not row:
        return None
    return {"act": "tax-cases", "section": cite, "title": row[1] or "",
            "court": court_name_from_citation(cite), "snippet": _body_text(row[2])[:SNIPPET_LEN],
            "embedding_id": None, "source_type": "case", "type": "case"}


def _afsa_link(item: dict) -> None:
    """AFSA vector rows carry act 'afsa' and a bare paragraph ('8.3') as section;
    the page route is /afsa-guides/<slug>. Point the row at its guide, keeping the
    paragraph. Output-only: the fusion merge key is untouched."""
    item["act"] = "afsa-guides"
    try:
        conn = sqlite3.connect(f"file:{vector_search_service.EMBEDDINGS_DB}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT file_path FROM embeddings WHERE id = ?",
                               (item.get("embedding_id"),)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("AFSA slug lookup failed")
        row = None
    if row:
        item["paragraph"] = item.get("section")
        item["section"] = Path(row[0]).stem


# --- Private rulings search ------------------------------------------------
_PRIVATE_RULINGS_INDEX: dict | None = None
_PRIVATE_RULINGS_INDEX_MTIME: float = 0.0
_PRIVATE_RULINGS_PATH = DATA_DIR / "private_rulings_index.json"


def _is_private_ruling(item: dict) -> bool:
    return item.get("source_type") == "private_ruling" or item.get("act") == "private-rulings"


def _load_private_rulings_index() -> dict:
    global _PRIVATE_RULINGS_INDEX, _PRIVATE_RULINGS_INDEX_MTIME
    try:
        mtime = _PRIVATE_RULINGS_PATH.stat().st_mtime
        if _PRIVATE_RULINGS_INDEX is None or mtime != _PRIVATE_RULINGS_INDEX_MTIME:
            _PRIVATE_RULINGS_INDEX = json.loads(_PRIVATE_RULINGS_PATH.read_text())
            _PRIVATE_RULINGS_INDEX_MTIME = mtime
    except Exception:
        logger.exception("Failed to load private rulings index")
        _PRIVATE_RULINGS_INDEX = _PRIVATE_RULINGS_INDEX or {}
    return _PRIVATE_RULINGS_INDEX


def search_private_rulings(q: str, limit: int = 50, operator: str = "AND",
                            date_from: str | None = None, date_to: str | None = None,
                            outcome: str | None = None) -> list[dict]:
    """Match private rulings by authnum (bare number), year, or name keywords.

    date_from/date_to/outcome are checked per-candidate inside the scan loop,
    before `limit` is reached — a scoped query isn't left choosing from
    whatever happened to land in an unfiltered top-`limit` match list.
    """
    idx = _load_private_rulings_index()
    if not idx:
        return []
    ql = q.strip().lower()
    if not ql:
        return []
    terms = [t for t in ql.split()]
    is_number = ql.isdigit()

    want_outcome = outcome.strip().lower() if outcome else None
    if want_outcome and want_outcome not in private_ruling_outcomes.VALID_OUTCOMES:
        want_outcome = None

    out: list[dict] = []
    for authnum, meta in idx.items():
        if len(out) >= limit:
            break
        name = (meta.get("name") or "").lower()
        year = str(meta.get("year") or "")
        if is_number:
            # Bare number: authnum substring (13-digit EV ids) or exact year
            hit = (len(ql) >= 4 and ql in authnum) or ql == year
        else:
            if operator == "OR":
                hit = any(t in authnum or t in name for t in terms)
            else:
                hit = all(t in authnum or t in name for t in terms)
        if not hit:
            continue
        item_date = meta.get("date_of_advice")
        if date_from and item_date and str(item_date)[:10] < date_from:
            continue
        if date_to and item_date and str(item_date)[:10] > date_to:
            continue
        if want_outcome:
            rec = private_ruling_outcomes.get(authnum) or {}
            if not private_ruling_outcomes.outcome_matches(rec.get("outcome") or "", want_outcome):
                continue
        out.append({
            "act": "private-rulings",
            "section": authnum,
            "title": meta.get("name") or authnum,
            "year": meta.get("year"),
            "date": meta.get("date_of_advice"),
            "snippet": f"Private ruling {authnum}" + (f" — {meta.get('name')}" if meta.get("name") else ""),
            "source_type": "private_ruling",
            "type": "private_ruling",
        })
    return out

# Normalize old-format citations (e.g. "2015_FCAFC_168" → "[2015] FCAFC 168")
CITATION_NORMALIZE_RE = re.compile(r'^(\d{4})_([A-Z]+)_(\d+)$')

def _normalize_citation(s: str) -> str:
    """Convert old filename-style citation to proper format."""
    m = CITATION_NORMALIZE_RE.match(s)
    if m:
        return f"[{m.group(1)}] {m.group(2)} {m.group(3)}"
    return s


# A query that reads as nothing but a section reference — "26AH", "s 160AAB",
# "118-110" — is a lookup, not a topic. Folded into the fusion as just another
# RRF list, a rank-1 citation hit is worth only 1/61 (0.0164) and is routinely
# overtaken by sources that merely *mention* the section.
SECTION_REF_STRIP = ("section ", "sec ", "s ")


def _strip_section_prefix(v: str) -> str:
    v = (v or "").strip().lower()
    for p in SECTION_REF_STRIP:
        if v.startswith(p):
            return v[len(p):].strip()
    return v


def _section_ref(q: str) -> str | None:
    """The section id if the whole query reads as a reference, else None."""
    t = _strip_section_prefix(q)
    if not t or len(t) > 12:
        return None
    if not any(c.isdigit() for c in t):
        return None
    if not all(c.isalnum() or c == "-" for c in t):
        return None
    return t


@router.get("/api/search")
@usage_log.web_logged("web_search", "/api/search", _web_content_type)
def search(request: Request, q: str, act: str | None = None, scope: str | None = None,
           offset: int = 0, limit: int = 50, depth: int = 1, rtype: str | None = None):
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0

    scope, domain_acts = _resolve_scope(scope, act)

    if not SEARCH_DB.exists():
        build_search_index()

    q = q.strip()
    all_results = []
    exact_row = None

    if SECTION_NUMBER_RE.match(q):
        with search_conn() as conn:
            if act:
                exact_row = conn.execute(
                    "SELECT act, section, title, part, division FROM sections_meta WHERE act = ? AND section = ?",
                    (act, q)
                ).fetchone()
            else:
                placeholders = ",".join("?" * len(domain_acts))
                exact_row = conn.execute(
                    f"SELECT act, section, title, part, division FROM sections_meta "
                    f"WHERE section = ? AND act IN ({placeholders})",
                    (q, *domain_acts)
                ).fetchone()
    else:  # "s 326-20 ITAA 1936"
        doc = _cited_doc(q)
        # Named-Act citation overrides the scope tab (exact citations only); an act= filter still binds.
        if doc and doc["type"] == "section" and doc["act"] == (act or doc["act"]):
            exact_row = doc
    if exact_row:
        all_results.append({
            "act": exact_row["act"],
            "section": exact_row["section"],
            "title": exact_row["title"],
            "part": exact_row["part"],
            "division": exact_row["division"],
            "exact_match": True,
        })

    fts_total = 0
    try:
        _fts = fts_search(q, act, limit=min(offset + limit, 100), acts=None if act else domain_acts)
        fts_results = _fts.get("results", [])
        fts_total = _fts.get("total_count", 0)
        for r in fts_results:
            if exact_row and r["act"] == exact_row["act"] and r["section"] == exact_row["section"]:
                continue
            all_results.append(r)
    except Exception:
        logger.exception("FTS search failed")

    if not all_results:
        acts_to_search = [act] if act else sorted(domain_acts & {d.name for d in DATA_DIR.iterdir() if d.is_dir()})
        for a in acts_to_search:
            try:
                tree = load_tree(a)
            except HTTPException:
                continue
            q_lower = q.lower()
            for part in tree.get("parts", []):
                for sec in part.get("sections", []):
                    if q_lower in sec["id"].lower() or q_lower in sec.get("title", "").lower():
                        all_results.append({"act": a, "section": sec["id"], "title": sec.get("title", "")})
                for div in part.get("divisions", []):
                    for sec in div.get("sections", []):
                        if q_lower in sec["id"].lower() or q_lower in sec.get("title", "").lower():
                            all_results.append({"act": a, "section": sec["id"], "title": sec.get("title", "")})
                    for sub in div.get("subdivisions", []):
                        for sec in sub.get("sections", []):
                            if q_lower in sec["id"].lower() or q_lower in sec.get("title", "").lower():
                                all_results.append({"act": a, "section": sec["id"], "title": sec.get("title", "")})

    # ATO rulings are not in sections_meta — they live in their own rulings_fts table — so a
    # search scoped to the rulings tab (act="rulings", the id the UI's rulings tab sends) matched
    # nothing in sections_fts and returned an empty list for every query, including an exact
    # citation.  Read the rulings index for that scope, exactly as /search/flat already does.
    rulings_count = 0
    if scope == "au-tax" and act == "rulings":
        try:
            # CR and PR only when `rtype` names them (search_facets.OPT_IN_SERIES)
            series = (searchd_client.normalize_series(rtype.split(",")) if rtype
                      else search_facets.default_series())
            for r in search_rulings(q, limit=limit, rtype=series):
                all_results.append({
                    "act": "rulings",
                    "section": r["citation"],
                    "title": r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "type": "ruling",
                })
                rulings_count += 1
        except Exception:
            logger.exception("Ruling search failed")

    # Private rulings by authnum / year / name — searchable by bare number.
    # Opt-in: only when the caller names the private-rulings act.
    private_count = 0
    if scope == "au-tax" and act == "private-rulings":
        try:
            for pr in search_private_rulings(q, limit=50):
                if not any(r.get("act") == "private-rulings" and r.get("section") == pr["section"] for r in all_results):
                    all_results.append(pr)
                    private_count += 1
        except Exception:
            logger.exception("Private ruling search failed")

    # A query that is nothing but a ruling citation names one document: it goes first.
    cited = _cited_ruling(q) if scope == "au-tax" and act in (None, "rulings") else None
    if cited:
        rec, row = cited
        all_results = [{**row, "exact_match": True}] + [
            r for r in all_results
            if not (r.get("act") == "rulings" and _withdrawal_record(r.get("section") or "") is rec)]

    # A bare authorisation number names one private ruling: it goes first.
    # PBR id overrides the default-off mask (no act=private-rulings needed).
    pbr = _cited_pbr(q) if scope == "au-tax" and act in (None, "private-rulings") else None
    if pbr:
        same = [r for r in all_results if _is_private_ruling(r) and r.get("section") == pbr["section"]]
        pbr_new = not same
        all_results = [{**(same[0] if same else {}), **pbr, "exact_match": True}] + \
            [r for r in all_results if r not in same]

    # FTS rows are fetched only up to the requested page, so the true total
    # comes from FTS's own count; fall back to len() when the tree-scan
    # fallback populated all_results (fts_total == 0).
    total = (fts_total + (1 if exact_row else 0) + private_count + (1 if cited else 0)
             + (1 if pbr and pbr_new else 0)) if fts_total else len(all_results)
    page = _drop_twins([_tag_ruling(r) for r in _finish_snippets(all_results[offset:offset + limit])])
    engine = "fallback" if not SEARCH_DB.exists() else "fts5"

    # Graph neighbourhood enrichment (spec §6.1): counts + top-3 per edge type.
    # depth is accepted now; depth=2 aggregation lands with serialization (Phase 2).
    aliases: list[dict] = []
    try:
        graph_keys = [f"section:{r['act']}:{r['section']}" for r in page if r.get("act") and r.get("section")]

        # Entity-alias resolution: the raw query and each result's citation/title/name
        # can map to a canonical graph key the FTS text match missed (e.g. a search
        # for "FBTAA section 49" or "Glenn v Federal Commissioner of Land Tax").
        alias_hits: dict[str, str] = {}

        def _probe(ref) -> None:
            if not ref:
                return
            key = alias_lookup(ref)
            if key and key not in alias_hits:
                alias_hits[key] = ref

        _probe(q)
        for r in page:
            for field in ("citation", "title", "name"):
                _probe(r.get(field))

        result_keys = set(graph_keys)
        extra_keys = [k for k in alias_hits if k not in result_keys]
        neigh = graph_neighborhoods(graph_keys + extra_keys) if (graph_keys or extra_keys) else {}
        for r in page:
            g = neigh.get(f"section:{r['act']}:{r['section']}")
            if g:
                r["graph"] = g
        for k in extra_keys:
            g = neigh.get(k)
            if g:
                aliases.append({"ref": alias_hits[k], "key": k, "graph": g})
    except Exception:
        logger.exception("[graph] neighbourhood enrichment failed — returning search without graph field")

    for r in page:
        _tag_content_type(r)
    # ponytail: total is pre-filter; disabled types drop from the page only.
    page = filter_hits(user_from(request), page)
    resp = {"results": page, "total": total, "offset": offset, "limit": limit, "engine": engine}
    if aliases:
        resp["aliases"] = aliases
    return resp


@router.get("/api/search/flat")
@usage_log.web_logged("web_search_flat", "/api/search/flat")
def search_flat(request: Request, q: str, limit: int = 50):
    """Flat-ranked search across legislation sections AND rulings. Single FTS5 query, BM25 order."""
    q = q.strip()
    if not q:
        return {"query": q, "results": []}
    if not SEARCH_DB.exists():
        build_search_index()
    try:
        section_results = fts_search(q, act=None, limit=limit).get("results", [])
        ruling_results = search_rulings(q, limit=limit)

        # Interleave: take from both sources to show mixed results
        combined = []
        sec_idx = 0
        rul_idx = 0
        while len(combined) < limit and (sec_idx < len(section_results) or rul_idx < len(ruling_results)):
            if sec_idx < len(section_results) and (rul_idx >= len(ruling_results) or len(combined) % 2 == 0):
                r = section_results[sec_idx]
                sec_idx += 1
                combined.append({"type": "section", "act": r["act"], "section": r["section"], "title": r.get("title", ""), "snippet": r.get("snippet", "")})
            elif rul_idx < len(ruling_results):
                r = ruling_results[rul_idx]
                rul_idx += 1
                combined.append({"type": "ruling", "act": "rulings", "section": r["citation"], "title": r.get("title", ""), "snippet": r.get("snippet", "")})

        return {"query": q, "results": filter_hits(user_from(request), [_tag_content_type(_tag_ruling(r)) for r in _finish_snippets(combined[:limit])])}
    except Exception as e:
        logger.exception("Flat search failed")
        return {"query": q, "results": [], "error": str(e)}


@router.get("/api/search/suggest")
def search_suggest(request: Request, q: str, limit: int = 12):
    """Quick prefix-based suggestions for autocomplete. Lightweight — titles + section numbers only."""
    q = q.strip()
    if not q or len(q) < 2:
        return {"query": q, "suggestions": []}
    if not SEARCH_DB.exists():
        build_search_index()

    results = []
    with search_conn() as conn:
        # FTS5 prefix match — append * to query for prefix mode
        prefix_q = q.replace('"', '""')
        prefix_q = f'"{prefix_q}"*'

        # Sections
        try:
            rows = conn.execute(
                "SELECT act, section, title, rank FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                (prefix_q, limit)
            ).fetchall()
            for r in rows:
                results.append({
                    "act": r["act"],
                    "section": r["section"],
                    "title": r["title"],
                    "type": "section",
                })
        except Exception:
            pass

        # Rulings
        try:
            rows = conn.execute(
                "SELECT citation, title, rank FROM rulings_fts WHERE rulings_fts MATCH ? ORDER BY rank LIMIT ?",
                (prefix_q, limit)
            ).fetchall()
            for r in rows:
                results.append({
                    "act": "rulings",
                    "section": r["citation"],
                    "title": r["title"],
                    "type": "ruling",
                })
        except Exception:
            pass

    # Deduplicate while preserving FTS5 rank order
    seen: set[tuple[str, str]] = set()
    deduped = []
    for r in results[:limit * 2]:
        key = (r["act"], r["section"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    avail = available_keys(user_from(request))
    deduped = [r for r in deduped if act_key(r["act"]) in avail]
    return {"query": q, "suggestions": [_tag_ruling(r) for r in deduped[:limit]]}


def _pg_case_search(q: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Query the PostgreSQL case database for citation/name matches.

    All values are bound as parameters; date_from/date_to are pushed into the
    SQL WHERE clause, before LIMIT.
    """
    from backend.services.tax_case_sql import _sql_dict
    # Escape LIKE wildcards so a literal % or _ in the query matches literally.
    like_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    _date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    params: list = ["%" + like_q + "%", "%" + like_q + "%"]
    date_clause = ""
    if date_from and _date_re.match(date_from):
        date_clause += " AND decision_date >= %s"
        params.append(date_from)
    if date_to and _date_re.match(date_to):
        date_clause += " AND decision_date <= %s"
        params.append(date_to)
    sql = (
        "SELECT citation, case_name, court, decision_date::text FROM cases "
        f"WHERE (citation ILIKE %s OR case_name ILIKE %s){date_clause} "
        "ORDER BY decision_date DESC LIMIT 20"
    )
    rows = _sql_dict(
        ["citation", "case_name", "court", "decision_date"],
        sql,
        tuple(params),
    )
    results: list[dict] = []
    for r in rows:
        name = r.get("case_name") or ""
        date = r.get("decision_date") or ""
        results.append({
            "act": "tax-cases",
            "section": r.get("citation"),
            "title": name,
            "court": r.get("court") or "",
            "date": date,
            "snippet": f"{name} — Decided {date}" if date else name,
        })
    return results


def _fts_case_search(q: str, operator: str = "AND") -> list[dict]:
    """Search via FTS5 case index for better text matching on case names/summaries."""
    from backend.services.search_service import search_cases_fts
    results: list[dict] = []
    for cr in search_cases_fts(q, limit=20, operator=operator):
        results.append({
            "act": "tax-cases",
            "section": cr["citation"],
            "title": cr["case_name"],
            "court": cr.get("court", ""),
            "snippet": cr.get("case_name", ""),
        })
    return results


def _case_index_search(q: str, operator: str = "AND") -> list[dict]:
    """Search the full FCA/FCAFC/HCA case lists (citation, parallel cites, name,
    catchwords, subject) via the case_index_fts table."""
    results: list[dict] = []
    for cr in search_case_index(q, limit=20, operator=operator):
        results.append({
            "act": "tax-cases",
            "section": cr["citation"],
            "title": cr["case_name"],
            "court": cr.get("court", ""),
            "subject": cr.get("subject", ""),
            "npa": cr.get("npa", ""),
            "year": cr.get("year", ""),
            "snippet": cr.get("snippet", cr.get("case_name", "")),
        })
    return results


def _fuse(q: str, act: str | None, scope: str | None, type: str | None = None,
          operator: str = "AND", date_from: str | None = None, date_to: str | None = None,
          rtype: str | None = None, outcome: str | None = None,
          year_from: int | None = None, year_to: int | None = None, court: str | None = None):
    """Run every lane and fuse them. Returns (scope, ranked_keys, merged, scores, total).
    `act` may be a comma list of sources in the scope."""
    acts_req = {a.strip() for a in act.split(",") if a.strip()} if act else set()
    scope, domain_acts = _resolve_scope(scope, None)
    for a in acts_req:
        _resolve_scope(scope, a)
    # The source set every lane is restricted to: the requested acts if given,
    # else the scope's whole Act set. A lane whose source isn't in it doesn't run.
    lane_acts = acts_req or domain_acts
    # Vector rows for AFSA practice guidance carry act "afsa", not the
    # "afsa-guides" id the scope and UI use.
    vec_acts = lane_acts | ({"afsa"} if "afsa-guides" in lane_acts else set())

    q = q.strip()
    if not SEARCH_DB.exists():
        build_search_index()

    if operator not in ("AND", "OR"):
        operator = "AND"

    # Parse type filter
    type_filter: set[str] | None = None
    if type:
        type_filter = set(type.lower().split(","))

    # Parse ruling series filter
    # Exact series, "PSLA" == "PS LA" — the rule searchd applies (see
    # searchd_client.ruling_series), so every path agrees on a key.
    rtype_set = searchd_client.normalize_series(rtype.split(",")) if rtype else None
    # Class and product rulings are opt-in, like private rulings: a search that
    # names no series gets every series but CR and PR. The mask is non-strict
    # (it constrains public rulings only), so other sources pass through.
    lane_series = rtype_set or search_facets.default_series()

    # Year and court are strict (as in searchd): a row without the value never
    # passes. The lanes get them as a pre-LIMIT narrowing where they can take
    # it; the fused list is then filtered exactly (below).
    courts = {c.strip().upper() for c in court.split(",") if c.strip()} if court else None
    strict = search_facets.Slice(year_from=year_from, year_to=year_to, courts=courts)
    if year_from is not None and not date_from:
        date_from = f"{year_from:04d}-01-01"
    if year_to is not None and not date_to:
        date_to = f"{year_to:04d}-12-31"
    years = ([str(y) for y in range(year_from or 1900, (year_to or datetime.date.today().year) + 1)]
             if strict.years_active else None)

    # The six searches below are independent — run them concurrently so wall
    # time tracks the slowest source (~400ms), not the sum (~850ms).
    from concurrent.futures import ThreadPoolExecutor

    def _run(label: str, fn, default):
        try:
            return fn()
        except Exception:
            logger.exception("%s failed", label)
            return default

    # FTS results are always legislation sections; ruling/private-ruling/case
    # sources are skipped outright when scoped elsewhere or excluded by type —
    # everything else (act, rtype, date range, outcome) is passed into the
    # fetch itself so filtering happens before each source's own LIMIT, not
    # after it on an already-truncated top-50.
    want_section = not type_filter or "section" in type_filter
    want_ruling = not type_filter or "ruling" in type_filter
    # Private rulings are opt-in: ~306k of 373k rows, and loading their index and
    # outcomes enrichment is slow. They are retrieved only when the caller
    # explicitly asks — the "Private rulings" slicer, or the private-rulings act
    # — never on a default/All search. That includes the vector lane.
    want_private = bool(type_filter and "private_ruling" in type_filter) or "private-rulings" in acts_req
    # A request that names CR or PR keeps the retrieval it always had (its vector
    # lane still sees private rulings), and the private rulings it didn't ask for
    # are dropped from the final list instead — so it returns exactly what it did
    # before, minus those rows.
    drop_private_late = bool(rtype_set and rtype_set & search_facets.OPT_IN_SERIES) and not want_private
    if not want_private and not drop_private_late:
        vec_acts = vec_acts - {"private-rulings"}
    want_case = not type_filter or "case" in type_filter
    want_cases = want_case and "tax-cases" in lane_acts
    if courts:  # only cases carry a court
        want_section = want_ruling = want_private = False

    def _vector_source():
        """Vector retrieval for the fusion.

        Prefer searchd: its HNSW walk replaces an exhaustive scan of the whole
        embedding matrix (~124ms, O(N), growing with the corpus) with a graph
        search (~2ms). The rtype filter maps to searchd's ruling-series field,
        applied before retrieval. If searchd is down (None), fall back to the
        Python scan.
        """
        if not vec_acts or _AUTHNUM_RE.fullmatch(q):  # a bare authorisation number has no semantic signal
            return []
        if searchd_client.available():
            got = searchd_client.ann_candidates(
                expand_semantic_query(q), limit=50,
                acts=vec_acts,
                types=type_filter,
                series=lane_series,
                year=years,
                court=courts,
            )
            if got is not None:
                return got
        return vector_search_service.search(
            expand_semantic_query(q), limit=50,
            acts=vec_acts,
            source_types=type_filter,
            series=lane_series,
        )

    with ThreadPoolExecutor(max_workers=7) as _ex:
        _f_fts = _ex.submit(
            _run, "FTS search",
            lambda: fts_search(q, None, limit=50, operator=operator,
                                acts=lane_acts).get("results", [])
            if want_section else [], [])
        _f_rulings = _ex.submit(
            _run, "Ruling FTS search",
            lambda: search_rulings(q, limit=50, operator=operator, rtype=lane_series,
                                    date_from=date_from, date_to=date_to)
            if "rulings" in lane_acts and want_ruling else [], [])
        _f_private = _ex.submit(
            _run, "Private ruling search",
            lambda: search_private_rulings(q, limit=50, operator=operator,
                                            date_from=date_from, date_to=date_to, outcome=outcome)
            if "private-rulings" in lane_acts and want_private else [], [])
        _f_private_fts = _ex.submit(
            _run, "Private ruling FTS search",
            lambda: search_private_rulings_fts(q, limit=50, operator=operator,
                                                date_from=date_from, date_to=date_to, outcome=outcome)
            if "private-rulings" in lane_acts and want_private else [], [])
        _f_vector = _ex.submit(_run, "Vector search", lambda: _vector_source(), [])
        _f_pg = _ex.submit(
            _run, "PostgreSQL case search (non-fatal)",
            lambda: _pg_case_search(q, date_from=date_from, date_to=date_to) if want_cases else [], [])
        _f_fts_case = _ex.submit(
            _run, "FTS5 case search (non-fatal)",
            lambda: _fts_case_search(q, operator) if want_cases else [], [])
        _f_case_index = _ex.submit(
            _run, "Full case-list search (non-fatal)",
            lambda: _case_index_search(q, operator) if want_cases else [], [])

        fts_results = _f_fts.result()
        ruling_results = _f_rulings.result()
        private_results = _f_private.result()
        fts_private = _f_private_fts.result()
        vector_results = _f_vector.result()
        pg_case_results = _f_pg.result()
        fts_case_results = _f_fts_case.result()
        case_index_results = _f_case_index.result()

    # CDN-0117: body term search — merge FTS hits over private ruling content
    # with the metadata matcher results, deduping by authnum.
    seen_auth = {r["section"] for r in private_results}
    for pr in fts_private:
        if pr["section"] not in seen_auth:
            private_results.append(pr)
            seen_auth.add(pr["section"])

    scores: dict[tuple[str, str], float] = {}
    merged: dict[tuple[str, str], dict] = {}

    # A pure section-reference query is a lookup, not a topic. The vector lane
    # has no signal for it: every private ruling sits within ~0.37-0.41 cosine
    # of "26AH" (a 0.04 spread across 200 rows), so ordering inside that band is
    # arbitrary. That noise is what put "Living-away-from-home allowance"
    # rulings at #4/#5 of a query about life-assurance bonuses. Scale that lane
    # down when the query is a reference; the lexical lanes and the +1.0 exact
    # boost below carry the answer.
    ref = _section_ref(q)

    for rank, r in enumerate(fts_results):
        key = (r["act"], r["section"])
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
        merged.setdefault(key, {**r, "embedding_id": None, "source_type": "section"})

    for rank, r in enumerate(vector_results):
        # Normalize citation for case-type results before key computation
        vr = {**r}
        if vr.get("source_type") == "case" or vr.get("act") == "tax-cases":
            vr["section"] = _normalize_citation(vr.get("section", ""))
        elif vr.get("source_type") == "ruling" and "_" in vr.get("section", ""):
            vr["section"] = _citation_to_display(vr["section"])  # 'AID_2001_1' -> the ruling lane's key
        key = (vr["act"], vr["section"])
        w = GUIDE_WEIGHT if vr.get("source_type") in ("regulatory_guide", "afsa_guide") else 1.0
        if ref and _strip_section_prefix(vr.get("section", "")) != ref:
            w *= REF_QUERY_VECTOR_WEIGHT
        scores[key] = scores.get(key, 0.0) + w / (RRF_K + rank + 1)
        existing = merged.setdefault(key, {**vr})
        existing.setdefault("embedding_id", vr["embedding_id"])
        existing.setdefault("snippet", vr["snippet"])
        existing["source_type"] = vr.get("source_type", "section")

    for rank, r in enumerate(fts_case_results):
        r["section"] = _normalize_citation(r.get("section", ""))
        key = (r["act"], r["section"])
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
        merged.setdefault(key, {**r, "source_type": "case", "type": "case"})

    for rank, r in enumerate(case_index_results):
        r["section"] = _normalize_citation(r.get("section", ""))
        key = (r["act"], r["section"])
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
        existing = merged.setdefault(key, {**r, "source_type": "case", "type": "case"})
        # carry subject/npa/fields from the full-list row even when a summary
        # row already claimed the key
        for f in ("subject", "npa", "year", "court"):
            if r.get(f) and not existing.get(f):
                existing[f] = r[f]

    for rank, r in enumerate(pg_case_results):
        r["section"] = _normalize_citation(r.get("section", ""))
        key = (r["act"], r["section"])
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
        merged.setdefault(key, {**r, "source_type": "case", "type": "case"})

    for rank, r in enumerate(ruling_results):
        key = (r["act"], r["section"])
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
        merged.setdefault(key, {**r, "embedding_id": None, "source_type": "ruling"})

    for rank, r in enumerate(private_results):
        key = (r["act"], r["section"])
        # Downweighted (proposal §3.3): private rulings are numerous and
        # lower-signal (binding only for the applicant), so a rank-1 private
        # ruling hit is worth less than a rank-1 public hit.
        scores[key] = scores.get(key, 0.0) + PRIVATE_RULING_WEIGHT / (RRF_K + rank + 1)
        merged.setdefault(key, {**r, "embedding_id": None})

    # Citation-style queries ("118-110", "s 6(1)") — exact/prefix hits on the
    # section id, folded in as just another RRF list.
    if not type_filter or "section" in type_filter:
        try:
            for rank, r in enumerate(search_section_ids(q, None, limit=20, acts=lane_acts)):
                key = (r["act"], r["section"])
                scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank + 1)
                merged.setdefault(key, {**r, "embedding_id": None, "source_type": "section"})
        except Exception:
            logger.exception("Section-id search failed (non-fatal)")

    if GRAPH_BOOST:
        degrees = _graph_degrees()
        norm = math.log(1 + GRAPH_DEGREE_CAP)
        for key in scores:
            if merged[key].get("source_type") != "section":
                continue
            d = degrees.get(f"section:{key[0]}:{key[1]}".lower(), 0)
            if d:
                # Capped at GRAPH_BOOST regardless of degree — about a quarter
                # of one first-place RRF hit (1/61 ≈ 0.0164), so degree can
                # break ties but never outrank relevance (proposal §3.5).
                scores[key] += GRAPH_BOOST * min(1.0, math.log(1 + d) / norm)

    # An exact section reference is decisive, not merely one more RRF list.
    # Measured before this change on this corpus: `26AH` returned ITAA 1936
    # s 160AAB first and s 26AH third, because prose *mentioning* the section
    # outscored the citation match itself. 1.0 is ~61x a rank-1 RRF
    # contribution, so nothing that merely mentions the section can overtake it.
    ref = _section_ref(q)
    if ref:
        for key in scores:
            if _strip_section_prefix(key[1]) == ref:
                scores[key] += 1.0
                merged[key]["exact"] = True

    ranked_keys = sorted(scores, key=lambda k: -scores[k])
    if strict.years_active or courts:
        # skip the dimensions the lanes already applied — only year/court are checked
        ranked_keys = [k for k in ranked_keys if strict.passes(
            search_facets.row_dims(merged[k]), skip=("types", "acts", "series", "outcome"))]
    total = len(ranked_keys)

    # date_from/date_to and outcome are applied inside each source's own fetch
    # (search_rulings, search_private_rulings[_fts], _pg_case_search), before
    # that source's LIMIT — not here on the already-fused, already-truncated
    # list. See backend/services/search_service.py and this module's fetchers.

    # Private-ruling cap (proposal §3.6): on an untyped query, at most
    # PRIVATE_RULING_CAP of the top PRIVATE_RULING_CAP_WINDOW fused results
    # may be private rulings — further ones slide down, in their fused order,
    # past the window. Not applied when the query is explicitly scoped to
    # private rulings only.
    if not (type_filter and type_filter == {"private_ruling"}):
        window = ranked_keys[:PRIVATE_RULING_CAP_WINDOW]
        rest = ranked_keys[PRIVATE_RULING_CAP_WINDOW:]
        kept, deferred, pr_count = [], [], 0
        for key in window:
            if merged[key].get("source_type") == "private_ruling":
                if pr_count < PRIVATE_RULING_CAP:
                    kept.append(key)
                    pr_count += 1
                else:
                    deferred.append(key)
            else:
                kept.append(key)
        ranked_keys = kept + deferred + rest

    # A query that is nothing but a ruling citation ("ATO ID 2001/1", "CR 2002/71")
    # names one document. As bag-of-words it loses to its neighbours — every ATO ID
    # opens with "ATO ID <year>/", CR/TR numbers repeat across years — so every row
    # of that document goes above the fused list. Nothing else moves.
    cited = _cited_ruling(q) if "rulings" in lane_acts and want_ruling else None
    if cited:
        rec, row = cited
        pinned = [k for k in ranked_keys if k[0] == "rulings" and _withdrawal_record(k[1]) is rec]
        if not pinned and not (rtype_set or date_from or date_to):
            # The lanes missed it; inject it — unless a filter may have excluded it.
            pinned = [("rulings", row["section"])]
            merged[pinned[0]], scores[pinned[0]] = dict(row), 0.0
            total += 1
        for k in pinned:
            merged[k]["exact_match"] = True
        ranked_keys = pinned + [k for k in ranked_keys if k not in pinned]

    # Same for an act-section or case citation ("s 326-20 ITAA 1936", "[1905] HCA 57").
    doc = None if cited else _cited_doc(q)
    # Named-Act citation overrides the scope tab (exact citations only); an explicit act= filter still binds.
    if doc and (want_section if doc["type"] == "section" else want_cases) and \
            (doc["act"] in lane_acts or not acts_req or doc["type"] == "case") and \
            (not (strict.years_active or courts) or strict.passes(
                search_facets.row_dims(doc), skip=("types", "acts", "series", "outcome"))):
        k = (doc["act"], doc["section"])
        if k not in merged:
            merged[k], scores[k] = doc, 0.0
            total += 1
        merged[k]["exact_match"] = True
        ranked_keys = [k] + [x for x in ranked_keys if x != k]

    # A bare authorisation number ("1012101440404") names one private ruling.
    # PBR id overrides the default-off mask: pinned without the Private rulings slicer.
    pbr = _cited_pbr(q) if "private-rulings" in lane_acts and \
        (not type_filter or "private_ruling" in type_filter) else None
    if pbr:
        k = (pbr["act"], pbr["section"])
        if k in merged or not (date_from or date_to or outcome or strict.years_active or courts):
            if k not in merged:  # the lanes missed it (default-off); inject it unless a filter may exclude it
                scores[k] = 0.0
                total += 1
            # the embedding row's text, not the index lane's "Private ruling <n> — name" stub
            merged[k] = {**merged.get(k, {}), **pbr, "exact_match": True}
            ranked_keys = [k] + [x for x in ranked_keys if x != k]

    if drop_private_late:
        ranked_keys = [k for k in ranked_keys if not _is_private_ruling(merged[k])]
        total = len(ranked_keys)
    return scope, ranked_keys, merged, scores, total


@router.get("/api/search/hybrid")
@usage_log.web_logged("web_search_hybrid", "/api/search/hybrid", _web_content_type)
def search_hybrid(request: Request, q: str, act: str | None = None, scope: str | None = None,
                  limit: int = 20, type: str | None = None,
                  offset: int = 0, operator: str = "AND",
                  date_from: str | None = None, date_to: str | None = None,
                  rtype: str | None = None, outcome: str | None = None,
                  year_from: int | None = None, year_to: int | None = None,
                  court: str | None = None):
    if limit > 50:
        limit = 50
    if offset < 0:
        offset = 0
    scope, ranked_keys, merged, scores, total = _fuse(
        q, act, scope, type, operator, date_from, date_to, rtype, outcome, year_from, year_to, court)

    # No cross-encoder rerank — ranking authority comes from the graph
    # authority prior above (proposal §4.2, decided 29 Sep 2026).
    ranked_keys = ranked_keys[offset:offset + limit]
    results = []
    for key in ranked_keys:
        r = merged[key]
        r["fusion_score"] = scores[key]
        emb_id = r.get("embedding_id")
        r["cross_references"] = vector_search_service.get_cross_references(emb_id) if emb_id else []
        if _is_private_ruling(r):
            private_ruling_outcomes.enrich(r)
        if r.get("act") == "afsa":
            _afsa_link(r)
        results.append(r)
    results = [_tag_content_type(r) for r in _drop_twins([_tag_ruling(r) for r in _finish_snippets(results)])]
    results = filter_hits(user_from(request), results)

    return {"results": results, "total": total, "offset": offset, "limit": limit,
            "meta": {"reranked": False, "scope": scope}}


@router.get("/api/search/facets")
def search_facets_route(request: Request, q: str, scope: str | None = None, operator: str = "AND",
                        act: str | None = None, type: str | None = None, rtype: str | None = None,
                        outcome: str | None = None, year_from: int | None = None,
                        year_to: int | None = None, court: str | None = None):
    """Slicer counts for the search screen, over the app's own candidate pool.

    The pool is every row a default search for `q` fuses in `scope`, plus the
    rows each opt-in request returns (type=private_ruling; rtype=CR; rtype=PR). Each option counts
    the pool rows that pass every *other* active slicer (search_facets.facets).
    The slicer params mirror /api/search/hybrid; `act` takes a comma list.
    """
    from concurrent.futures import ThreadPoolExecutor
    scope, _ = _resolve_scope(scope, None)
    # one run per request a chip sends, so an opt-in's count is what selecting it returns
    runs = ({}, {"type": "private_ruling"}, *({"rtype": s} for s in sorted(search_facets.OPT_IN_SERIES)))
    with ThreadPoolExecutor(max_workers=len(runs)) as ex:
        outs = list(ex.map(lambda kw: _fuse(q, None, scope, operator=operator, **kw), runs))
    pool: dict = {}
    for _, keys, merged, _, _ in outs:
        for k in keys:
            pool.setdefault(k, merged[k])
    # Counts must not reveal disabled types: drop their rows before counting anything.
    avail = available_keys(user_from(request))
    pool = {k: r for k, r in pool.items()
            if (hit_key(_tag_content_type(dict(r))) or "legislation") in avail}
    sl = search_facets.Slice(
        types=set(type.lower().split(",")) if type else None,
        acts=set(act.split(",")) if act else None,
        series=searchd_client.normalize_series(rtype.split(",")) if rtype else None,
        outcome=outcome.strip().lower() if outcome else None,
        year_from=year_from, year_to=year_to,
        courts={c.strip().upper() for c in court.split(",") if c.strip()} if court else None,
    )
    facets = search_facets.facets(pool.values(), sl)
    # §7.2: the same type counts grouped by registry content_type (types kept for the UI chips)
    by_ct: dict = {}
    for v in facets["types"]:
        ct = content_type_for(v["value"])
        by_ct[ct] = by_ct.get(ct, 0) + v["count"]
    facets["content_types"] = [{"value": k, "count": n}
                               for k, n in sorted(by_ct.items(), key=lambda x: (-x[1], x[0] or ""))]
    return {"facets": facets, "scope": scope,
            # additive: per-source counts over the whole library, not the drawn pool
            "library": search_facets.library(
                q, frozenset(a for a in DOMAIN_ACTS[scope] if act_key(a) in avail), operator)}


# ── Warm-up ──────────────────────────────────────────────────────────────────
# The first search after a restart pays ~2-3s of one-time lazy init (searchd/ANN,
# embed client, FTS, DB pool, fusion). The search screen calls this on mount so a
# throwaway query through the real hybrid path absorbs it instead of the user's
# first search. Session-gated like the rest of /api/* (it costs an OpenAI embed).
_warm_lock = threading.Lock()
_warmed = False


@router.get("/api/search/warm")
def search_warm(request: Request, response: Response):
    global _warmed
    response.headers["Cache-Control"] = "no-store"
    try:
        with _warm_lock:  # concurrent first-callers wait, then see _warmed and skip
            if _warmed:
                return {"ok": True, "warmed": False, "skipped": True, "ms": 0}
            t0 = time.perf_counter()
            # Default path only: private rulings are opt-in, so warming them here
            # would load their index on every process start for nothing.
            search_hybrid(request, q="income tax", limit=5)
            _warmed = True
            ms = int((time.perf_counter() - t0) * 1000)
        logger.info("[search-warm] throwaway warm-up query done in %dms (not user traffic)", ms)
        return {"ok": True, "warmed": True, "skipped": False, "ms": ms}
    except Exception as e:
        logger.warning("[search-warm] warm-up failed (not user traffic): %s: %s", type(e).__name__, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
