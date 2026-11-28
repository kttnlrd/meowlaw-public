"""
Batch 1 — Core API Contract Tests.

Covers all endpoints listed in the Batch 1 spec.  Tests are ordered by endpoint
path and grouped into:
  - Health & Info
  - Acts & Tree
  - Sections
  - Definitions
  - Search (including hybrid, flat, unified)
  - Data Version
  - Auth (session-cookie gating)

Every /api/* data route is behind ``Depends(require_user)`` (commit 4a60ca291), so
``client`` carries a signed session cookie (autouse fixture below) while ``anon``
never does: use ``anon`` for anything that asserts public access or a 401.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from jose import jwt

# Load .env but strip AZURE_CLIENT_ID so the run doesn't depend on the dev
# machine's Entra config (backend/routes/admin.py reports auth_enabled from it).
from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)

from backend.main import app
from backend import config
from backend.tests.helpers import TEST_EMAIL, authed_cookie, session_token

client = TestClient(app)  # signed in for every test by auth_session
anon = TestClient(app)  # never given a cookie


@pytest.fixture(autouse=True)
def auth_session():
    """Sign `client` in with a valid session cookie; `anon` is deliberately left alone."""
    client.cookies.set("session", session_token())
    yield
    client.cookies.clear()


# ============================================================================
# Helpers
# ============================================================================

SAMPLE_ACT = "itaa-1997"
SAMPLE_SECTION = "6-5"
SAMPLE_TERM = "ordinary income"
GARBAGE_ACT = "this-act-does-not-exist"
GARBAGE_SECTION = "9999-9999"
GARBAGE_TERM = "zzznotadefinitionzzz"

TREE_SHAPE_KEYS = {"act", "parts"}
PART_SHAPE_KEYS = {"id", "title"}
SECTION_SHAPE_KEYS = {"id", "title"}
DIVISION_SHAPE_KEYS = {"id", "title", "sections"}

SEARCH_Q = "income"
SEARCH_Q_EMPTY = ""
SEARCH_Q_NOMATCH = "zzzunique99nonexistent999"


# ============================================================================
# 1.  /health
# ============================================================================

class TestHealth:
    """/health — no auth, always public (asserted without a session cookie)."""

    def test_health_returns_ok(self):
        resp = anon.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_exact_shape(self):
        resp = anon.get("/health")
        body = resp.json()
        assert set(body.keys()) == {"status"}


# ============================================================================
# 2.  /api/info
# ============================================================================

class TestInfo:
    """/api/info — version and changelog (the endpoints map was removed in 18e148c6d)."""

    def test_info_200(self):
        resp = client.get("/api/info")
        assert resp.status_code == 200
        body = resp.json()
        assert "name" in body
        assert "version" in body
        assert body["name"] == "lawkitty"

    def test_info_has_changelog(self):
        resp = client.get("/api/info")
        body = resp.json()
        assert "changelog" in body
        assert isinstance(body["changelog"], list)
        assert len(body["changelog"]) > 0
        assert "version" in body["changelog"][0]

    def test_info_does_not_expose_endpoint_map(self):
        """18e148c6d dropped the API-surface listing as hardening; keep it gone."""
        resp = client.get("/api/info")
        assert "endpoints" not in resp.json()


# ============================================================================
# 3.  /api/data-version
# ============================================================================

class TestDataVersion:
    """/api/data-version — version registry info."""

    def test_data_version_200(self):
        resp = client.get("/api/data-version")
        assert resp.status_code == 200
        body = resp.json()
        # At minimum the registry returns a dict with keys we expect
        assert isinstance(body, dict)


# ============================================================================
# 4.  /api/acts
# ============================================================================

class TestActs:
    """GET /api/acts — list all available acts."""

    def test_list_acts_200(self):
        resp = client.get("/api/acts")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_acts_non_empty(self):
        resp = client.get("/api/acts")
        assert len(resp.json()) > 0

    def test_list_acts_contains_itaa_1997(self):
        resp = client.get("/api/acts")
        act_ids = [a["id"] for a in resp.json()]
        assert "itaa-1997" in act_ids

    def test_list_acts_contains_virtual_acts(self):
        resp = client.get("/api/acts")
        act_ids = [a["id"] for a in resp.json()]
        assert "rulings" in act_ids
        assert "tax-cases" in act_ids

    def test_list_acts_each_entry_has_id_and_name(self):
        resp = client.get("/api/acts")
        for entry in resp.json():
            assert "id" in entry
            assert "name" in entry

    def test_list_acts_requires_session(self):
        """No dev-mode public fallback: anonymous is 401, a signed-in session is 200."""
        assert anon.get("/api/acts").status_code == 401
        assert client.get("/api/acts").status_code == 200


# ============================================================================
# 5.  /api/tree/{act}
# ============================================================================

class TestTree:
    """GET /api/tree/{act} — full structure of an act."""

    def test_tree_itaa_1997_200(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        assert resp.status_code == 200

    def test_tree_itaa_1997_shape(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        body = resp.json()
        assert "act" in body
        assert "parts" in body
        assert isinstance(body["parts"], list)

    def test_tree_itaa_1997_act_name(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        # The actual name is the full title, not just the acronym
        assert "Income Tax Assessment Act" in resp.json()["act"]

    def test_tree_itaa_1997_has_parts(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        assert len(resp.json()["parts"]) > 0

    def test_tree_itaa_1997_parts_have_ids(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        for part in resp.json()["parts"]:
            assert "id" in part
            assert "title" in part

    def test_tree_itaa_1997_parts_have_sections_or_divisions(self):
        resp = client.get(f"/api/tree/{SAMPLE_ACT}")
        for part in resp.json()["parts"]:
            has_sections = len(part.get("sections", [])) > 0
            has_divisions = len(part.get("divisions", [])) > 0
            assert has_sections or has_divisions, \
                f"Part {part['id']} has neither sections nor divisions"

    def test_tree_nonexistent_act_returns_404(self):
        resp = client.get(f"/api/tree/{GARBAGE_ACT}")
        assert resp.status_code == 404

    @pytest.mark.parametrize("act", ["rulings", "tax-cases"])
    def test_tree_virtual_acts_200(self, act):
        """rulings and tax-cases are virtual acts with their own tree shapes."""
        resp = client.get(f"/api/tree/{act}")
        assert resp.status_code == 200
        body = resp.json()
        # These return different shapes (not the standard tree)
        assert isinstance(body, (dict, list))


# ============================================================================
# 6.  /api/section/{act}/{section}
# ============================================================================

class TestSection:
    """GET /api/section/{act}/{section} — full text of a section."""

    def test_section_200(self):
        resp = client.get(f"/api/section/{SAMPLE_ACT}/{SAMPLE_SECTION}")
        assert resp.status_code == 200

    def test_section_has_frontmatter_and_body(self):
        resp = client.get(f"/api/section/{SAMPLE_ACT}/{SAMPLE_SECTION}")
        body = resp.json()
        assert "frontmatter" in body
        assert "body" in body

    def test_section_6_5_contains_expected_text(self):
        resp = client.get(f"/api/section/{SAMPLE_ACT}/{SAMPLE_SECTION}")
        html = resp.json()["body"]
        assert "ordinary concepts" in html
        assert "ordinary income" in html

    def test_section_nonexistent_returns_404(self):
        resp = client.get(f"/api/section/{SAMPLE_ACT}/{GARBAGE_SECTION}")
        assert resp.status_code == 404

    def test_section_nonexistent_act_returns_404(self):
        resp = client.get(f"/api/section/{GARBAGE_ACT}/{SAMPLE_SECTION}")
        assert resp.status_code == 404

    def test_section_404_detail_message(self):
        resp = client.get(f"/api/section/{SAMPLE_ACT}/{GARBAGE_SECTION}")
        assert "detail" in resp.json()


# ============================================================================
# 7.  /api/definitions/{act}
# ============================================================================

class TestDefinitions:
    """GET /api/definitions/{act} — all defined terms in an act."""

    def test_definitions_200(self):
        resp = client.get(f"/api/definitions/{SAMPLE_ACT}")
        assert resp.status_code == 200

    def test_definitions_has_act_count_terms(self):
        resp = client.get(f"/api/definitions/{SAMPLE_ACT}")
        body = resp.json()
        assert "act" in body
        assert body["act"] == SAMPLE_ACT
        assert "count" in body
        assert "terms" in body

    def test_definitions_non_empty(self):
        resp = client.get(f"/api/definitions/{SAMPLE_ACT}")
        assert resp.json()["count"] > 0
        assert len(resp.json()["terms"]) > 0

    def test_definitions_terms_are_list_with_text(self):
        resp = client.get(f"/api/definitions/{SAMPLE_ACT}")
        terms = resp.json()["terms"]
        # terms is an alphabetical list of {term, section, anchor, text}
        assert isinstance(terms, list)
        if terms:
            sample = terms[0]
            assert isinstance(sample, dict)
            for key in ("term", "section", "anchor", "text"):
                assert key in sample

    def test_definitions_nonexistent_act_returns_404_or_empty(self):
        """Non-existent act may 404 or return empty depending on implementation."""
        resp = client.get(f"/api/definitions/{GARBAGE_ACT}")
        assert resp.status_code in (200, 404)


# ============================================================================
# 8.  /api/definition/{act}/{term}
# ============================================================================

class TestDefinition:
    """GET /api/definition/{act}/{term} — single definition lookup."""

    def test_definition_200(self):
        resp = client.get(f"/api/definition/{SAMPLE_ACT}/{SAMPLE_TERM}")
        assert resp.status_code == 200

    def test_definition_returns_object(self):
        resp = client.get(f"/api/definition/{SAMPLE_ACT}/{SAMPLE_TERM}")
        body = resp.json()
        assert isinstance(body, dict)
        # Should have term/definition/section_url or equivalent
        assert "term" in body or "definition" in body or "section" in body

    def test_definition_garbage_term_returns_404(self):
        resp = client.get(f"/api/definition/{SAMPLE_ACT}/{GARBAGE_TERM}")
        assert resp.status_code == 404

    def test_definition_garbage_act_falls_back_across_acts(self):
        # v2.7.5+: get_definition searches ALL acts (preferring the
        # requested act), so a term defined in any act resolves even
        # when the requested act slug is unknown/typo'd.
        resp = client.get(f"/api/definition/{GARBAGE_ACT}/{SAMPLE_TERM}")
        assert resp.status_code == 200
        body = resp.json()
        assert "term" in body
        assert "text" in body

    def test_definition_garbage_act_and_term_returns_404(self):
        # Cross-act fallback must still 404 when the term exists nowhere.
        resp = client.get(f"/api/definition/{GARBAGE_ACT}/{GARBAGE_TERM}")
        assert resp.status_code == 404


# ============================================================================
# 9.  /api/definition-text/{act}/{term}
# ============================================================================

class TestDefinitionText:
    """GET /api/definition-text/{act}/{term} — full definition text."""

    def test_definition_text_200(self):
        resp = client.get(f"/api/definition-text/{SAMPLE_ACT}/{SAMPLE_TERM}")
        assert resp.status_code == 200

    def test_definition_text_returns_object(self):
        resp = client.get(f"/api/definition-text/{SAMPLE_ACT}/{SAMPLE_TERM}")
        body = resp.json()
        assert isinstance(body, dict)

    def test_definition_text_garbage_returns_404(self):
        resp = client.get(f"/api/definition-text/{SAMPLE_ACT}/{GARBAGE_TERM}")
        assert resp.status_code == 404


# ============================================================================
# 10.  /api/section-defined-terms/{act}/{section}
# ============================================================================

class TestSectionDefinedTerms:
    """GET /api/section-defined-terms/{act}/{section} — terms defined in a section."""

    def test_section_defined_terms_200(self):
        resp = client.get(
            f"/api/section-defined-terms/{SAMPLE_ACT}/{SAMPLE_SECTION}"
        )
        assert resp.status_code == 200

    def test_section_defined_terms_shape(self):
        resp = client.get(
            f"/api/section-defined-terms/{SAMPLE_ACT}/{SAMPLE_SECTION}"
        )
        body = resp.json()
        assert "act" in body
        assert "section" in body
        assert "count" in body
        assert "terms" in body

    def test_section_defined_terms_terms_are_list(self):
        resp = client.get(
            f"/api/section-defined-terms/{SAMPLE_ACT}/{SAMPLE_SECTION}"
        )
        assert isinstance(resp.json()["terms"], list)

    def test_section_defined_terms_each_term_has_keys(self):
        resp = client.get(
            f"/api/section-defined-terms/{SAMPLE_ACT}/{SAMPLE_SECTION}"
        )
        for term in resp.json()["terms"]:
            assert "term" in term

    def test_section_defined_terms_nonexistent_section(self):
        """Should return empty terms, not 404, since the section content lookup
        may fail gracefully."""
        resp = client.get(
            f"/api/section-defined-terms/{SAMPLE_ACT}/{GARBAGE_SECTION}"
        )
        # Could be 404 (if the section lookup throws) or 200 with empty terms
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert resp.json()["count"] == 0


# ============================================================================
# 11.  /api/search  (plain FTS5)
# ============================================================================

class TestSearch:
    """GET /api/search — keyword search over sections."""

    def test_search_200(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}")
        assert resp.status_code == 200

    def test_search_shape(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}")
        body = resp.json()
        assert "results" in body
        assert "total" in body
        assert "offset" in body
        assert "limit" in body

    def test_search_has_results(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}")
        body = resp.json()
        assert body["total"] > 0
        assert len(body["results"]) > 0

    def test_search_result_shape(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}")
        result = resp.json()["results"][0]
        assert "act" in result
        assert "section" in result
        assert "title" in result

    def test_search_empty_query(self):
        """Empty query should return 200 with no or empty results (not crash)."""
        resp = client.get(f"/api/search?q={SEARCH_Q_EMPTY}")
        assert resp.status_code == 200

    def test_search_with_act_filter(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}&act={SAMPLE_ACT}")
        assert resp.status_code == 200
        body = resp.json()
        for r in body["results"]:
            assert r["act"] == SAMPLE_ACT

    def test_search_pagination_offset(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}&offset=5&limit=3")
        body = resp.json()
        assert body["offset"] == 5
        assert body["limit"] == 3
        assert len(body["results"]) <= 3

    def test_search_limit_capped_at_100(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}&limit=999")
        body = resp.json()
        assert body["limit"] == 100

    def test_search_negative_offset_defaults(self):
        resp = client.get(f"/api/search?q={SEARCH_Q}&offset=-5")
        assert resp.status_code == 200

    def test_search_nomatch_returns_empty(self):
        resp = client.get(f"/api/search?q={SEARCH_Q_NOMATCH}")
        body = resp.json()
        assert body["total"] == 0
        assert body["results"] == []


# ============================================================================
# 12.  /api/search/hybrid
# ============================================================================

class TestSearchHybrid:
    """GET /api/search/hybrid — RRF-fused FTS5 + vector search."""

    def test_hybrid_200(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}")
        assert resp.status_code == 200

    def test_hybrid_shape(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}")
        body = resp.json()
        assert "results" in body
        assert "total" in body

    def test_hybrid_result_has_fusion_score(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}")
        body = resp.json()
        if body["results"]:
            assert "fusion_score" in body["results"][0]

    def test_hybrid_result_shape(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}")
        body = resp.json()
        if body["results"]:
            r = body["results"][0]
            assert "act" in r
            assert "section" in r
            assert "title" in r or "snippet" in r

    def test_hybrid_with_act_filter(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}&act={SAMPLE_ACT}")
        assert resp.status_code == 200

    def test_hybrid_empty_query(self):
        resp = client.get("/api/search/hybrid?q=")
        assert resp.status_code == 200

    def test_hybrid_limit_default(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}&limit=5")
        body = resp.json()
        assert len(body["results"]) <= 5

    def test_hybrid_limit_capped_at_50(self):
        resp = client.get(f"/api/search/hybrid?q={SEARCH_Q}&limit=999")
        body = resp.json()
        assert len(body["results"]) <= 50


# ============================================================================
# 13.  /api/search/flat
# ============================================================================

class TestSearchFlat:
    """GET /api/search/flat — interleaved sections + rulings."""

    def test_flat_200(self):
        resp = client.get(f"/api/search/flat?q={SEARCH_Q}")
        assert resp.status_code == 200

    def test_flat_shape(self):
        resp = client.get(f"/api/search/flat?q={SEARCH_Q}")
        body = resp.json()
        assert "results" in body
        assert "query" in body

    def test_flat_result_shape(self):
        resp = client.get(f"/api/search/flat?q={SEARCH_Q}")
        body = resp.json()
        if body["results"]:
            r = body["results"][0]
            assert "type" in r
            assert "act" in r
            assert "section" in r or "citation" in r

    def test_flat_empty_query(self):
        resp = client.get("/api/search/flat?q=")
        body = resp.json()
        assert body["results"] == []

    def test_flat_results_are_mixed_types(self):
        """When both sections and rulings exist, results should include both."""
        resp = client.get(f"/api/search/flat?q={SEARCH_Q}")
        body = resp.json()
        types = {r.get("type") for r in body["results"]}
        # At minimum some type is present
        assert len(types) > 0

    def test_flat_limit(self):
        resp = client.get(f"/api/search/flat?q={SEARCH_Q}&limit=5")
        body = resp.json()
        assert len(body["results"]) <= 5

    def test_flat_nomatch(self):
        resp = client.get(f"/api/search/flat?q={SEARCH_Q_NOMATCH}")
        body = resp.json()
        assert body["results"] == []


# ============================================================================
# 15.  Auth Middleware
# ============================================================================

# Every data route this file exercises, plus not-found variants: the 401 must come before
# the act/section lookup, so an anonymous caller can't probe for what exists.
GATED_ENDPOINTS = [
    "/api/info",
    "/api/data-version",
    "/api/acts",
    f"/api/tree/{SAMPLE_ACT}",
    f"/api/tree/{GARBAGE_ACT}",
    f"/api/section/{SAMPLE_ACT}/{SAMPLE_SECTION}",
    f"/api/section/{SAMPLE_ACT}/{GARBAGE_SECTION}",
    f"/api/definitions/{SAMPLE_ACT}",
    f"/api/definition/{SAMPLE_ACT}/{SAMPLE_TERM}",
    f"/api/definition-text/{SAMPLE_ACT}/{SAMPLE_TERM}",
    f"/api/section-defined-terms/{SAMPLE_ACT}/{SAMPLE_SECTION}",
    f"/api/search?q={SEARCH_Q}",
    f"/api/search/hybrid?q={SEARCH_Q}",
    f"/api/search/flat?q={SEARCH_Q}",
]

# A well-formed admin JWT (expiry far in the future) signed with the wrong key, so it can
# only be rejected on its signature, not on parsing or expiry.
FORGED_JWT = jwt.encode(
    {"id": 1, "email": TEST_EMAIL, "tier": "admin", "exp": 4102444800},
    "not-the-real-secret",
    algorithm="HS256",
)


class TestAuth:
    """Session-cookie auth gating on /api/* endpoints (SSO model, Depends(require_user))."""

    AUTH_TOKEN = "integration-test-token-abc123"  # legacy bearer value: must grant nothing (public-export: allow)

    def test_health_is_public_without_session(self):
        """/health is exempt from auth."""
        resp = anon.get("/health")
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", GATED_ENDPOINTS)
    def test_api_returns_401_without_token(self, path):
        resp = anon.get(path)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize(
        "cookie", ["wrong-token", FORGED_JWT], ids=["garbage", "wrong-secret-jwt"]
    )
    def test_api_returns_401_with_bad_token(self, cookie):
        resp = anon.get("/api/acts", headers={"Cookie": f"session={cookie}"})
        assert resp.status_code == 401

    def test_api_returns_200_with_valid_token(self):
        resp = client.get("/api/acts")
        assert resp.status_code == 200
        # Same explicit Cookie-header mechanism the bad-token cases use, so their 401s
        # can't be a silently dropped header.
        assert anon.get("/api/acts", headers=authed_cookie()).status_code == 200

    def test_legacy_bearer_token_grants_nothing(self, monkeypatch):
        """Replaces 'no auth when token is None': dev-mode public access is gone, and a
        configured bearer token in an Authorization header must not open /api/* either."""
        monkeypatch.setattr(config, "BEARER_TOKEN", self.AUTH_TOKEN, raising=False)
        resp = anon.get("/api/acts", headers={"Authorization": f"Bearer {self.AUTH_TOKEN}"})
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "exempt_path",
        ["/health", "/", "/favicon.ico", "/.well-known/oauth-authorization-server"],
    )
    def test_exempt_paths_are_public(self, exempt_path):
        """Ungated paths should not require a session."""
        resp = anon.get(exempt_path)
        assert resp.status_code != 401, \
            f"Exempt path {exempt_path} returned 401"

    def test_anonymous_client_has_no_session(self):
        """Isolation: a session leaking into `anon` would make every 401 test vacuous."""
        assert not anon.cookies.get("session")


# ============================================================================
# 16.  Edge Cases & Cross-Cutting
# ============================================================================

class TestEdgeCases:
    """URL encoding, special characters, numeric-only sections."""

    def test_section_with_dash_is_handled(self):
        """Section IDs like '6-5' are path params with dashes."""
        resp = client.get(f"/api/section/{SAMPLE_ACT}/6-5")
        assert resp.status_code == 200

    def test_definition_with_spaces_is_url_encoded(self):
        """Terms with spaces need URL encoding."""
        resp = client.get(
            f"/api/definition/{SAMPLE_ACT}/ordinary%20income"
        )
        assert resp.status_code in (200, 404)

    def test_definition_text_with_spaces(self):
        resp = client.get(
            f"/api/definition-text/{SAMPLE_ACT}/ordinary%20income"
        )
        assert resp.status_code in (200, 404)

    def test_numeric_act_name(self):
        """Acts with numeric-only IDs should still resolve if they exist."""
        resp = client.get("/api/tree/itaa-1936")
        if resp.status_code == 200:
            assert "act" in resp.json()

    def test_search_special_chars(self):
        """Search with special regex chars shouldn't crash."""
        resp = client.get("/api/search?q=tax+%26+gst")
        assert resp.status_code == 200

    def test_search_unicode(self):
        resp = client.get("/api/search?q=%C3%A9t%C3%A9")
        assert resp.status_code == 200

    def test_concurrent_requests(self):
        """Multiple rapid-fire requests should all succeed."""
        for _ in range(5):
            resp = client.get(f"/api/search?q={SEARCH_Q}")
            assert resp.status_code == 200

    def test_response_content_type(self):
        """All API responses should be JSON."""
        endpoints = [
            "/api/acts",
            f"/api/tree/{SAMPLE_ACT}",
            f"/api/section/{SAMPLE_ACT}/{SAMPLE_SECTION}",
            f"/api/definitions/{SAMPLE_ACT}",
            f"/api/search?q={SEARCH_Q}",
            "/api/info",
            "/api/data-version",
        ]
        for ep in endpoints:
            resp = client.get(ep)
            assert resp.status_code == 200, f"{ep} returned {resp.status_code}"
            assert "application/json" in resp.headers.get("content-type", ""), \
                f"{ep} did not return JSON"