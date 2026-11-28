"""Resolve a document ID or citation to a canonical document (spec §4.3 fetch).

``resolve_ref(ref)`` accepts either a lawkitty document ID (``leg:itaa-1997:8-1``,
``case:2024-hca-1``, ``ruling:tr-2024-1``, ``pbr:1051234567890``, ``rg:146``,
``dta:gb:7``) or a human citation (``s 8-1 ITAA 1997``, ``TR 2024/1``,
``[2024] HCA 1``).

Returns a normalised document dict with at least ``id``, ``type``, ``citation``
and ``text``, or ``{"error": {"code", "message"}}``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
from pathlib import Path

from backend.doc_ids import parse_id, case_id, leg_id, ruling_id, court_name_from_citation, state_tax_act_names
from backend.services import data_loader
from backend.services.case_db_service import (
    get_case_metadata,
    load_case_summary,
    load_case_manifest,
    load_case_index_meta,
    load_case_list_date,
    clean_case_citations,
    lookup_case_alias,
    austlii_url_for,
)
from backend.services.private_ruling_outcomes import get as get_pbr_outcome
# Citation-only lookup, NOT the route handler (needs a Request since d9ce7de5b). The MCP
# gate is _deny_resolved on the returned type + jurisdiction.
from backend.routes.rulings import _ruling_payload as _get_ruling, _is_nz_citation
from backend.routes.regulatory_guides import get_regulatory_guide as _get_rg
from backend.routes.treaties import get_treaty_article as _get_treaty

# --- citation normalisation ------------------------------------------------

_CASE_RE = re.compile(r"\[(\d{4})\]\s*([A-Z]+(?: [A-Z]+)*)\s*(\d+(?:[-–]\d+)*)", re.IGNORECASE)
_CASE_BRACKETLESS_RE = re.compile(r"(\d{4})\s+([A-Z]+(?: [A-Z]+)*)\s+(\d+(?:[-–]\d+)*)", re.IGNORECASE)
_RULING_RE = re.compile(r"\b([A-Z]{2,4})\s*(\d{2}|\d{4})\s*/\s*(\d+)\b", re.IGNORECASE)
_SECTION_CITATION_RE = re.compile(
    r"\b(?:s(?:ec(?:tion)?)?|subs(?:ection)?)\.?\s*(\d[\w.\-]*|(?-i:[A-Z]{2})[ -]?\d+[A-Z]*)(?:\((\d+)\))?(?=[\s,]|$)", re.IGNORECASE
)


def normalise_case_citation(raw: str) -> str | None:
    m = _CASE_RE.search(raw)
    if m:
        return f"[{m.group(1)}] {m.group(2).upper()} {m.group(3)}"
    m = _CASE_BRACKETLESS_RE.search(raw)
    if m:
        return f"[{m.group(1)}] {m.group(2).upper()} {m.group(3)}"
    return None


# Court code → canonical display name: see backend.doc_ids.court_name_from_citation.



_RULING_LOOSE_RE = re.compile(r"^([A-Za-z]+(?:[ _][A-Za-z]+)?)[ _]*(\d{2}|\d{4})\s*[/_]\s*(\d+)$")


def normalise_ruling_citation(raw: str) -> str | None:
    m = _RULING_RE.search(raw)
    if not m:
        return None
    series, year, n = m.group(1).upper(), m.group(2), m.group(3)
    if len(year) == 2:  # 97 -> 1997, 04 -> 2004 (matches data_loader.load_rulings)
        year = f"19{year}" if int(year) >= 90 else f"20{year}"
    return f"{series} {year}/{n}"


# --- resolvers -------------------------------------------------------------

def _err(code: str, message: str, suggestions: list[str] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "suggestions": suggestions or []}}


# Case summaries are machine-generated and the full judgment is not hosted, so
# `text` must not be mistaken for the primary-source judgment.
_GENERATED_SUMMARY_NOTE = (
    "[AI-GENERATED SUMMARY — machine-generated, not the judgment text. "
    "The full judgment is not hosted here; follow source_url to the court record.]"
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_DATE_IN_NAME_RE = re.compile(
    r"\((\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\)"
)


def _date_from_case_name(name: str) -> str | None:
    """Extract an ISO date from a trailing ``(13 March 2026)`` in a case name."""
    m = _DATE_IN_NAME_RE.search(name or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"


def _coerce_compilation_no(value) -> str | None:
    """Return a real compilation number, or None when unknown.

    Section frontmatter (and some act trees) carry a literal ``0`` as a
    placeholder for 'not recorded' — never surface that as a compilation number.
    Valid values are returned unchanged (they are strings in the source data).
    """
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "0"):
        return None
    return s


# Last data refresh date — surfaced as `as_at` on every primary-source result.
AS_AT = os.getenv("LAWKITTY_AS_AT", "2026-04-01")

# Per-call point-in-time request (CDN-0298). A ContextVar so the resolvers can read the
# caller's date without every signature growing an argument.
from contextvars import ContextVar  # noqa: E402
_REQUESTED_AS_AT: ContextVar[str | None] = ContextVar("requested_as_at", default=None)

_ASAT_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_ASAT_MONTHS.update({k[:3]: v for k, v in list(_ASAT_MONTHS.items())})
_ISO_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_source_date(value: str) -> str | None:
    """'21 April 2026' / '21 Apr 2026' / '2026-04-21' → ISO 'YYYY-MM-DD'."""
    s = (value or "").strip()
    if not s:
        return None
    if _ISO_DATE_ONLY_RE.match(s):
        return s
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})$", s)
    if m:
        month = _ASAT_MONTHS.get(m.group(2).lower())
        if month:
            return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
    return None


def _as_at_on_or_after(source_date: str) -> str | None:
    """as_at (data-refresh date) must not predate the source document (LK-31).

    ASIC republishes guides after our last data refresh (RG 1 carries date
    '21 April 2026' while AS_AT defaulted to '2026-04-01'), so clamp as_at up to
    the document date; return None when neither date can be established.
    """
    doc = _parse_source_date(source_date)
    base = (AS_AT or "").strip()
    base = base if _ISO_DATE_ONLY_RE.match(base) else None
    if doc and base:
        return max(base, doc)
    return doc or base


@functools.lru_cache(maxsize=None)
def _paragraph_slugs(act: str) -> dict[str, str]:
    """"4-200" -> section slug, from data/<act>/section_index.json (commentary only)."""
    if not data_loader.is_plain_id(act):
        return {}
    try:
        rows = json.loads((data_loader.DATA_DIR / act / "section_index.json").read_text())
        return {r["paragraph"].lstrip("¶").strip().upper(): r["id"]
                for r in rows if isinstance(r, dict) and r.get("paragraph") and r.get("id")}
    except Exception:
        return {}


def _resolve_legislation(act: str, section: str, subsection: str | None = None,
                         requested_as_at: str | None = None) -> dict | None:
    act = data_loader.resolve_act_id(act)
    try:
        fm, body = data_loader.get_act_section_content(act, section)
    except Exception:
        # Commentary (MTG, GST guide, NZ MTG) is filed by slug; accept its ¶ number too.
        slug = _paragraph_slugs(act).get(section.strip().lstrip("¶").strip().upper())
        # NZ IT Act 2007 sections are stored "CB-1"; accept "CB1", "cb 1", "CB_1".
        nz = re.fullmatch(r"([A-Za-z]{2})[\s_-]*(\d+[A-Za-z]*)", section.strip()) \
            if act.startswith("nz-") else None
        slug = slug or (f"{nz.group(1).upper()}-{nz.group(2).upper()}" if nz else None)
        if not slug:
            return None
        try:
            fm, body = data_loader.get_act_section_content(act, slug)
        except Exception:
            return None
    canonical = fm.get("section") or section
    text = body
    if subsection is not None:
        extracted = _extract_subsection(body, canonical, subsection)
        if extracted:
            text = extracted
        else:
            # Can't isolate the subsection — return the whole section but do
            # NOT echo a subsection id we didn't honour.
            subsection = None
    # A body that stops mid-sentence is a corpus defect (the text ends on an internal
    # subheading), not a cut by this call. Say so, because a silent short body looks complete.
    # Measure the whole stored body: a subsection legitimately ends without a full stop, so
    # testing `text` flagged every pinpoint fetch as incomplete.
    body_incomplete = bool(body.strip()) and not re.search(r'[.\\)"\'!?]\s*$', body.strip())
    extra: dict = {}
    if body_incomplete:
        extra["body_incomplete"] = True
        extra["body_incomplete_note"] = (
            "The stored text for this section stops mid-sentence, usually at an internal "
            "subheading. Nothing was cut by this call; the corpus text itself is incomplete. "
            "Treat the tail as missing and check the source PDF before relying on it."
        )
    if requested_as_at:
        comp_date = str(fm.get("compilation_date") or "")
        extra["as_at_requested"] = requested_as_at
        # Never compare date strings lexicographically: the corpus carries both '2025-07-01' and
        # '1 July 2025'. Parse both (None when unparseable) and only compare parsed values.
        req_iso = _parse_source_date(requested_as_at)
        comp_iso = _parse_source_date(comp_date)
        if req_iso:
            extra["as_at_requested"] = req_iso
        if req_iso and comp_iso and req_iso < comp_iso:
            extra["as_at_warning"] = (
                f"as_at={requested_as_at} predates the held compilation ({comp_date}). "
                f"Historical compilations are not held, so the text returned is LATER than "
                f"requested. Point-in-time retrieval is not satisfied by this response."
            )
        elif comp_date:
            extra["as_at_note"] = (f"Text is compilation {fm.get('compilation_no')} of "
                                   f"{comp_date}, in force for {requested_as_at}.")
    return {
        **fm,
        "id": leg_id(act, canonical, subsection),
        "type": "legislation",
        "act": act,
        "section": canonical,
        "compilation_no": _coerce_compilation_no(fm.get("compilation_no")),
        "citation": f"s {canonical}{f'({subsection})' if subsection else ''} {fm.get('act', act)}",
        "as_at": fm.get("compilation_date") or AS_AT,
        "text": text,
        **({"licence": lic} if (lic := data_loader.licence_notice(act)) else {}),
        **extra,
    }


def _extract_subsection(body: str, section: str, subsection: str) -> str:
    """Return a single subsection's text, split on its `id="s<section>-<n>"` anchor."""
    anchor = f'id="s{section}-{subsection}"'
    idx = body.find(anchor)
    if idx == -1:
        m = re.search(
            rf'id="s{re.escape(section)}-{re.escape(subsection)}"', body, re.IGNORECASE)
        if not m:
            return ""
        idx = m.start()
        anchor = m.group(0)
    rest = body[idx + len(anchor):]
    # Skip the remainder of the anchor tag (`></a>`).
    gt = rest.find('>')
    if gt != -1:
        rest = rest[gt + 1:]
    # The anchor's closing `</a>` (and any other stray tags at the seam) must
    # not leak into the subsection text.
    rest = re.sub(r"^\s*(?:</a>\s*)+", "", rest)
    nxt = re.search(rf'id="s{re.escape(section)}-\d+[A-Za-z]*"', rest)
    text = rest[:nxt.start()] if nxt else rest
    # We cut immediately before the next anchor's `id=`, which can leave a
    # partial opening tag (e.g. `<a `) at the tail — strip it and any trailing
    # stray tags so the text ends on prose.
    text = re.sub(r"<[^>]*$", "", text)
    return text.strip()


JUDGMENT_COLLECTION = "case-law"


def _resolve_case(citation: str) -> dict | None:
    """A case's metadata, with the full judgment as its text when the case-law collection holds it."""
    result = _resolve_case_meta(citation)
    if not result or result.get("type") != "case":
        return result
    try:
        _fm, body = data_loader.get_act_section_content(
            JUDGMENT_COLLECTION, case_id(citation).split(":", 1)[1])
    except Exception:
        return result
    if "## Judgment" not in (body or ""):
        return result
    # text is now the judgment itself (metadata block, "---", "## Judgment", reasons): the AI summary,
    # when there is one, stays in "summary" untouched and no longer describes text.
    return {**result, "text": body, "collection": JUDGMENT_COLLECTION, "text_source": "judgment",
            "summary_is_generated": False}


def _resolve_case_meta(citation: str) -> dict | None:
    summary = load_case_summary(citation)
    manifest = load_case_manifest(citation)

    if summary is None and manifest is None:
        # Full case-list index first (covers the ~71k FCA/FCAFC/HCA judgments
        # that have no AI summary and no tax-manifest entry), then legacy Postgres.
        ci = load_case_index_meta(citation)
        if ci is not None:
            return {
                "id": case_id(citation),
                "type": "case",
                "citation": citation,
                "case_name": ci.get("case_name", ""),
                "court": court_name_from_citation(citation) or ci.get("court", ""),
                "decision_date": load_case_list_date(citation),
                "judges": [],
                "outcome": "",
                "subject": ci.get("subject", ""),
                "npa": ci.get("npa", ""),
                "catchwords": ci.get("catchwords", ""),
                "parallel_citations": ci.get("parallel_citations", []),
                "source_url": ci.get("austlii_url", ""),
                "as_at": AS_AT,
                "text": "",
            }
        # Legacy fallback: an internal Postgres (absent in lawkitty).
        data = get_case_metadata(citation, include_legislation_refs=True)
        if data is None:
            return None
        urls = data.get("download_urls") or {}
        source_url = urls.get("austlii_url") or urls.get("own_url") or urls.get("fed_court_url") or ""
        return {
            **data,
            "id": case_id(citation),
            "type": "case",
            "citation": citation,
            "source_url": source_url,
            "as_at": AS_AT,
            "text": data.get("content", ""),
        }

    case_name = (summary or {}).get("case_name") or (manifest or {}).get("title") or ""
    court = court_name_from_citation(citation) or (summary.get("court", "") if summary else "")
    judges = (summary.get("judges") if summary else None) or []
    # Summary `date` first, then a date in the case name (ARTA/AAT names carry
    # "(13 March 2026)"), then the case-list record.
    decision_date = (
        (summary.get("date") if summary else None)
        or _date_from_case_name(case_name)
        or load_case_list_date(citation)
    )
    outcome = summary.get("outcome", "") if summary else ""
    legislation_refs = summary.get("legislation_cited", []) if summary else []
    cases_cited = summary.get("cases_cited", []) if summary else []

    return {
        "id": case_id(citation),
        "type": "case",
        "citation": citation,
        "case_name": case_name,
        "court": court,
        "decision_date": decision_date,
        "judges": judges,
        "outcome": outcome,
        "source_url": austlii_url_for(citation),
        "as_at": AS_AT,
        "summary": summary,
        "summary_is_generated": summary is not None,
        "text": (
            f"{_GENERATED_SUMMARY_NOTE}\n\n{_case_summary_text(summary)}"
            if summary else ""
        ),
        "legislation_refs": legislation_refs,
        "case_citations": clean_case_citations(cases_cited),
    }


def _case_summary_text(summary: dict) -> str:
    parts: list[str] = []
    if summary.get("facts"):
        parts.append(f"Facts\n{summary['facts']}")
    if summary.get("issues"):
        parts.append("Issues\n" + "\n".join(f"- {i}" for i in summary["issues"]))
    if summary.get("held"):
        parts.append(f"Held\n{summary['held']}")
    if summary.get("reasoning"):
        parts.append(f"Reasoning\n{summary['reasoning']}")
    if summary.get("outcome"):
        parts.append(f"Outcome\n{summary['outcome']}")
    return "\n\n".join(parts)


def _resolve_ruling(citation: str) -> dict | None:
    try:
        data = _get_ruling(citation)
    except Exception:
        return None
    if not data or data.get("ok") is False:
        return None
    # _deny_resolved keys NZ rulings off this field; the payload doesn't carry it.
    data = {**data, "jurisdiction": "NZ" if _is_nz_citation(citation) else "AU"}
    citation = data.get("citation") or citation
    # Multi-word series (PS LA, BR Pub), stems (TR_2024_1) and NZ 2-digit years.
    m = _RULING_LOOSE_RE.match(citation.strip())
    doc_id = ""
    if m:
        year = m.group(2)
        if len(year) == 2:
            year = f"19{year}" if int(year) >= 90 else f"20{year}"
        doc_id = ruling_id(re.sub(r"[\s_]", "", m.group(1)).upper(), int(year), int(m.group(3)))
    stem = "_".join(doc_id.split(":", 1)[1].split("-")).upper() if doc_id else ""
    full_text = _load_ruling_full_text(citation, stem)
    return {
        **data,
        "id": doc_id,
        "type": "ruling",
        "citation": citation,
        "source_url": data.get("ato_url", ""),
        "as_at": AS_AT,
        "text": full_text or data.get("body", "") or data.get("ruling", ""),
    }


def _load_ruling_full_text(citation: str, stem: str = "") -> str:
    """Load the full ruling body: data/rulings/<citation>.txt, else <stem>.txt in the AU or
    NZ ruling dir (NZ display citations like "BR Pub 24/01" are stored as BRPUB_2024_1.txt)."""
    try:
        from backend.config import RULING_DIR, NZ_RULING_DIR
        normalized = re.sub(r"[\s/]+", "_", citation).strip("_")
        for path in (Path(RULING_DIR) / f"{normalized}.txt", Path(RULING_DIR) / f"{stem}.txt",
                     Path(NZ_RULING_DIR) / f"{stem}.txt"):
            if path.stem and path.exists():
                return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


def _resolve_private_ruling(authnum: str) -> dict | None:
    outcome = get_pbr_outcome(authnum)
    if outcome is None:
        return None
    full_text = _load_pbr_full_text(authnum)
    return {
        **outcome,
        "id": f"pbr:{authnum}",
        "type": "private_ruling",
        "authnum": authnum,
        "citation": f"private ruling {authnum}",
        "weight": "Private ruling. Applies only to the applicant. Not precedent.",
        "source_url": f"https://www.ato.gov.au/law/view/print?DocID=EV/{authnum}&PiT=99991231235958",
        "as_at": AS_AT,
        "text": full_text,
    }


def _load_pbr_full_text(authnum: str) -> str:
    """Load the full edited PBR body from the private-rulings corpus JSON."""
    try:
        from backend.config import PRIVATE_RULINGS_DIR
        path = PRIVATE_RULINGS_DIR / f"{authnum}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("formatted_text", "") or ""
    except Exception:
        pass
    return ""


def _resolve_rg(n: int, para: str | None = None) -> dict | None:
    from backend.routes.regulatory_guides import get_rg_paragraph, get_rg_paragraphs
    if para is not None:
        # ASIC numbers paragraphs "RG 179.1"; also accept the short form rg:179:1.
        p = get_rg_paragraph(n, para) or get_rg_paragraph(n, f"{n}.{para}")
        if p is None:
            return None
        para = p["paragraph"]
        return {
            "id": f"rg:{n}:{para}",
            "type": "rg",
            "citation": f"RG {n} [{para}]",
            "title": p.get("title", ""),
            "body": p.get("text", ""),
            "rg_number": n,
            "paragraph": para,
            "as_at": AS_AT,
        }
    try:
        data = _get_rg(n)
    except Exception:
        return None
    if not data:
        return None
    result = {
        **data,
        "id": f"rg:{n}",
        "type": "rg",
        "citation": f"RG {n}",
        "source_url": data.get("page_url", "") or data.get("download_url", ""),
        # LK-31: ASIC republishes guides after our last data refresh, so `as_at`
        # must be clamped to be on/after the guide's own date.
        "as_at": _as_at_on_or_after(data.get("date", "")),
    }
    paras = get_rg_paragraphs(n)
    if paras:
        result["paragraphs"] = [p["num"] for p in paras]
        result["paragraph_count"] = len(paras)
    return result


def _resolve_afsa(slug: str, para: str | None = None) -> dict | None:
    from backend.routes.afsa_guides import (
        get_afsa_guide as _get_afsa,
        get_afsa_paragraph,
        get_afsa_paragraphs,
    )
    if para is not None:
        p = get_afsa_paragraph(slug, para)
        if p is None:
            return None
        para = p["paragraph"]
        return {
            "id": f"afsa:{slug}:{para}",
            "type": "afsa",
            "citation": f"AFSA {slug} [{para}]",
            "title": p.get("title", ""),
            "body": p.get("text", ""),
            "slug": slug,
            "paragraph": para,
            "as_at": AS_AT,
        }
    try:
        data = _get_afsa(slug)
    except Exception:
        return None
    if not data:
        return None
    result = {
        **data,
        "id": f"afsa:{slug}",
        "type": "afsa",
        "citation": slug,
        "source_url": data.get("page_url", "") or data.get("download_url", ""),
        "as_at": AS_AT,
    }
    paras = get_afsa_paragraphs(slug)
    if paras:
        result["paragraphs"] = [p["num"] for p in paras]
        result["paragraph_count"] = len(paras)
    return result


def _resolve_treaty(iso2: str, article: str) -> dict | None:
    try:
        data = _get_treaty(iso2, article)
    except Exception:
        return None
    if not data:
        return None
    return {
        **data,
        "id": f"dta:{iso2}:{article}",
        "type": "treaty",
        "country": iso2,
        "article": article,
        "as_at": AS_AT,
    }


_ID_RESOLVERS = {
    "legislation": lambda d: _resolve_legislation(*_split_leg(d.id), requested_as_at=_REQUESTED_AS_AT.get()),
    "case": lambda d: _resolve_case(_case_id_to_citation(d.id)),
    "ruling": lambda d: _resolve_ruling(_ruling_id_to_citation(d.id)),
    "private_ruling": lambda d: _resolve_private_ruling(d.id.split(":", 1)[1]),
    "rg": lambda d: _resolve_rg(*_split_rg(d.id)),
    "afsa": lambda d: _resolve_afsa(*_split_afsa(d.id)),
    "treaty": lambda d: _resolve_treaty(*d.id.split(":", 1)[1].split(":", 1)),
}


def _split_leg(id_: str) -> tuple[str, str, str | None]:
    body = id_.split(":", 2)[2]
    m = re.match(r"(.+?)\((\d+)\)$", body)
    if m:
        return id_.split(":")[1], m.group(1), m.group(2)
    return id_.split(":")[1], body, None


def _split_rg(id_: str) -> tuple[int, str | None]:
    parts = id_.split(":")
    return int(parts[1]), parts[2] if len(parts) > 2 else None


def _split_afsa(id_: str) -> tuple[str, str | None]:
    parts = id_.split(":")
    return parts[1], parts[2] if len(parts) > 2 else None


def _case_id_to_citation(id_: str) -> str:
    year, court, n = id_.split(":", 1)[1].split("-", 2)
    return f"[{year}] {court.upper()} {n}"


def _ruling_id_to_citation(id_: str) -> str:
    series, year, n = id_.split(":", 1)[1].split("-", 2)
    return f"{series.upper()} {year}/{n}"


def resolve_ref(ref: str, requested_as_at: str | None = None) -> dict:
    """Resolve a reference. requested_as_at is honoured per call (the ContextVar is the
    transport for the MCP layer, but a direct caller's argument must win)."""
    if requested_as_at is None:
        return _resolve_ref_impl(ref)
    _tok = _REQUESTED_AS_AT.set(requested_as_at)
    try:
        return _resolve_ref_impl(ref)
    finally:
        _REQUESTED_AS_AT.reset(_tok)


def _resolve_ref_impl(ref: str) -> dict:
    """Resolve an ID or citation to a document, or an error dict."""
    raw = (ref or "").strip()
    if not raw:
        return _err("invalid_param", "ref is required", ["pass an ID or citation"])

    docid = parse_id(raw)
    if docid is not None:
        resolver = _ID_RESOLVERS.get(docid.type)
        if resolver is None:
            return _err("invalid_param", f"unsupported id type {docid.type!r}")
        result = resolver(docid)
    else:
        result = _resolve_citation(raw)

    if result is None:
        return _err("not_found", f"no document for {raw!r}")
    return result


def _resolve_citation(raw: str) -> dict:
    # Case citation
    norm = normalise_case_citation(raw)
    if norm:
        result = _resolve_case(norm)
        if result is not None:
            return result
    # Ruling citation
    norm = normalise_ruling_citation(raw)
    if norm:
        result = _resolve_ruling(norm)
        if result is not None:
            return result
    # Shapes the single-token regex above mangles: "PS LA 2011/12" (-> "LA 2011/12"),
    # "BR Pub 24/01" (NZ display form), "TR_2024_1" (stem). _ruling_payload normalises these.
    if _RULING_LOOSE_RE.match(raw):
        for cand in (raw, re.sub(r"^([A-Za-z]+)\s+([A-Za-z]+)(?=\s*\d)", r"\1\2", raw)):
            result = _resolve_ruling(cand)
            if result is not None:
                return result
    # Section citation: "s 8-1 ITAA 1997" -> act + section
    m = _SECTION_CITATION_RE.search(raw)
    if m:
        section, subsection = m.group(1), m.group(2)
        act_hint = re.sub(r"^\s*(?:of\s+)?(?:the\s+)?", "", _SECTION_CITATION_RE.sub("", raw, count=1), flags=re.IGNORECASE).strip()
        act = _normalise_act_hint(act_hint)
        if act is not None:
            result = _resolve_legislation(act, section, subsection,
                                         requested_as_at=_REQUESTED_AS_AT.get())
            if result is not None:
                return result
            return _err("not_found", f"section {section} not found in {act}")
        # bare section citation: never auto-resolve to one act (spec §4.3)
        candidates = _find_section_across_acts(section)
        if not candidates:
            return _err("not_found", f"no document for {raw!r}")
        return _err("ambiguous_ref", f"{raw!r} is ambiguous; name the act", [c["id"] for c in candidates])
    # Bare NZ section code: "CB 1" / "CB1" / "CB-1" / "CB_1" -> nz-it-2007 subpart section.
    # NZ subpart lettering (two letters + number) is distinctive and unambiguous in the
    # corpus, so it auto-resolves where a bare numeric section would not (spec §4.3).
    nz = re.fullmatch(r"([A-Za-z]{2})[\s_-]?(\d+[A-Za-z]?)", raw)
    if nz:
        result = _resolve_legislation(
            "nz-it-2007", f"{nz.group(1).upper()}-{nz.group(2).upper()}", None,
            requested_as_at=_REQUESTED_AS_AT.get())
        if result is not None:
            return result
    # Collection citation aliases (data/<id>/aliases.json, e.g. "MTG ¶25-320").
    from backend.services.collections import alias_lookup
    for cid, doc in alias_lookup(raw):
        result = _resolve_legislation(cid, doc, None, requested_as_at=_REQUESTED_AS_AT.get())
        if result is not None:
            return result
    # Party-name alias (e.g. "Ready Mixed Concrete" -> "[1969] HCA 12").
    alias_citation = lookup_case_alias(raw)
    if alias_citation:
        result = _resolve_case(alias_citation)
        if result is not None:
            return result
    return _err("not_found", f"no document for {raw!r}")


_ACT_HINT_MAP: dict[str, str] = {
    "itaa 1997": "itaa-1997", "income tax assessment act 1997": "itaa-1997", "1997 act": "itaa-1997",
    "itaa 1936": "itaa-1936", "income tax assessment act 1936": "itaa-1936", "1936 act": "itaa-1936",
    "gst act": "gst-1999", "goods and services tax act": "gst-1999",
    "fbt act": "fbt-1986", "fringe benefits tax": "fbt-1986",
    "taa 1953": "taa-1953", "taxation administration act": "taa-1953",
    "corps act": "corporations-act-2001", "corporations act": "corporations-act-2001", "corporations act 2001": "corporations-act-2001",
    "aml ctf": "aml-ctf-2006", "aml/ctf": "aml-ctf-2006",
    "nz it act": "nz-it-2007", "nz income tax act": "nz-it-2007",
    "sis act": "sis-1993", "aml/ctf act": "aml-ctf-2006", "aml/ctf rules": "aml-ctf-rules-2007",
    **state_tax_act_names(),
}


def _normalise_act_hint(hint: str) -> str | None:
    """Resolve an act name/abbreviation fragment to a canonical slug, or None."""
    h = re.sub(r"\s+", " ", hint).strip().lower().strip(".,;").strip()
    h = re.sub(r"\s*\(cth\)", "", h)
    # try the full hint first ("itaa 1997"), then retry with a trailing year stripped
    # ("corporations act 2001" -> "corporations act").
    for cand in (h, re.sub(r"\s+\d{4}$", "", h)):
        slug = _ACT_HINT_MAP.get(cand)
        if slug is None:
            slug = data_loader.resolve_act_id(cand)
        if slug in data_loader_act_slugs():
            return slug
    return None


def _find_section_across_acts(section: str) -> list[dict]:
    """Return matching documents for a section number across all acts."""
    from backend.doc_ids import ACT_SLUGS
    found: list[dict] = []
    for slug in ACT_SLUGS:
        r = _resolve_legislation(slug, section,
                                 requested_as_at=_REQUESTED_AS_AT.get())
        if r is not None:
            found.append(r)
    return found


def data_loader_act_slugs() -> set[str]:
    from backend.doc_ids import ACT_SLUGS
    return set(ACT_SLUGS)


def warm_caches() -> None:
    """Load the one-off caches the first `fetch` would otherwise build (rulings index, case-alias
    map). Idempotent and result-neutral: they are lru_cache loaders the resolvers already call."""
    from backend.services.case_db_service import case_alias_map
    from backend.services.data_loader import load_rulings

    for loader in (load_rulings, case_alias_map):
        try:
            loader()
        except Exception:
            logging.getLogger(__name__).exception("fetch cache warm failed: %s", loader.__name__)
