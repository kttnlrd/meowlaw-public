"""Document ID grammar for the lawkitty MCP (spec §3).

Stable, lowercase, URL-safe IDs. Every tool returns them; ``fetch``, ``outline``
and ``related`` accept them. IDs never change between compilations.

ID forms
--------
    leg:<act>:<section>        legislation section       leg:itaa-1997:8-1
    leg:<act>:<section>(<n>)   subsection                leg:itaa-1997:855-10(1)
    case:<year>-<court>-<n>    case                      case:2024-hca-1
    ruling:<series>-<year>-<n> public ruling             ruling:tr-2024-1
    pbr:<authnum>              private ruling            pbr:1051234567890
    rg:<n>                     regulatory guide          rg:146
    dta:<country>:<article>     treaty article            dta:usa:7
"""

from __future__ import annotations

import re

# Canonical act slugs — hyphenated, matching the data registry
# (backend/routes/search.py DOMAIN_ACTS). Published in server instructions + get_info().
#: State tax Acts (practice area "State taxes", search scope ``state-tax``): slug -> (title, jurisdiction).
#: Source, licence and attribution for each are in data/<slug>/licence.json.
STATE_TAX_ACTS: dict[str, tuple[str, str]] = {
    "qld-duties-2001": ("Duties Act 2001", "Qld"),
    "qld-landtax-2010": ("Land Tax Act 2010", "Qld"),
    "qld-payrolltax-1971": ("Payroll Tax Act 1971", "Qld"),
    "tas-duties-2001": ("Duties Act 2001", "Tas"),
    "tas-landtax-2000": ("Land Tax Act 2000", "Tas"),
    "tas-payrolltax-2008": ("Payroll Tax Act 2008", "Tas"),
    "sa-stampduties-1923": ("Stamp Duties Act 1923", "SA"),
    "sa-landtax-1936": ("Land Tax Act 1936", "SA"),
    "sa-payrolltax-2009": ("Payroll Tax Act 2009", "SA"),
    "wa-duties-2008": ("Duties Act 2008", "WA"),
    "wa-landtaxassessment-2002": ("Land Tax Assessment Act 2002", "WA"),
    "wa-payrolltaxassessment-2002": ("Pay-roll Tax Assessment Act 2002", "WA"),
    "nsw-duties-1997": ("Duties Act 1997", "NSW"),
    "nsw-landtaxmgmt-1956": ("Land Tax Management Act 1956", "NSW"),
    "nsw-landtax-1956": ("Land Tax Act 1956", "NSW"),
    "nsw-payrolltax-2007": ("Payroll Tax Act 2007", "NSW"),
}

ACT_SLUGS: tuple[str, ...] = (
    "itaa-1997", "itaa-1936", "gst-1999", "fbt-1986", "taa-1953", "sis-1993",
    "nz-it-2007", "corporations-act-2001", "aml-ctf-2006", "aml-ctf-rules-2007",
    *STATE_TAX_ACTS,
)


def state_tax_act_names() -> dict[str, str]:
    """Lower-case act names a user may type -> slug. Jurisdiction-qualified forms always resolve
    ("duties act 2001 (qld)", "qld duties act 2001"); a bare title only when no other State uses it
    ("duties act 2001" is both Qld and Tas, so it stays ambiguous)."""
    out: dict[str, str] = {}
    bare: dict[str, list[str]] = {}
    for slug, (title, jur) in STATE_TAX_ACTS.items():
        t, j = title.lower(), jur.lower()
        full = {"qld": "queensland", "tas": "tasmania", "sa": "south australia", "wa": "western australia",
                "nsw": "new south wales"}[j]
        for name in (f"{t} ({j})", f"{t} {j}", f"{j} {t}", f"{t} ({full})", f"{full} {t}", slug):
            out[name] = slug
        bare.setdefault(t, []).append(slug)
    out.update({t: s[0] for t, s in bare.items() if len(s) == 1})
    return out

# Non-legislation collections — never valid as a ``leg:`` act slug.
COLLECTIONS: tuple[str, ...] = (
    "rulings", "private-rulings", "tax-cases", "treaties", "regulatory-guides",
)

_LEG_RE = re.compile(r"^leg:(?P<act>[a-z0-9-]+):(?P<section>[0-9a-z.-]+?)(?:\((?P<sub>\d+)\))?$")
_CASE_RE = re.compile(r"^case:(?P<year>\d{4})-(?P<court>[a-z]+)-(?P<n>\d+)$")
_RULING_RE = re.compile(r"^ruling:(?P<series>[a-z0-9]+)-(?P<year>\d{4})-(?P<n>\d+)$")
_PBR_RE = re.compile(r"^pbr:(?P<authnum>\d+)$")
_RG_RE = re.compile(r"^rg:(?P<n>\d+)(?::(?P<para>\d+(?:\.\d+)?[a-z]?))?$")
_AFSA_RE = re.compile(r"^afsa:(?P<slug>[a-z0-9-]+)(?::(?P<para>\d+\.\d+))?$")
_DTA_RE = re.compile(r"^dta:(?P<country>[a-z][a-z-]*):(?P<article>.+)$")

# Canonical court display names, keyed by medium-neutral court code. Derived
# from the citation (authoritative), never from the stored summary `court`
# field (which carries stale labels like "Administrative Appeals Tribunal"
# for ARTA decisions and many FCAFC spelling variants).
COURT_DISPLAY_NAMES: dict[str, str] = {
    "ARTA": "Administrative Review Tribunal",
    "AATA": "Administrative Appeals Tribunal",
    "AAT": "Administrative Appeals Tribunal",
    "HCA": "High Court of Australia",
    "FCA": "Federal Court of Australia",
    "FCAFC": "Full Court of the Federal Court of Australia",
}
_CITATION_RE = re.compile(
    r"\[(\d{4})\]\s*([A-Z]+(?: [A-Z]+)*)\s*(\d+(?:[-–]\d+)*)", re.IGNORECASE
)


def court_name_from_citation(citation: str) -> str:
    """Return the canonical court display name for a medium-neutral citation,
    or '' when the citation has no recognisable court code."""
    m = _CITATION_RE.search(citation or "")
    if not m:
        return ""
    return COURT_DISPLAY_NAMES.get(m.group(2).upper(), "")



class DocId:
    """A parsed document ID: its type plus its canonical string form."""

    __slots__ = ("type", "id")

    def __init__(self, type_: str, id_: str) -> None:
        self.type = type_
        self.id = id_

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"DocId({self.type!r}, {self.id!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DocId) and (self.type, self.id) == (other.type, other.id)


def parse_id(raw: str) -> DocId | None:
    """Parse a lawkitty document ID; return None if it is not a valid ID."""
    s = (raw or "").strip().lower()
    for pat, type_ in (
        (_LEG_RE, "legislation"),
        (_CASE_RE, "case"),
        (_RULING_RE, "ruling"),
        (_PBR_RE, "private_ruling"),
        (_RG_RE, "rg"),
        (_AFSA_RE, "afsa"),
        (_DTA_RE, "treaty"),
    ):
        if pat.match(s):
            return DocId(type_, s)
    return None


def is_id(raw: str) -> bool:
    return parse_id(raw) is not None


# --- serializers ----------------------------------------------------------

def leg_id(act: str, section: str, subsection: str | None = None) -> str:
    base = f"leg:{act.lower()}:{section}"
    return f"{base}({subsection})" if subsection else base


def case_id(citation: str) -> str:
    """Build case:<year>-<court>-<n> from a medium-neutral citation '[2024] HCA 1'."""
    m = re.search(r"\[(\d{4})\]\s*([A-Za-z]+)\s*(\d+)", citation)
    if not m:
        raise ValueError(f"not a medium-neutral citation: {citation!r}")
    return f"case:{m.group(1)}-{m.group(2).lower()}-{m.group(3)}"


def ruling_id(series: str, year: int, n: int) -> str:
    return f"ruling:{series.lower().replace(' ', '')}-{year}-{n}"


def pbr_id(authnum: str) -> str:
    return f"pbr:{authnum}"


def rg_id(n: int, para: str | None = None) -> str:
    return f"rg:{n}" + (f":{para}" if para else "")


def afsa_id(slug: str, para: str | None = None) -> str:
    return f"afsa:{slug}" + (f":{para}" if para else "")


def dta_id(country: str, article: str) -> str:
    return f"dta:{country.lower().replace(' ', '-')}:{article}"
