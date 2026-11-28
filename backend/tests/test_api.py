import os

from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Load environment variables for testing purposes
load_dotenv()

# .env has AZURE_CLIENT_ID set, which would cause main.py to
# import SSO auth modules (authlib, etc.) that may not be installed.
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("AZURE_TENANT_ID", None)
os.environ.pop("AZURE_CLIENT_SECRET", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.main import app
from backend.tests.helpers import session_token

# Authenticated client (SSO session cookie) for content tests.
client = TestClient(app)
client.cookies.set("session", session_token())


# Test GET /health
def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# Test GET /api/acts returns a list
def test_list_acts():
    response = client.get("/api/acts")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert len(response.json()) > 0
    # Check for presence of 'itaa-1997'
    act_ids = [act["id"] for act in response.json()]
    assert "itaa-1997" in act_ids


# Test GET /api/tree/itaa-1997 returns a tree structure
def test_get_itaa_1997_tree():
    response = client.get("/api/tree/itaa-1997")
    assert response.status_code == 200
    json_response = response.json()
    assert "act" in json_response
    assert "ITAA" in json_response["act"].upper() or "Income Tax" in json_response["act"]
    assert "parts" in json_response
    assert isinstance(json_response["parts"], list)
    assert len(json_response["parts"]) > 0


# Test GET /api/section/itaa-1997/6-5 returns frontmatter and body
def test_get_itaa_1997_section_6_5():
    response = client.get("/api/section/itaa-1997/6-5")
    assert response.status_code == 200
    json_response = response.json()
    assert "frontmatter" in json_response
    assert "body" in json_response
    # The exact string changes due to automatic linking, so check for a stable part.
    assert "Income according to ordinary concepts" in json_response["body"]
    assert "ordinary income" in json_response["body"]


# Test that missing section returns 404
def test_get_missing_section_404():
    response = client.get("/api/section/itaa-1997/non-existent-section")
    assert response.status_code == 404
    assert response.json() == {"detail": "Section non-existent-section not found"}


# Test GET /api/search?q=income returns results
def test_search_income():
    response = client.get("/api/search?q=income")
    assert response.status_code == 200
    json_response = response.json()
    assert "results" in json_response
    assert isinstance(json_response["results"], list)
    assert len(json_response["results"]) > 0
    assert "total" in json_response
    assert json_response["total"] > 0


# Test SSO gating: an unauthenticated request is rejected with 401.
def test_anonymous_request_is_401():
    anon = TestClient(app)
    response = anon.get("/api/acts")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


# Test SSO gating: an authenticated request (session cookie) succeeds.
def test_authed_request_is_200():
    response = client.get("/api/acts")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
