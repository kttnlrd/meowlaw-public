"""Layer 1 — API Contract Tests for Batch 3 (Auth + MCP + Admin) endpoints.

Auth mechanism (commit 4a60ca291): a signed JWT in the ``session`` cookie.
  - AuthMiddleware (backend/auth/__init__.py) decodes the cookie into
    request.state.user and never blocks; ungated paths (e.g. /health) need no session.
  - api_router is mounted with Depends(require_user): without a valid session
    its routes return 401 {"detail": "Not authenticated"} before the handler
    (or body validation) runs.
  - mcp_router has no router-level dependency; its token handlers return
    401 {"error": "Login required..."} themselves.
  - An Authorization header (Bearer or Basic) is not a credential.

Endpoint auth classification:
  Session   — any signed-in user
  AdminOnly — signed-in user whose email is in config.ADMIN_EMAILS (else 403)
"""

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from jose import jwt

load_dotenv()  # before backend imports: backend.config reads the environment at import time

from backend.main import app
from backend.tests.helpers import TEST_EMAIL, admin_token, authed_cookie, session_token

client = TestClient(app)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def auth_session():
    """Sign every request in as a plain (non-admin) user; `anon` / `admin` override it."""
    client.cookies.set("session", session_token())
    yield
    client.cookies.clear()


@pytest.fixture
def anon(auth_session):
    """No session cookie: the unauthenticated caller."""
    client.cookies.clear()


@pytest.fixture
def admin(auth_session):
    """Session for a configured admin email."""
    client.cookies.set("session", admin_token())


@pytest.fixture(autouse=True)
def _no_live_writes(monkeypatch):
    """A session now gets past the gate, so stub the writes those handlers make.

    The stubs echo the identity they were handed, so tests can check it came
    from the session rather than the request body.
    """
    import backend.routes.comments as comments_route
    import backend.routes.user_prefs as prefs_route
    from backend.mcp_token_manager import token_manager

    monkeypatch.setattr(comments_route, "add_comment", lambda act, section, author, text: {"author": author})
    monkeypatch.setattr(comments_route, "resolve_comment", lambda comment_id: None)
    monkeypatch.setattr(prefs_route, "update_prefs", lambda email, updates: {"email": email, **updates})
    monkeypatch.setattr(prefs_route, "reset_prefs", lambda email: {"email": email})
    monkeypatch.setattr(token_manager, "create_token", lambda name="", created_by="": f"token-for:{created_by}")


# =============================================================================
# GET /api/comments/{act}/{section} — Session (no longer public)
# =============================================================================

class TestListComments:

    def test_with_session(self):
        """Comments are readable with a signed-in session."""
        response = client.get("/api/comments/itaa-1997/6-5")
        assert response.status_code == 200
        data = response.json()
        assert "act" in data
        assert "section" in data
        assert "count" in data
        assert "comments" in data

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/comments/itaa-1997/6-5")
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session_cookie_header(self, anon):
        """Returns 200 with a raw `Cookie: session=<jwt>` header."""
        response = client.get("/api/comments/itaa-1997/6-5", headers=authed_cookie())
        assert response.status_code == 200
        data = response.json()
        assert "comments" in data

    def test_with_forged_session(self, anon):
        """Returns 401 for a session JWT signed with the wrong key."""
        forged = jwt.encode(
            {"id": 1, "email": TEST_EMAIL, "tier": "trial"}, "wrong-secret-" * 3, algorithm="HS256"
        )
        response = client.get(
            "/api/comments/itaa-1997/6-5",
            headers={"Cookie": f"session={forged}"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_malformed_session_cookie(self, anon):
        """Returns 401 when the session cookie is not a JWT."""
        response = client.get(
            "/api/comments/itaa-1997/6-5",
            headers={"Cookie": "session=not-a-jwt"},
        )
        assert response.status_code == 401

    def test_empty_act_section(self):
        """Returns 200 with empty lists for non-existent act/section."""
        response = client.get("/api/comments/nonexistent/nonexistent")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0


# =============================================================================
# POST /api/comments — Session
# =============================================================================

class TestCreateComment:

    VALID_PAYLOAD = {"act": "itaa-1997", "section": "6-5", "text": "Test comment text"}

    def test_no_session_returns_401(self, anon):
        """Returns 401 with no session (require_user runs before the handler)."""
        response = client.post("/api/comments", json=self.VALID_PAYLOAD)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session(self):
        """A session passes the gate; the author is taken from the session."""
        response = client.post("/api/comments", json=self.VALID_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["author"] == "Test User"

    def test_invalid_payload(self):
        """Returns 422 for invalid payload (missing required fields)."""
        response = client.post("/api/comments", json={})
        assert response.status_code == 422

    def test_empty_text(self):
        """Returns 422 for missing text field."""
        response = client.post("/api/comments", json={"act": "itaa-1997", "section": "6-5"})
        assert response.status_code == 422


# =============================================================================
# POST /api/comments/resolve — AdminOnly
# =============================================================================

class TestResolveComment:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.post("/api/comments/resolve", json={"comment_id": 1})
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.post("/api/comments/resolve", json={"comment_id": 1})
        assert response.status_code == 403

    def test_admin_session_returns_200(self, admin):
        """Returns 200 for an admin session."""
        response = client.post("/api/comments/resolve", json={"comment_id": 1})
        assert response.status_code == 200
        assert response.json() == {"ok": True}


# =============================================================================
# GET /api/issues — Session (no longer public)
# =============================================================================

class TestListIssues:

    def test_with_session(self):
        """Issues are readable with a signed-in session."""
        response = client.get("/api/issues")
        assert response.status_code == 200
        data = response.json()
        assert "issues" in data
        assert "total" in data
        assert isinstance(data["issues"], list)

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/issues")
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session_cookie_header(self, anon):
        """Returns 200 with a raw `Cookie: session=<jwt>` header."""
        response = client.get("/api/issues", headers=authed_cookie())
        assert response.status_code == 200
        assert "issues" in response.json()

    def test_filter_by_status(self):
        """Can filter issues by status query param."""
        response = client.get("/api/issues?status=open")
        assert response.status_code == 200
        data = response.json()
        assert "issues" in data

    def test_invalid_status(self):
        """Invalid status still returns a list (no server-side validation)."""
        response = client.get("/api/issues?status=nonexistent")
        assert response.status_code == 200
        assert isinstance(response.json()["issues"], list)


# =============================================================================
# POST /api/issues — Session (no longer public)
# =============================================================================

class TestCreateIssue:

    @pytest.fixture(autouse=True)
    def _no_live_db_writes(self, monkeypatch):
        """Keep auth-contract tests from writing real tickets to the live DB.

        Previously every test-suite run inserted "Test issue" / "Auth test issue"
        rows into lawkitty_cases.issues, polluting the bug queue (CDN-0105–0113
        etc). These tests verify auth + response shape only, so stub the route's
        DB helpers: no INSERT ever reaches the database.
        """
        import backend.routes.issues as issues_route

        def fake_dict(columns, query, params=None):
            if "MAX(id)" in query:
                return [{"next_id": 900000}]
            return []

        monkeypatch.setattr(issues_route, "_sql_write_params", lambda sql, params=(): True)
        monkeypatch.setattr(issues_route, "_sql_dict", fake_dict)

    def test_with_session(self):
        """Create an issue with a signed-in session."""
        response = client.post(
            "/api/issues",
            json={"category": "bug", "tool": "test-tool", "note": "Test issue"},
        )
        # Returns 200 with ticket info — DB may not be available in test,
        # but the route code handles failures gracefully.
        assert response.status_code == 200
        data = response.json()
        assert "ticket" in data
        assert "status" in data

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.post(
            "/api/issues",
            json={"category": "bug", "note": "Test"},
        )
        assert response.status_code == 401

    def test_with_session_cookie_header(self, anon):
        """Returns 200 with a raw `Cookie: session=<jwt>` header."""
        response = client.post(
            "/api/issues",
            json={"category": "bug", "tool": "test-tool", "note": "Auth test issue"},
            headers=authed_cookie(),
        )
        assert response.status_code == 200
        data = response.json()
        assert "ticket" in data


# =============================================================================
# GET /api/user/prefs — Session
# =============================================================================

class TestGetUserPrefs:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/user/prefs")
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session(self):
        """Returns the signed-in user's own prefs."""
        response = client.get("/api/user/prefs")
        assert response.status_code == 200
        assert response.json()["email"] == TEST_EMAIL


# =============================================================================
# PUT /api/user/prefs — Session
# =============================================================================

class TestUpdateUserPrefs:

    VALID_PAYLOAD = {"theme": "light", "default_act": "gst"}

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.put("/api/user/prefs", json=self.VALID_PAYLOAD)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session(self):
        """Updates the signed-in user's own prefs."""
        response = client.put("/api/user/prefs", json=self.VALID_PAYLOAD)
        assert response.status_code == 200
        assert response.json()["email"] == TEST_EMAIL

    def test_invalid_payload_validation(self, anon):
        """Returns 401 (not validation error) because auth check runs first."""
        response = client.put("/api/user/prefs", json={"invalid_field": "value"})
        assert response.status_code == 401


# =============================================================================
# POST /api/user/prefs/reset — Session
# =============================================================================

class TestResetUserPrefs:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.post("/api/user/prefs/reset")
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_with_session(self):
        """Resets the signed-in user's own prefs."""
        response = client.post("/api/user/prefs/reset")
        assert response.status_code == 200
        assert response.json()["email"] == TEST_EMAIL


# =============================================================================
# GET /api/admin/tokens — AdminOnly
# =============================================================================

class TestAdminListTokens:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/admin/tokens")
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.get("/api/admin/tokens")
        assert response.status_code == 403
        assert response.json() == {"detail": "Admin access required"}

    def test_admin_session_returns_200(self, admin):
        """Returns 200 for an admin session."""
        response = client.get("/api/admin/tokens")
        assert response.status_code == 200
        assert "tokens" in response.json()


# =============================================================================
# POST /api/admin/tokens/{token_id}/revoke — AdminOnly
# (admin path not exercised: it would revoke a live token)
# =============================================================================

class TestAdminRevokeToken:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.post("/api/admin/tokens/1/revoke")
        assert response.status_code == 401

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.post("/api/admin/tokens/1/revoke")
        assert response.status_code == 403

    def test_non_existent_token(self, anon):
        """Returns 401 (auth check runs before token lookup)."""
        response = client.post("/api/admin/tokens/99999/revoke")
        assert response.status_code == 401


# =============================================================================
# GET /api/admin/health — AdminOnly
# =============================================================================

class TestAdminHealth:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/admin/health")
        assert response.status_code == 401

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.get("/api/admin/health")
        assert response.status_code == 403

    def test_admin_session_returns_200(self, admin):
        """Returns 200 for an admin session."""
        response = client.get("/api/admin/health")
        assert response.status_code == 200
        assert "backend" in response.json()


# =============================================================================
# POST /api/admin/reindex — AdminOnly
# (admin path not exercised: it starts a real background search reindex)
# =============================================================================

class TestAdminReindex:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.post("/api/admin/reindex")
        assert response.status_code == 401

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.post("/api/admin/reindex")
        assert response.status_code == 403


# =============================================================================
# GET /api/admin/logs — AdminOnly
# =============================================================================

class TestAdminLogs:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/admin/logs")
        assert response.status_code == 401

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.get("/api/admin/logs?lines=20&level=INFO")
        assert response.status_code == 403

    def test_admin_session_returns_200(self, admin):
        """Returns 200 for an admin session."""
        response = client.get("/api/admin/logs?lines=20&level=INFO")
        assert response.status_code == 200
        assert "logs" in response.json()


# =============================================================================
# GET /api/admin/users — AdminOnly
# =============================================================================

class TestAdminUsers:

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/admin/users")
        assert response.status_code == 401

    def test_non_admin_session_returns_403(self):
        """Returns 403 for a signed-in non-admin."""
        response = client.get("/api/admin/users")
        assert response.status_code == 403

    def test_admin_session_returns_200(self, admin):
        """Returns 200 for an admin session."""
        response = client.get("/api/admin/users")
        assert response.status_code == 200
        assert "users" in response.json()


# =============================================================================
# POST /api/mcp-token — Session (checked in the handler; mcp_router has no require_user)
# =============================================================================

class TestCreateMcpToken:

    def test_no_session_returns_401(self, anon):
        """Returns 401 from route handler when there is no session cookie."""
        response = client.post("/api/mcp-token", json={"name": "test-token"})
        assert response.status_code == 401
        assert "Login required" in response.json().get("error", "")

    def test_with_session(self):
        """A session passes the gate; the token is created for the session user."""
        response = client.post("/api/mcp-token", json={"name": "test-token"})
        assert response.status_code == 200
        assert response.json()["token"] == f"token-for:{TEST_EMAIL}"

    def test_empty_name(self, anon):
        """Returns 401 (auth check runs before field validation matters)."""
        response = client.post("/api/mcp-token", json={"name": ""})
        assert response.status_code == 401

    def test_missing_name_field(self, anon):
        """Returns 401 (auth check runs first, then empty name is allowed)."""
        response = client.post("/api/mcp-token", json={})
        assert response.status_code == 401


# =============================================================================
# GET /api/mcp-tokens — Session (checked in the handler)
# =============================================================================

class TestListMcpTokens:

    def test_no_session_returns_401(self, anon):
        """Returns 401 from route handler when there is no session cookie."""
        response = client.get("/api/mcp-tokens")
        assert response.status_code == 401
        assert "Login required" in response.json().get("error", "")

    def test_with_session(self):
        """Returns 200 with the signed-in user's tokens."""
        response = client.get("/api/mcp-tokens")
        assert response.status_code == 200
        assert isinstance(response.json()["tokens"], list)


# =============================================================================
# POST /api/mcp-tokens/{token_id}/revoke — Session (checked in the handler)
# =============================================================================

class TestRevokeMcpToken:

    def test_no_session_returns_401(self, anon):
        """Returns 401 from route handler when there is no session cookie."""
        response = client.post("/api/mcp-tokens/test123/revoke")
        assert response.status_code == 401
        assert "Login required" in response.json().get("error", "")

    def test_with_session(self):
        """A session passes the login gate; an unknown token then 404s at the ownership check."""
        response = client.post("/api/mcp-tokens/test123/revoke")
        assert response.status_code == 404
        assert response.json() == {"error": "Token not found"}

    def test_non_existent_token_id(self, anon):
        """Returns 401 (auth check runs before token lookup)."""
        response = client.post("/api/mcp-tokens/nonexistent/revoke")
        assert response.status_code == 401


# =============================================================================
# POST /api/mcp-tokens/{token_id}/rename — Session (checked in the handler)
# =============================================================================

class TestRenameMcpToken:

    def test_no_session_returns_401(self, anon):
        """Returns 401 from route handler when there is no session cookie."""
        response = client.post(
            "/api/mcp-tokens/test123/rename",
            json={"name": "new-name"},
        )
        assert response.status_code == 401
        assert "Login required" in response.json().get("error", "")

    def test_with_session(self):
        """A session passes the login gate; an unknown token then 404s at the ownership check."""
        response = client.post(
            "/api/mcp-tokens/test123/rename",
            json={"name": "new-name"},
        )
        assert response.status_code == 404
        assert response.json() == {"error": "Token not found"}

    def test_missing_name_in_body(self, anon):
        """Returns 422 when name field is missing (Pydantic validation runs before auth)."""
        response = client.post(
            "/api/mcp-tokens/test123/rename",
            json={},
        )
        assert response.status_code == 422  # Pydantic validation catches missing field first

    def test_empty_name(self, anon):
        """Returns 401 (auth check runs before field validation)."""
        response = client.post(
            "/api/mcp-tokens/test123/rename",
            json={"name": ""},
        )
        assert response.status_code == 401


# =============================================================================
# GET /api/mcp-hall-of-fame — Session
# =============================================================================

class TestMcpHallOfFame:

    def test_with_session(self):
        """Hall of fame is readable with a signed-in session."""
        response = client.get("/api/mcp-hall-of-fame")
        assert response.status_code == 200
        data = response.json()
        assert "all_time" in data
        assert "monthly" in data
        assert "weekly" in data
        assert "daily" in data

    def test_no_session_returns_401(self, anon):
        """Returns 401 when there is no session cookie."""
        response = client.get("/api/mcp-hall-of-fame")
        assert response.status_code == 401

    def test_with_session_cookie_header(self, anon):
        """Returns 200 with a raw `Cookie: session=<jwt>` header."""
        response = client.get("/api/mcp-hall-of-fame", headers=authed_cookie())
        assert response.status_code == 200
        data = response.json()
        assert "all_time" in data

    def test_returns_leaderboard_structure(self):
        """Verify hall of fame data structure."""
        response = client.get("/api/mcp-hall-of-fame")
        assert response.status_code == 200
        data = response.json()
        for period in ("all_time", "monthly", "weekly", "daily"):
            assert isinstance(data[period], list)


# =============================================================================
# POST /api/comments — Edge case: payload validation (signed in, so validation answers)
# =============================================================================

class TestCreateCommentEdgeCases:

    def test_empty_json_body(self):
        """Returns 422 for empty JSON object (missing required fields)."""
        response = client.post("/api/comments", json={})
        assert response.status_code == 422

    def test_missing_act_field(self):
        """Returns 422 when act field is missing."""
        response = client.post(
            "/api/comments", json={"section": "6-5", "text": "test"}
        )
        assert response.status_code == 422

    def test_missing_section_field(self):
        """Returns 422 when section field is missing."""
        response = client.post(
            "/api/comments", json={"act": "itaa-1997", "text": "test"}
        )
        assert response.status_code == 422


# =============================================================================
# Exempt paths — ungated routes need no session; /api/info is not exempt
# =============================================================================

class TestExemptPaths:

    def test_health_endpoint(self, anon):
        """Health endpoint is ungated: no session needed."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_info_endpoint(self, anon):
        """/api/info is NOT exempt: 401 without a session, 200 with one."""
        response = client.get("/api/info")
        assert response.status_code == 401

        client.cookies.set("session", session_token())
        response = client.get("/api/info")
        assert response.status_code == 200
        assert "version" in response.json()

    def test_api_info_with_session(self):
        """Info payload for a signed-in user."""
        response = client.get("/api/info")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "lawkitty"
        assert "version" in data
        assert "changelog" in data


# =============================================================================
# Auth credential edge cases — only a valid `session` cookie counts
# =============================================================================

class TestAuthHeaderEdgeCases:

    def test_empty_session_cookie(self, anon):
        """Empty session cookie should fail."""
        response = client.get(
            "/api/issues",
            headers={"Cookie": "session="},
        )
        assert response.status_code == 401

    def test_bearer_header_not_accepted(self, anon):
        """A valid session JWT sent as a Bearer token should fail: the header is not a credential."""
        response = client.get(
            "/api/issues",
            headers={"Authorization": f"Bearer {session_token()}"},
        )
        assert response.status_code == 401

    def test_basic_auth_scheme(self, anon):
        """Basic auth scheme should not be accepted."""
        import base64
        creds = base64.b64encode(b"user:testtoken123").decode()
        response = client.get(
            "/api/issues",
            headers={"Authorization": f"Basic {creds}"},
        )
        assert response.status_code == 401

    def test_case_sensitive_cookie_name(self, anon):
        """The cookie must be named exactly `session`."""
        response = client.get(
            "/api/issues",
            headers={"Cookie": f"Session={session_token()}"},
        )
        assert response.status_code == 401


# =============================================================================
# Cross-endpoint auth consistency
# =============================================================================

class TestAuthConsistency:

    @pytest.fixture(autouse=True)
    def _no_live_db_writes(self, monkeypatch):
        """Keep auth-contract tests from writing real tickets to the live DB.

        This class POSTs /api/issues as part of endpoint sweep — stub the
        route's DB helpers so no INSERT reaches lawkitty_cases.issues.
        """
        import backend.routes.issues as issues_route

        def fake_dict(columns, query, params=None):
            if "MAX(id)" in query:
                return [{"next_id": 900000}]
            return []

        monkeypatch.setattr(issues_route, "_sql_write_params", lambda sql, params=(): True)
        monkeypatch.setattr(issues_route, "_sql_dict", fake_dict)

    def test_all_data_endpoints_with_session(self):
        """Verify the formerly public read endpoints work with a valid session."""
        endpoints = [
            ("GET", "/api/comments/itaa-1997/6-5"),
            ("GET", "/api/issues"),
        ]
        for method, path in endpoints:
            response = client.get(path)
            assert response.status_code == 200, f"{method} {path} returned {response.status_code}"

    def test_all_sso_endpoints_with_bearer_only(self, anon):
        """Verify session-gated endpoints return 401 for a Bearer header (even one carrying a
        valid session JWT) when there is no session cookie."""
        headers = {"Authorization": f"Bearer {session_token()}"}
        endpoints = [
            ("GET", "/api/user/prefs"),
            ("PUT", "/api/user/prefs", {"theme": "light"}),
            ("POST", "/api/user/prefs/reset"),
            ("GET", "/api/admin/tokens"),
            ("GET", "/api/admin/health"),
            ("POST", "/api/admin/reindex"),
            ("GET", "/api/admin/logs"),
            ("GET", "/api/admin/users"),
            ("POST", "/api/mcp-token", {"name": "test"}),
            ("GET", "/api/mcp-tokens"),
        ]
        for endpoint in endpoints:
            method = endpoint[0]
            path = endpoint[1]
            body = endpoint[2] if len(endpoint) > 2 else None
            if method == "GET":
                response = client.get(path, headers=headers)
            elif method == "PUT":
                response = client.put(path, json=body, headers=headers)
            else:
                response = client.post(path, json=body if body else {}, headers=headers)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code} (expected 401)"

    def test_all_endpoints_without_auth(self, anon):
        """Verify ALL endpoints return 401 without a session cookie."""
        endpoints = [
            ("GET", "/api/comments/itaa-1997/6-5"),
            ("GET", "/api/issues"),
            ("GET", "/api/user/prefs"),
            ("GET", "/api/admin/tokens"),
            ("GET", "/api/admin/health"),
            ("POST", "/api/admin/reindex"),
            ("GET", "/api/admin/logs"),
            ("GET", "/api/admin/users"),
            ("POST", "/api/mcp-token"),
            ("GET", "/api/mcp-tokens"),
        ]
        for method, path in endpoints:
            if method == "POST":
                response = client.post(path, json={})
            else:
                response = client.get(path)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code} (expected 401)"

    def test_formerly_public_endpoints_need_session(self, anon):
        """The old PublicRead endpoints: 401 without a session, 200 with one."""
        endpoints = [
            ("GET", "/api/comments/itaa-1997/6-5", None),
            ("GET", "/api/issues", None),
            ("POST", "/api/issues", {"category": "bug", "note": "Consistency test"}),
        ]

        def call(method, path, body):
            return client.get(path) if method == "GET" else client.post(path, json=body)

        for method, path, body in endpoints:
            response = call(method, path, body)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code} (expected 401)"

        client.cookies.set("session", session_token())
        for method, path, body in endpoints:
            response = call(method, path, body)
            assert response.status_code == 200, f"{method} {path} returned {response.status_code} (expected 200)"
