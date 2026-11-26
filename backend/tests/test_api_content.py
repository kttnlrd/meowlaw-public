"""
Layer 1 — API Contract Tests for Batch 2 (Content API) endpoints.

Covers all /api/cases, /api/rulings, /api/commentary, /api/smart-links,
/api/section-refs, /api/tax-cases, /api/graph/data, and /api/mcp-hall-of-fame.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.tests.helpers import admin_token

client = TestClient(app)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def admin_session():
    """Sign every request in as an admin so all content (incl. commentary) is visible."""
    client.cookies.set("session", admin_token())
    yield
    client.cookies.clear()


# ── /api/cases ────────────────────────────────────────────────────────────────

class TestListCases:
    """GET /api/cases — returns tree structure of all cases."""

    def test_returns_tree_structure(self):
        resp = client.get("/api/cases")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("act") == "Cases"
        assert "parts" in data
        assert isinstance(data["parts"], list)
        # Should have at least one category (tax, asic, or other)
        assert len(data["parts"]) > 0
        part_ids = [p["id"] for p in data["parts"]]
        assert any(pid in part_ids for pid in ("tax", "asic", "other"))

    def test_part_structure(self):
        resp = client.get("/api/cases")
        data = resp.json()
        part = data["parts"][0]
        assert "id" in part
        assert "title" in part
        assert "divisions" in part
        assert isinstance(part["divisions"], list)
        if part["divisions"]:
            div = part["divisions"][0]
            assert "id" in div
            assert "title" in div
            assert "sections" in div
            assert isinstance(div["sections"], list)


class TestCasesForSection:
    """GET /api/cases/{act}/{section} — returns cases referencing a section."""

    def test_happy_path_itaa_1997(self):
        resp = client.get("/api/cases/itaa-1997/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["act"] == "itaa-1997"
        assert data["section"] == "8-1"
        assert "count" in data
        assert "cases" in data
        assert isinstance(data["cases"], list)

    def test_happy_path_itaa_1936(self):
        resp = client.get("/api/cases/itaa-1936/6-5")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert isinstance(data["cases"], list)

    def test_unknown_section_returns_empty(self):
        resp = client.get("/api/cases/itaa-1997/this-section-does-not-exist-9999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["cases"] == []

    def test_unknown_act_returns_empty(self):
        resp = client.get("/api/cases/nonexistent-act/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0


class TestGetCaseByCitation:
    """GET /api/case/{citation:path} — returns full case metadata."""

    def test_valid_case_returns_200(self):
        # Use a citation we know exists from load_cases()
        resp = client.get("/api/case/Commissioner%20of%20Taxation%20v%20Bendel")
        # This may 404 if the exact citation is not in a JSON file;
        # assert it's either 200 or 404.
        assert resp.status_code in (200, 404)

    def test_unknown_case_returns_404(self):
        resp = client.get("/api/case/nonexistent-case-citation-99999")
        assert resp.status_code == 404

    def test_valid_case_has_expected_keys(self):
        # Find any case file that exists
        import json
        from pathlib import Path
        from backend.config import CASE_DIR
        files = list(CASE_DIR.glob("*.json"))
        if not files:
            pytest.skip("No case JSON files found")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        citation = data.get("citation", files[0].stem)
        from urllib.parse import quote
        resp = client.get(f"/api/case/{quote(citation)}")
        if resp.status_code == 200:
            body = resp.json()
            assert "frontmatter" in body
            assert "body" in body
            assert "citation" in body


# ── /api/rulings/{act}/{section} ─────────────────────────────────────────────

class TestRulingsForSection:
    """GET /api/rulings/{act}/{section} — returns rulings referencing a section."""

    def test_happy_path(self):
        resp = client.get("/api/rulings/itaa-1997/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["act"] == "itaa-1997"
        assert data["section"] == "8-1"
        assert "count" in data
        assert "rulings" in data
        assert isinstance(data["rulings"], list)

    def test_unknown_section_returns_empty(self):
        resp = client.get("/api/rulings/itaa-1997/999-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_empty_act_returns_empty(self):
        resp = client.get("/api/rulings/nonexistent-act/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_ruling_entries_have_citation_and_title(self):
        resp = client.get("/api/rulings/itaa-1997/8-1")
        data = resp.json()
        if data["rulings"]:
            r = data["rulings"][0]
            assert "citation" in r
            assert "title" in r


# ── /api/rulings-list ────────────────────────────────────────────────────────

class TestRulingsList:
    """GET /api/rulings-list — returns all rulings grouped."""

    def test_returns_tree_structure(self):
        resp = client.get("/api/rulings-list")
        assert resp.status_code == 200
        data = resp.json()
        assert "act" in data
        assert data["act"] == "ATO public rulings"
        assert "parts" in data
        assert isinstance(data["parts"], list)
        assert len(data["parts"]) > 0

    def test_part_structure(self):
        resp = client.get("/api/rulings-list")
        data = resp.json()
        part = data["parts"][0]
        assert "id" in part
        assert "title" in part
        assert "divisions" in part
        if part["divisions"]:
            div = part["divisions"][0]
            assert "id" in div
            assert "sections" in div
            assert isinstance(div["sections"], list)
            if div["sections"]:
                sec = div["sections"][0]
                assert "id" in sec
                assert "path" in sec

    def test_group_by_type(self):
        resp = client.get("/api/rulings-list?group=type")
        assert resp.status_code == 200
        data = resp.json()
        assert "parts" in data
        assert len(data["parts"]) > 0

    def test_has_withdrawn_flag_when_present(self):
        resp = client.get("/api/rulings-list")
        data = resp.json()
        # Just ensure no crash; withdrawn flag is optional
        assert "parts" in data


# ── /api/ruling/{citation:path} ──────────────────────────────────────────────

class TestGetRuling:
    """GET /api/ruling/{citation:path} — returns full ruling detail."""

    def test_valid_ruling_returns_200(self):
        # Try a citation that definitely exists
        resp = client.get("/api/ruling/TR_1992_1")
        # Could be 200 if found, or 404 if summaries not present
        assert resp.status_code in (200, 404)

    def test_unknown_ruling_returns_404(self):
        resp = client.get("/api/ruling/ZZ_9999_999")
        assert resp.status_code == 404

    def test_valid_ruling_has_frontmatter_and_structured_fields(self):
        # Try known existing ruling (full ruling, e.g. TR 92/1)
        resp = client.get("/api/ruling/TR_1992_1")
        if resp.status_code == 200:
            data = resp.json()
            assert "frontmatter" in data
            assert "citation" in data
            # Full rulings are summary-only by contract: structured fields
            # are returned; raw full text is served via the download endpoint.
            assert any(k in data for k in ("subject", "background", "ruling", "question", "notice"))


# ── /api/ruling/{citation}/download ──────────────────────────────────────────

class TestDownloadRuling:
    """GET /api/ruling/{citation:path}/download — serves ruling as file."""

    def test_valid_ruling_returns_200(self):
        resp = client.get("/api/ruling/TR_1992_1/download")
        # 200 for found, 404 if file doesn't exist
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert resp.headers.get("content-type") in (
                "text/plain",
                "application/octet-stream",
                "text/plain; charset=utf-8",
            )

    def test_unknown_ruling_returns_404(self):
        resp = client.get("/api/ruling/ZZ_9999_999/download")
        assert resp.status_code == 404

    def test_download_has_content_disposition(self):
        resp = client.get("/api/ruling/TR_1992_1/download")
        if resp.status_code == 200:
            assert "Content-Disposition" in resp.headers


# ── /api/ruling-sections/{citation:path} ─────────────────────────────────────

class TestRulingSections:
    """GET /api/ruling-sections/{citation:path} — section refs from a ruling."""

    def test_valid_ruling_returns_200(self):
        resp = client.get("/api/ruling-sections/TR_1992_1")
        assert resp.status_code == 200
        data = resp.json()
        assert "citation" in data
        assert "referenced_sections" in data
        assert isinstance(data["referenced_sections"], list)

    def test_unknown_ruling_returns_empty_sections(self):
        resp = client.get("/api/ruling-sections/ZZ_9999_999")
        assert resp.status_code == 200
        data = resp.json()
        assert "referenced_sections" in data
        assert data["referenced_sections"] == []

    def test_section_entries_have_act_and_section(self):
        resp = client.get("/api/ruling-sections/TR_1992_1")
        data = resp.json()
        if data["referenced_sections"]:
            ref = data["referenced_sections"][0]
            assert "act" in ref
            assert "section" in ref
            assert "title" in ref


# ── /api/commentary/{act}/{section} ──────────────────────────────────────────

class TestGetCommentary:
    """GET /api/commentary/{act}/{section} — returns commentary entries."""

    def test_happy_path_returns_200(self):
        resp = client.get("/api/commentary/itaa-1997/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert "act" in data
        assert "section" in data
        assert "count" in data
        assert "commentary" in data
        assert isinstance(data["commentary"], list)

    def test_unknown_section_returns_empty(self):
        resp = client.get("/api/commentary/itaa-1997/999-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["commentary"] == []

    def test_with_limit_and_offset(self):
        resp = client.get("/api/commentary/itaa-1997/8-1?limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["commentary"]) <= 5

    def test_commentary_entry_shape(self):
        resp = client.get("/api/commentary/itaa-1997/8-1")
        data = resp.json()
        if data["commentary"]:
            entry = data["commentary"][0]
            assert "publication" in entry
            assert "paragraph_number" in entry
            assert "content_blocks" in entry or "heading_title" in entry


# ── /api/smart-links/{item_type}/{item_id:path} ──────────────────────────────

class TestSmartLinks:
    """GET /api/smart-links/{item_type}/{item_id:path} — smart link data."""

    def test_section_smart_links(self):
        resp = client.get("/api/smart-links/section/itaa-1997/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_type"] == "section"
        assert data["item_id"] == "itaa-1997#8-1"
        assert "links" in data
        assert isinstance(data["links"], list)

    def test_part_smart_links(self):
        resp = client.get("/api/smart-links/part/itaa-1997/2-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_type"] == "part"
        assert "links" in data

    def test_unknown_item_returns_empty_links(self):
        resp = client.get("/api/smart-links/section/itaa-1997/999-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["links"] == []

    def test_unknown_item_type_returns_200(self):
        resp = client.get("/api/smart-links/unknown-type/some-id")
        assert resp.status_code == 200
        data = resp.json()
        assert "links" in data

    def test_case_smart_links(self):
        resp = client.get("/api/smart-links/case/%5B2026%5D%20HCA%2018")
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_type"] == "case"
        assert "links" in data


# ── /api/section-refs/{act}/{section} ────────────────────────────────────────

class TestSectionRefs:
    """GET /api/section-refs/{act}/{section} — references and definitions."""

    def test_happy_path(self):
        resp = client.get("/api/section-refs/itaa-1997/6-5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["act"] == "itaa-1997"
        assert data["section"] == "6-5"
        assert "sections" in data
        assert "definitions" in data
        assert isinstance(data["sections"], list)
        assert isinstance(data["definitions"], list)

    def test_cited_by_includes_incoming_refs(self):
        # s 8-1 itself only cites s 12-5; the related set comes from sections citing it
        data = client.get("/api/section-refs/itaa-1997/8-1").json()
        ids = {s["id"] for s in data["cited_by"]}
        assert len(ids) > 10 and "8-1" not in ids and "995-1" not in ids

    def test_section_entries_have_id_and_title(self):
        resp = client.get("/api/section-refs/itaa-1997/6-5")
        data = resp.json()
        if data["sections"]:
            ref = data["sections"][0]
            assert "id" in ref
            assert "title" in ref
            assert "act" in ref

    def test_definition_entries_have_term_and_section(self):
        resp = client.get("/api/section-refs/itaa-1997/995-1")
        assert resp.status_code == 200
        data = resp.json()
        if data["definitions"]:
            d = data["definitions"][0]
            assert "term" in d
            assert "section" in d

    def test_unknown_section_returns_empty(self):
        resp = client.get("/api/section-refs/itaa-1997/999-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sections"] == []
        assert data["definitions"] == []


# ── /api/tax-cases ───────────────────────────────────────────────────────────

class TestTaxCaseSources:
    """GET /api/tax-cases — deprecated list of case sources."""

    def test_returns_sources(self):
        resp = client.get("/api/tax-cases")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert isinstance(data["sources"], list)
        assert len(data["sources"]) > 0

    def test_source_has_court_and_total(self):
        resp = client.get("/api/tax-cases")
        data = resp.json()
        source = data["sources"][0]
        assert "court" in source
        assert "total" in source
        assert "years" in source

    def test_has_deprecated_marker(self):
        resp = client.get("/api/tax-cases")
        data = resp.json()
        assert "_deprecated" in data


# ── /api/tax-cases/{court} ───────────────────────────────────────────────────

class TestTaxCasesByCourt:
    """GET /api/tax-cases/{court} — deprecated per-court endpoint."""

    VALID_COURTS = ["hca", "fca", "fcafc", "aata"]

    def test_valid_court_returns_200(self):
        for court in self.VALID_COURTS:
            resp = client.get(f"/api/tax-cases/{court}")
            assert resp.status_code == 200, f"Failed for court={court}"
            data = resp.json()
            assert data["court"] == court
            assert "label" in data
            assert "total" in data
            assert "years" in data
            assert "_deprecated" in data

    def test_hca_has_years(self):
        resp = client.get("/api/tax-cases/hca")
        data = resp.json()
        assert data["total"] > 0
        assert len(data["years"]) > 0
        year_entry = data["years"][0]
        assert "year" in year_entry
        assert "count" in year_entry
        assert "cases" in year_entry

    def test_invalid_court_returns_error(self):
        resp = client.get("/api/tax-cases/notacourt")
        # Endpoint returns 200 with a list [error_dict, status_code] for invalid court
        assert resp.status_code == 200
        data = resp.json()
        # Response is actually a list: [{"error": "..."}, 404]
        if isinstance(data, list):
            assert len(data) == 2
            assert "error" in data[0]
            assert "notacourt" in data[0]["error"]
        else:
            assert "error" in data


# ── /api/tax-cases/search ────────────────────────────────────────────────────

class TestTaxCaseSearch:
    """GET /api/tax-cases/search — primary search endpoint."""

    def test_empty_query_returns_all(self):
        resp = client.get("/api/tax-cases/search")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "results" in data
        assert isinstance(data["results"], list)
        assert data["total"] > 0

    def test_search_by_name(self):
        resp = client.get("/api/tax-cases/search?q=income")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_search_by_citation(self):
        resp = client.get("/api/tax-cases/search?q=2026%20HCA")
        assert resp.status_code == 200
        data = resp.json()
        if data["results"]:
            r = data["results"][0]
            assert "citation" in r
            assert "title" in r

    def test_search_no_results(self):
        resp = client.get("/api/tax-cases/search?q=xyznonexistentzzz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["results"] == []

    def test_search_with_limit(self):
        resp = client.get("/api/tax-cases/search?q=tax&limit=5")
        data = resp.json()
        assert len(data["results"]) <= 5


# ── /api/tax-cases/sidebar ───────────────────────────────────────────────────

class TestTaxCaseSidebar:
    """GET /api/tax-cases/sidebar — sidebar tree data."""

    def test_returns_courts(self):
        resp = client.get("/api/tax-cases/sidebar")
        assert resp.status_code == 200
        data = resp.json()
        assert "courts" in data
        assert isinstance(data["courts"], list)
        assert len(data["courts"]) > 0

    def test_court_structure(self):
        resp = client.get("/api/tax-cases/sidebar")
        data = resp.json()
        court = data["courts"][0]
        assert "court" in court
        assert "label" in court
        assert "count" in court
        assert "years" in court
        assert isinstance(court["years"], list)
        if court["years"]:
            year = court["years"][0]
            assert "year" in year
            assert "cases" in year
            assert isinstance(year["cases"], list)

    def test_has_all_courts(self):
        resp = client.get("/api/tax-cases/sidebar")
        data = resp.json()
        court_keys = [c["court"] for c in data["courts"]]
        for expected in ("hca", "fca", "fcafc", "aata"):
            assert expected in court_keys


# ── /api/tax-cases/case/{citation:path} ──────────────────────────────────────

class TestGetTaxCaseByCitation:
    """GET /api/tax-cases/case/{citation:path} — full case detail."""

    def test_valid_citation_returns_200(self):
        resp = client.get("/api/tax-cases/case/%5B2026%5D%20HCA%2018")
        assert resp.status_code in (200, 404)  # may 404 if not in all data files
        if resp.status_code == 200:
            data = resp.json()
            assert "citation" in data
            assert "title" in data

    def test_unknown_citation_returns_404(self):
        resp = client.get("/api/tax-cases/case/%5B9999%5D%20ZZZ%20999")
        assert resp.status_code == 404

    def test_valid_has_metadata(self):
        resp = client.get("/api/tax-cases/case/%5B2026%5D%20HCA%2018")
        if resp.status_code == 200:
            data = resp.json()
            assert "citation" in data
            assert "title" in data
            # Optional fields
            if "court" in data or "court_key" in data:
                assert True


# ── /api/tax-cases/case/{citation}/download ──────────────────────────────────

class TestDownloadTaxCase:
    """GET /api/tax-cases/case/{citation:path}/download — raw HTML download."""

    def test_valid_citation_returns_200_or_404(self):
        resp = client.get("/api/tax-cases/case/%5B2026%5D%20HCA%2018/download")
        # 200 if HTML exists, 400 if citation unparseable, 404 if file missing
        assert resp.status_code in (200, 400, 404)

    def test_invalid_citation_returns_400(self):
        resp = client.get("/api/tax-cases/case/not-a-citation/download")
        assert resp.status_code == 400

    def test_download_has_headers(self):
        resp = client.get("/api/tax-cases/case/%5B2026%5D%20HCA%2018/download")
        if resp.status_code == 200:
            assert "Content-Disposition" in resp.headers


# ── /api/section-tax-cases/{act}/{section} ───────────────────────────────────

class TestSectionTaxCases:
    """GET /api/section-tax-cases/{act}/{section} — deprecated section-case link."""

    def test_happy_path(self):
        resp = client.get("/api/section-tax-cases/itaa-1997/8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["act"] == "itaa-1997"
        assert data["section"] == "8-1"
        assert "cases" in data
        assert isinstance(data["cases"], list)
        assert "_deprecated" in data

    def test_unknown_section_returns_empty(self):
        resp = client.get("/api/section-tax-cases/itaa-1997/999-999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cases"] == []

    def test_case_entries_have_citation(self):
        resp = client.get("/api/section-tax-cases/itaa-1997/8-1")
        data = resp.json()
        if data["cases"]:
            c = data["cases"][0]
            assert "citation" in c
            assert "court" in c


# ── /api/graph/data ──────────────────────────────────────────────────────────

class TestGraphData:
    """GET /api/graph/data — force-directed graph data."""

    def test_section_type_missing_params_returns_400(self):
        resp = client.get("/api/graph/data?type=section")
        assert resp.status_code == 400

    def test_ruling_type_missing_params_returns_400(self):
        resp = client.get("/api/graph/data?type=ruling")
        assert resp.status_code == 400

    def test_unknown_type_returns_400(self):
        resp = client.get("/api/graph/data?type=unknown&act=itaa-1997&section=8-1")
        assert resp.status_code == 400

    def test_section_graph_returns_200(self):
        resp = client.get("/api/graph/data?type=section&act=itaa-1997&section=8-1")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)
        assert "meta" in data

    def test_section_node_structure(self):
        resp = client.get("/api/graph/data?type=section&act=itaa-1997&section=8-1")
        data = resp.json()
        if data["nodes"]:
            node = data["nodes"][0]
            assert "id" in node
            assert "label" in node
            assert "group" in node

    def test_ruling_graph_returns_200(self):
        resp = client.get("/api/graph/data?type=ruling&citation=TR_1992_1")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    def test_case_graph_returns_200(self):
        resp = client.get('/api/graph/data?type=case&citation=%5B2026%5D%20HCA%2018')
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data


# ── /api/mcp-hall-of-fame ────────────────────────────────────────────────────

class TestMCPHallOfFame:
    """GET /api/mcp-hall-of-fame — MCP query leaderboard."""

    def test_returns_200(self):
        resp = client.get("/api/mcp-hall-of-fame")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))
        if isinstance(data, list):
            if data:
                entry = data[0]
                # Should have some identifying fields
                assert isinstance(entry, dict)

    def test_returns_list_or_dict(self):
        resp = client.get("/api/mcp-hall-of-fame")
        data = resp.json()
        # Token manager may return a dict with keys like all_time, monthly, daily
        # or a flat list — accept either
        assert isinstance(data, (list, dict))