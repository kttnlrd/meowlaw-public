"""CDN-0206 — related commentary for itaa-1997 8-1 must be ranked and labelled.

it has 112 `explained_in` edges to commentary nodes, all weight 1.0:
73 Master Tax Guide + 39 Master Tax Examples. Ranking by per-chapter link count
puts MTG ch-16 (Deductions, 47 links) first; a tie-break on the node key made
the slice alphabetical by chapter, so ch-16 never appeared.

Checks:
  * both publications present in the returned slice
  * every entry carries a non-empty `source`
  * at least one Master Tax Guide ch-16 row
  * every `chapter_title` non-empty
  * NOTE: `chapter_title` free of Cyrillic (C20_non_latin_script, the E-a mojibake
    repair — 875 MTG section_index titles + 22 tree titles) is a RELEASE assertion
    that cannot pass until the E-a data fix lands in a later batch. It is measured
    and reported here, deliberately not asserted.
"""
from __future__ import annotations

import asyncio
import json
import re

from backend.fastmcp_server import _graph_commentary_for_section, get_section

ACT, SECTION = "itaa-1997", "8-1"
CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def _entries() -> list[dict]:
    return _graph_commentary_for_section(ACT, SECTION, limit=10)


def test_both_publications_present():
    pubs = {e["publication"] for e in _entries()}
    assert "Master Tax Guide" in pubs, pubs
    assert "Master Tax Examples" in pubs, pubs


def test_every_entry_has_source_and_chapter_title():
    missing_src = [e["url"] for e in _entries() if not (e.get("source") or "").strip()]
    assert not missing_src, f"entries with empty source: {missing_src}"
    missing_ch = [e["url"] for e in _entries() if not (e.get("chapter_title") or "").strip()]
    assert not missing_ch, f"entries with empty chapter_title: {missing_ch}"


def test_master_tax_guide_chapter_16_is_ranked_in():
    rows = [e for e in _entries() if e["publication"] == "Master Tax Guide"]
    assert any(e["chapter_number"].lstrip("0") == "16" for e in rows), (
        "MTG ch-16 (Deductions, 47 of the 73 MTG links to 8-1) never appears: "
        + json.dumps(rows, ensure_ascii=False)
    )


def test_chapter_16_outranks_lower_link_chapters():
    """The top-ranked MTG chapter must be the one with the most links to 8-1."""
    rows = [e for e in _entries() if e["publication"] == "Master Tax Guide"]
    assert rows[0]["chapter_number"].lstrip("0") == "16", rows[0]


def test_mcp_get_section_commentary_is_labelled():
    payload = json.loads(asyncio.run(get_section(ACT, SECTION)))
    comm = payload["related"]["commentary"]
    assert comm, "no commentary on the MCP path — fixture moved"
    assert any(e.get("source") for e in comm), comm[:2]
    assert any((e.get("chapter_title") or "") for e in comm), comm[:2]


def test_report_cyrillic_chapter_titles_e_a_pending():
    """Measure, do not assert: the E-a mojibake repair is a later, data batch."""
    dirty = [e["chapter_title"] for e in _entries() if CYRILLIC.search(e.get("chapter_title") or "")]
    if dirty:
        print(f"CDN-0206 note: {len(dirty)} chapter_title(s) still carry E-a mojibake: {dirty[:3]}")
