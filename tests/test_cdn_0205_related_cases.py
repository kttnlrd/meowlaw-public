"""CDN-0205 — related cases must show party names on BOTH the REST and MCP paths.

At HEAD the REST route `/api/cases/{act}/{section}` returns
`{"title": "[2020] FCAFC 25", "citation": "[2020] FCAFC 25"}` for itaa-1997 8-1
because `data_loader.get_cases_for_section` never consults the case-name index
(`load_case_name_index()`); the MCP path was fixed by 6af2f67dd but the REST path
was not. `short_case_name` also returns the jurisdiction word 'Taxation' for the
574 AAT/ART names shaped 'X and Commissioner of Taxation (Taxation)'.
"""
from __future__ import annotations

import asyncio
import json
import re

from backend.services.data_loader import (
    get_cases_for_section,
    load_case_name_index,
    short_case_name,
)

ACT, SECTION = "itaa-1997", "8-1"


def test_rest_related_cases_carry_party_names():
    cases = get_cases_for_section(ACT, SECTION)
    assert cases, "no related cases returned — fixture moved"
    bad = [c for c in cases if not c.get("title") or c.get("title") == c.get("citation")]
    assert not bad, f"REST /api/cases/{ACT}/{SECTION} returns citations as titles: {bad[:5]}"


def test_mcp_related_cases_carry_party_names():
    from backend.fastmcp_server import get_section

    payload = json.loads(asyncio.run(get_section(ACT, SECTION)))
    cases = payload["related"]["cases"]
    assert cases, "no related cases on the MCP path — fixture moved"
    bad = [c for c in cases if not c.get("title") or c.get("title") == c.get("citation")]
    assert not bad, f"MCP get_section returns citations as titles: {bad[:5]}"


def test_short_case_name_names_the_taxpayer_not_the_jurisdiction():
    assert short_case_name("Foo Holdings Pty Ltd and Commissioner of Taxation (Taxation)") == "Foo Holdings Pty Ltd"
    assert short_case_name("Smith and Commissioner of Taxation (Taxation)") == "Smith"
    assert short_case_name("Re Smith and Commissioner of Taxation (Taxation)") == "Smith"
    # the ' v ' forms must not regress
    assert short_case_name("Smith v Commissioner of Taxation") == "Smith"
    assert short_case_name("Commissioner of Taxation v Smith") == "Smith"


def test_aat_conjoined_names_are_not_the_jurisdiction_word():
    """2043 cases shortened to 'Taxation' before the fix; 1961 of them the
    '... and Commissioner of Taxation' class and 381 the '...(Taxation)' class.

    The two names below are the gov-vs-gov residue: both sides of the conjoiner
    name a government body ('Melbourne Western Region Commission Incorporated')
    or the party name is truncated upstream ('s Joint Administrators of ...').
    They are out of this ticket's scope and are pinned, not hidden.
    """
    known_gov_vs_gov = {
        "Re Melbourne Western Region Commission Incorporated and Commissioner of Taxation",
        "s Joint Administrators of Cooper & Oxley Builders Pty Ltd as trustee for the "
        "Cooper & Oxley Builders Unit Trust and Commissioner of Taxation (Taxation)",
    }
    index = load_case_name_index()
    conjoined = [
        v for v in index.values()
        if re.search(r"\s+and\s+(?:the\s+)?(?:Federal\s+|Deputy\s+)?Commissioner\b",
                     v.get("title") or "")
    ]
    assert conjoined, "no ' and Commissioner' names found — fixture moved"
    bad = {v["title"] for v in conjoined if v.get("short_name") == "Taxation"} - known_gov_vs_gov
    assert not bad, f"{len(bad)} AAT-conjoined names still shorten to 'Taxation': {sorted(bad)[:5]}"


def test_short_name_taxation_count_collapses():
    index = load_case_name_index()
    n = sum(1 for v in index.values() if v.get("short_name") == "Taxation")
    assert n <= 85, f"{n} cases shorten to the jurisdiction word (2043 before the fix)"
    assert len(index) >= 2047, f"name index shrank: {len(index)}"
