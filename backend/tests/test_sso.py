"""Google / Microsoft sign-in (backend/auth/providers/sso.py).

No network: a throwaway RSA key plays the identity provider, and the token
endpoint is stubbed, so authlib's real ID-token validation (signature, audience,
nonce, expiry) and our verified-email rules both run.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.jose import JsonWebKey, jwt
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from backend.auth import decode_session_token, store  # noqa: E402
from backend.auth.providers import sso  # noqa: E402
from backend.main import app  # noqa: E402

CLIENT_ID = "test-client-id"
TID = "11111111-2222-3333-4444-555555555555"
MS_ISS = f"https://login.microsoftonline.com/{TID}/v2.0"
KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
OTHER_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})


# ── verified_identity: the rules that decide whose account a sign-in reaches ──

def test_google_requires_verified_email():
    ok = {"iss": "https://accounts.google.com", "sub": "g-1", "email": "A@Example.com", "email_verified": True, "name": "A"}
    assert sso.verified_identity("google", ok) == {"email": "a@example.com", "name": "A", "subject": "g-1"}
    for bad in ({"email_verified": False}, {"email_verified": "true"}, {"email": ""}, {"iss": "https://evil.example"}, {"sub": ""}):
        with pytest.raises(sso.SSOError):
            sso.verified_identity("google", {**ok, **bad})


def test_microsoft_requires_domain_verified_email_and_matching_issuer(monkeypatch):
    ok = {"iss": MS_ISS, "tid": TID, "oid": "o-1", "sub": "s-1", "email": "a@example.com", "xms_edov": True}
    assert sso.verified_identity("microsoft", ok) == {"email": "a@example.com", "name": "", "subject": f"{TID}:o-1"}
    without_edov = {k: v for k, v in ok.items() if k != "xms_edov"}
    for bad in (without_edov, {**ok, "xms_edov": False}, {**ok, "email": ""},
                {**ok, "iss": "https://login.microsoftonline.com/{tenantid}/v2.0"},
                {**ok, "iss": "https://login.microsoftonline.com/99999999-2222-3333-4444-555555555555/v2.0"},
                {**ok, "tid": "not-a-guid"}):
        with pytest.raises(sso.SSOError):
            sso.verified_identity("microsoft", bad)
    # personal accounts (consumer tenant) need no xms_edov; the issuer must still match
    msa_tid = sso.MSA_CONSUMER_TENANT
    msa = {**without_edov, "tid": msa_tid, "iss": f"https://login.microsoftonline.com/{msa_tid}/v2.0"}
    assert sso.verified_identity("microsoft", msa)["subject"] == f"{msa_tid}:o-1"
    with pytest.raises(sso.SSOError):  # consumer tid with a work tenant's issuer
        sso.verified_identity("microsoft", {**msa, "iss": MS_ISS})
    # a single-tenant deployment refuses every other tenant, verified email or not
    monkeypatch.setattr(sso, "MICROSOFT_TENANT", "99999999-2222-3333-4444-555555555555")
    with pytest.raises(sso.SSOError):
        sso.verified_identity("microsoft", ok)


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def provider(monkeypatch, request):
    """Register a fake provider under the name given by the test (default google)."""
    name = getattr(request, "param", "google")
    issuer = "https://accounts.google.com" if name == "google" else "https://login.microsoftonline.com/{tenantid}/v2.0"
    sso.oauth.register(
        name, overwrite=True, client_id=CLIENT_ID, client_secret="s",
        authorize_url="https://idp.example/authorize", access_token_url="https://idp.example/token",
        issuer=issuer, jwks={"keys": [KEY.as_dict(is_private=False)]},
        id_token_signing_alg_values_supported=["RS256"],
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    sso.oauth._clients.pop(name, None)
    monkeypatch.setitem(sso.LABELS, name, name.title())
    yield name
    sso.oauth._clients.pop(name, None)
    sso.oauth._registry.pop(name, None)


@pytest.fixture()
def client():
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture()
def saved_users(monkeypatch):
    """Stand in for Postgres: capture what would be upserted."""
    calls = []

    def upsert(email, name, provider, provider_subject):
        calls.append({"email": email, "name": name, "provider": provider, "subject": provider_subject})
        return {"id": 7, "email": email, "name": name, "tier": "trial"}

    monkeypatch.setattr(store, "upsert_user", upsert)
    monkeypatch.setattr("backend.services.login_log.log_login", lambda *a, **k: None)
    return calls


def _start(client, name, query=""):
    r = client.get(f"/auth/login/{name}{query}")
    assert r.status_code == 302, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)


def _finish(client, monkeypatch, name, q, claims, key=KEY):
    payload = {"aud": CLIENT_ID, "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": q["nonce"][0], **claims}
    id_token = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, key).decode()

    async def fake_fetch(self, **kwargs):
        assert kwargs["code"] == "the-code" and kwargs["code_verifier"]  # PKCE verifier is sent
        return {"access_token": "at", "token_type": "Bearer", "id_token": id_token}

    monkeypatch.setattr(type(sso.client(name)), "fetch_access_token", fake_fetch)
    return client.get(f"/auth/callback/{name}", params={"code": "the-code", "state": q["state"][0]})


def test_unconfigured_provider_is_404_and_not_listed(client):
    assert client.get("/auth/login/facebook").status_code == 404
    assert client.get("/auth/callback/facebook").status_code == 404
    assert client.get("/auth/login/magic").status_code == 404  # the magic route is POST-only; never an SSO provider
    assert all(p["id"] != "facebook" for p in client.get("/auth/providers").json()["providers"])


def test_redirect_carries_state_nonce_pkce(client, provider):
    assert {"id": "google", "label": "Google"} in client.get("/auth/providers").json()["providers"]
    q = _start(client, provider)
    assert q["client_id"] == [CLIENT_ID] and q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0] and q["state"][0] and q["nonce"][0]
    assert q["redirect_uri"][0].endswith("/auth/callback/google")
    assert set(q["scope"][0].split()) == {"openid", "email", "profile"}


def test_google_sign_in_sets_session(client, provider, saved_users, monkeypatch):
    q = _start(client, provider, "?next=/app/search%3Fq%3Dx&remember=1")
    r = _finish(client, monkeypatch, provider, q, {
        "iss": "https://accounts.google.com", "sub": "g-1", "email": "User@Example.com", "email_verified": True, "name": "U"})
    assert r.status_code == 303 and r.headers["location"] == "/app/search?q=x"
    assert saved_users == [{"email": "user@example.com", "name": "U", "provider": "google", "subject": "g-1"}]
    session = decode_session_token(client.cookies.get("session"))
    assert session["email"] == "user@example.com" and session["id"] == 7
    assert "Max-Age=2592000" in r.headers["set-cookie"]  # remember=1


@pytest.mark.parametrize("claims", [
    {"iss": "https://accounts.google.com", "sub": "g-1", "email": "user@example.com", "email_verified": False},
    {"iss": "https://evil.example", "sub": "g-1", "email": "user@example.com", "email_verified": True},
])
def test_google_sign_in_refused(client, provider, saved_users, monkeypatch, claims):
    r = _finish(client, monkeypatch, provider, _start(client, provider), claims)
    assert r.status_code == 303 and r.headers["location"] == "/login.html?error=sso_failed"
    assert saved_users == [] and not client.cookies.get("session")


def test_forged_signature_wrong_audience_and_replayed_state_are_refused(client, provider, saved_users, monkeypatch):
    good = {"iss": "https://accounts.google.com", "sub": "g-1", "email": "user@example.com", "email_verified": True}
    r = _finish(client, monkeypatch, provider, _start(client, provider), good, key=OTHER_KEY)
    assert r.headers["location"] == "/login.html?error=sso_failed"
    r = _finish(client, monkeypatch, provider, _start(client, provider), {**good, "aud": "someone-else"})
    assert r.headers["location"] == "/login.html?error=sso_failed"
    q = _start(client, provider)
    r = _finish(client, monkeypatch, provider, {**q, "nonce": ["not-the-nonce"]}, good)
    assert r.headers["location"] == "/login.html?error=sso_failed"
    assert saved_users == []
    # a state this browser never started (login CSRF) and a state used twice
    assert client.get("/auth/callback/google", params={"code": "c", "state": "made-up"}).headers["location"] == "/login.html?error=sso_failed"
    q = _start(client, provider)
    assert _finish(client, monkeypatch, provider, q, good).headers["location"] == "/app"
    assert _finish(client, monkeypatch, provider, q, good).headers["location"] == "/login.html?error=sso_failed"
    assert len(saved_users) == 1


def test_open_redirect_next_is_dropped(client, provider, saved_users, monkeypatch):
    q = _start(client, provider, "?next=https://evil.example/")
    r = _finish(client, monkeypatch, provider, q, {
        "iss": "https://accounts.google.com", "sub": "g-1", "email": "user@example.com", "email_verified": True})
    assert r.headers["location"] == "/app"


@pytest.mark.parametrize("provider", ["microsoft"], indirect=True)
def test_microsoft_sign_in(client, provider, saved_users, monkeypatch):
    claims = {"iss": MS_ISS, "tid": TID, "oid": "o-1", "sub": "s-1", "email": "user@example.com", "name": "U"}
    # No xms_edov: the email is unverified, so this must not reach the account.
    r = _finish(client, monkeypatch, provider, _start(client, provider), claims)
    assert r.headers["location"] == "/login.html?error=sso_failed" and saved_users == []
    r = _finish(client, monkeypatch, provider, _start(client, provider), {**claims, "xms_edov": True})
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert saved_users == [{"email": "user@example.com", "name": "U", "provider": "microsoft", "subject": f"{TID}:o-1"}]
