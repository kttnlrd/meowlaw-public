"""Search slicer semantics shared by /api/search/hybrid and /api/search/facets.

Two jobs:

1. Default-off sources. Class rulings (CR), product rulings (PR) and private
   binding rulings (PBR, type `private_ruling`) are searched only when the caller
   names them: CR/PR via `rtype`, PBR via `type=private_ruling`. A bare search
   never returns them.

2. Facet counts. Each option's count is the number of rows in the app's own
   fused candidate pool (every row the lanes of a default search fuse, plus the
   rows the CR/PR/PBR opt-ins would add) that pass every *other* active slicer.
   Counts are over that pool, never the corpus. `library()` (below) is the
   corpus-wide counterpart: documents per source that contain the words. The year/court derivations are
   the ones searchd/build_index.py stamps on its rows.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache

from backend.services import private_ruling_outcomes
from backend.services.searchd_client import ruling_series

OPT_IN_SERIES = frozenset({"CR", "PR"})
OPT_IN_TYPES = frozenset({"private_ruling"})

# 'TR 2010/1' / 'AID_2001_1' / 'TR 98/17' -> 2010 / 2001 / 1998; 'IT 2346' is undated.
_RULING_YEAR_RE = re.compile(r"^[A-Za-z ]+[ _]((?:19|20)?\d\d)[/_]")
# '[2010] HCA 57' / '2010_HCA_57' -> ('2010', 'HCA')
_CASE_RE = re.compile(r"^\[?((?:18|19|20)\d\d)\]?[ _]([A-Z]+)[ _]")


def ruling_year(ref: str) -> str:
    m = _RULING_YEAR_RE.match((ref or "").strip())
    if not m:
        return ""
    y = m.group(1)
    return y if len(y) == 4 else ("19" if y >= "50" else "20") + y


@lru_cache(maxsize=1)
def all_series() -> frozenset:
    """Every public-ruling series the rulings index holds."""
    from backend.services.search_service import search_conn
    with search_conn() as conn:
        rows = conn.execute("SELECT citation FROM rulings_meta").fetchall()
    return frozenset(s for s in (ruling_series(r[0]) for r in rows) if s)


def default_series() -> set:
    """The rtype mask for a search that names no series: all but CR and PR."""
    return set(all_series() - OPT_IN_SERIES)


def row_dims(r: dict) -> dict:
    st = r.get("source_type") or r.get("type") or "section"
    act = r.get("act") or ""
    d = {"type": st, "act": "afsa-guides" if act == "afsa" else act,
         "series": "", "outcome": "", "year": "", "court": ""}
    ref = r.get("section") or ""
    if st == "private_ruling" or act == "private-rulings":
        d["type"] = "private_ruling"
        rec = private_ruling_outcomes.get(ref) or {}
        d["outcome"] = (rec.get("outcome") or "").strip().lower()
        y = str(rec.get("date_of_advice") or r.get("date") or r.get("year") or "")[:4]
        d["year"] = y if y.isdigit() else ""
    elif st == "ruling" or act == "rulings":
        d["type"] = "ruling"
        d["series"] = ruling_series(ref)
        d["year"] = ruling_year(ref)
    elif st == "case" or act == "tax-cases":
        m = _CASE_RE.match(ref.strip())
        if m:
            d["year"], d["court"] = m.group(1), m.group(2)
    return d


@dataclass
class Slice:
    """The active slicers. None = not set (types/series then mean the default)."""
    types: set | None = None
    acts: set | None = None
    series: set | None = None
    outcome: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    courts: set | None = None

    @property
    def years_active(self) -> bool:
        return self.year_from is not None or self.year_to is not None

    def passes(self, d: dict, skip: tuple = ()) -> bool:
        if "types" not in skip:
            if self.types is not None and d["type"] not in self.types:
                return False
            # naming the private-rulings source is the same opt-in as the type
            if self.types is None and d["type"] in OPT_IN_TYPES and \
                    not (self.acts and d["act"] in self.acts):
                return False
        if "acts" not in skip and self.acts is not None and d["act"] not in self.acts:
            return False
        # series and outcome constrain only the rows they describe (public / private rulings)
        if "series" not in skip and d["type"] == "ruling":
            if self.series is not None and d["series"] not in self.series:
                return False
            if self.series is None and d["series"] in OPT_IN_SERIES:
                return False
        if "outcome" not in skip and self.outcome and d["type"] == "private_ruling":
            if d["outcome"] != self.outcome:
                return False
        # year and court are strict: a row without the value never passes
        if "year" not in skip and self.years_active:
            if not d["year"]:
                return False
            y = int(d["year"])
            if (self.year_from is not None and y < self.year_from) or \
               (self.year_to is not None and y > self.year_to):
                return False
        if "court" not in skip and self.courts is not None and d["court"] not in self.courts:
            return False
        return True


# dimension -> how choosing one value of it sets the Slice
_CHOOSE = {
    "type": lambda v: {"types": {v}},
    "act": lambda v: {"acts": {v}},
    "series": lambda v: {"series": {v}},
    "outcome": lambda v: {"outcome": v},
    "year": lambda v: {"year_from": int(v), "year_to": int(v)},
    "court": lambda v: {"courts": {v}},
}


def _count(dims: list, sl: Slice, key: str) -> list:
    """Per value: the pool rows that would show if that value alone were chosen
    in its dimension, every other active slicer unchanged."""
    c = Counter()
    for v in {d[key] for d in dims if d[key]}:
        chosen = replace(sl, **_CHOOSE[key](v))
        n = sum(1 for d in dims if d[key] == v and chosen.passes(d))
        if n:
            c[v] = n
    return [{"value": v, "count": n} for v, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def facets(rows, sl: Slice) -> dict:
    """{types, acts, series, outcome, year, court, scope, pool} — searchd's shape.
    Values with no rows are omitted."""
    dims = [row_dims(r) for r in rows]
    return {
        "types": _count(dims, sl, "type"),
        "acts": _count(dims, sl, "act"),
        "series": _count(dims, sl, "series"),
        "outcome": _count(dims, sl, "outcome"),
        "year": _count(dims, sl, "year"),
        "court": _count(dims, sl, "court"),
        "scope": "candidates",
        "pool": len(dims),
    }


# ── Library counts ───────────────────────────────────────────────────────────
# The pool counts above describe the candidates one search drew. These describe
# the library: per source, how many documents contain the query's words (the
# same FTS5 match each lexical lane runs, without the lane's LIMIT). The vector
# lane can still return documents that match by meaning only, so a 0 here means
# "no document contains these words", not "selecting this returns nothing".

_LEGISLATION_SKIP = {"rulings", "private-rulings", "tax-cases", "treaties",
                     "regulatory-guides", "afsa-guides"}


def _quoted(q: str, operator: str) -> str:
    """The rulings / private-rulings / cases / guides lanes' MATCH string."""
    out = []
    for tok in q.split():
        if tok.endswith("*") and len(tok) > 1:
            out.append('"' + tok[:-1].replace('"', '""') + '"*')
        else:
            out.append('"' + tok.replace('"', '""') + '"')
    return (" OR " if operator == "OR" else " ").join(out)


def _listed(c: Counter) -> list:
    return [{"value": v, "count": n} for v, n in sorted(c.items(), key=lambda x: (-x[1], x[0])) if n]


@lru_cache(maxsize=256)
def library(q: str, domain_acts: frozenset, operator: str = "AND") -> dict:
    """{types, acts, series, year, court, all_series, scope: "library"} for `q`
    over the sources in `domain_acts`. `types.ruling` counts the default series
    (all but CR/PR); `series` counts every series, CR and PR included."""
    from backend.services.search_service import _expand_query, search_conn
    q = (q or "").strip()
    operator = "OR" if operator == "OR" else "AND"
    types, acts, series, years, courts = Counter(), Counter(), Counter(), Counter(), Counter()
    if not q:
        return {"types": [], "acts": [], "series": [], "year": [], "court": [],
                "all_series": sorted(all_series()), "scope": "library"}
    match = _quoted(q, operator)
    leg = sorted(domain_acts - _LEGISLATION_SKIP)

    def count(sql, *args, m=None):
        try:
            return conn.execute(sql, (match if m is None else m, *args)).fetchall()
        except Exception:  # an FTS syntax the lane would also reject
            return []

    with search_conn() as conn:
        # sections: search_sections' own count, by act (a fully quoted query is
        # its literal LIKE match; anything else its synonym-expanded MATCH)
        if leg:
            ph = re.match(r'^\s*"(.+)"\s*$', q)
            where = ("(content LIKE ? OR title LIKE ?)" if ph else "sections_fts MATCH ?") + \
                f" AND act IN ({','.join('?' * len(leg))}) GROUP BY act"
            args = (f"%{ph.group(1)}%", f"%{ph.group(1)}%") if ph else (_expand_query(q, operator),)
            for a, n in count("SELECT act, COUNT(*) FROM sections_fts WHERE " + where,
                              *args[1:], *leg, m=args[0]):
                acts[a] = n
            types["section"] = sum(acts.values())
        if "rulings" in domain_acts:
            for s, y, n in count(
                    "SELECT ruling_series(m.citation), m.year, COUNT(*) FROM rulings_fts "
                    "JOIN rulings_meta m ON rulings_fts.citation = m.citation "
                    "WHERE rulings_fts MATCH ? GROUP BY 1, 2"):
                series[s] += n
                if s not in OPT_IN_SERIES:
                    types["ruling"] += n
                    if y:
                        years[str(y)] += n
        if "private-rulings" in domain_acts:
            types["private_ruling"] = sum(r[0] for r in count(
                "SELECT COUNT(*) FROM private_rulings_fts WHERE private_rulings_fts MATCH ?"))
        if "tax-cases" in domain_acts:
            cites = {r[0] for r in count(
                "SELECT citation FROM case_index_fts WHERE case_index_fts MATCH ?")}
            cites |= {r[0] for r in count(
                "SELECT citation FROM case_summaries_fts WHERE case_summaries_fts MATCH ?")}
            types["case"] = len(cites)
            for c in cites:
                m = _CASE_RE.match(c.strip())
                if m:
                    years[m.group(1)] += 1
                    courts[m.group(2)] += 1
        for act, table, t in (("regulatory-guides", "regulatory_guides_fts", "regulatory_guide"),
                              ("afsa-guides", "afsa_guides_fts", "afsa_guide")):
            if act in domain_acts:
                types[t] = sum(r[0] for r in count(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?"))
    return {"types": _listed(types), "acts": _listed(acts), "series": _listed(series),
            "year": _listed(years), "court": _listed(courts),
            "all_series": sorted(all_series()), "scope": "library"}


if __name__ == "__main__":
    assert [ruling_year(r) for r in ("TR 2010/1", "AID_2001_1", "TR 98/17", "IT 2346", "")] == \
        ["2010", "2001", "1998", "", ""]
    assert row_dims({"act": "tax-cases", "section": "[1997] AATA 391", "source_type": "case"})["court"] == "AATA"
    rows = [{"act": "rulings", "section": "CR 2002/71", "source_type": "ruling"},
            {"act": "rulings", "section": "TR 2010/1", "source_type": "ruling"},
            {"act": "itaa-1997", "section": "8-1", "source_type": "section"},
            {"act": "tax-cases", "section": "[2010] HCA 57", "source_type": "case"}]
    f = facets(rows, Slice())
    assert {x["value"]: x["count"] for x in f["types"]} == {"ruling": 1, "section": 1, "case": 1}
    assert {x["value"]: x["count"] for x in f["series"]} == {"CR": 1, "TR": 1}  # own dim skipped
    f = facets(rows, Slice(year_from=2010, year_to=2010))
    assert {x["value"]: x["count"] for x in f["types"]} == {"ruling": 1, "case": 1}  # strict
    assert {x["value"]: x["count"] for x in f["year"]} == {"2010": 2}  # CR is off by default
    pbr = {"act": "private-rulings", "section": "1", "source_type": "private_ruling"}
    f = facets(rows + [pbr], Slice())
    assert {x["value"]: x["count"] for x in f["acts"]}["private-rulings"] == 1  # the act is an opt-in
    assert {x["value"]: x["count"] for x in f["types"]}["private_ruling"] == 1
    assert _quoted('a "b" c*', "AND") == '"a" """b""" "c"*' and " OR " in _quoted("a b", "OR")
    lib = library("26AH", frozenset({"itaa-1936", "itaa-1997", "rulings", "private-rulings", "tax-cases"}))
    t = {x["value"]: x["count"] for x in lib["types"]}
    assert t["section"] == sum(x["count"] for x in lib["acts"]) and t.get("ruling", 0) <= \
        sum(x["count"] for x in lib["series"])  # CR/PR sit in series, not in the default ruling count
    assert library("26AH", frozenset({"nz-it-2007"}))["types"] == [] or "ruling" not in \
        {x["value"] for x in library("26AH", frozenset({"nz-it-2007"}))["types"]}
    print("ok")
