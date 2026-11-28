"""Regression tests for the auth / file-serving hardening pass.

- /app/{path} must never serve a file outside frontend/dist.
- OAuth: PKCE is mandatory, codes and refresh tokens redeem once, redirect_uri is bound.
- Login codes: a wrong-guess cap and a cap on outstanding codes per address (needs Postgres).
"""
from __future__ import annotations

import base64
import hashlib
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from backend import db, main  # noqa: E402
from backend.auth import _safe_next, store  # noqa: E402
from backend.main import app  # noqa: E402
from backend.oauth_provider import provider  # noqa: E402
from backend.services.data_loader import is_plain_id  # noqa: E402
from backend.tests.helpers import TEST_EMAIL, session_token  # noqa: E402

REDIRECT = "https://client.example/cb"


@pytest.fixture()
def anon():
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def user():
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("session", session_token())
    return c


# ── /app file serving ────────────────────────────────────────────────────────

needs_dist = pytest.mark.skipif(not main.FRONTEND_DIST.exists(), reason="frontend not built")


@needs_dist
@pytest.mark.parametrize("path", [
    "/app/%2fetc%2fpasswd",
    "/app/..%2f..%2fbackend%2fconfig.py",
    "/app/assets%2f..%2f..%2f..%2fbackend%2fconfig.py",
    "/app/favicon%2f..%2f..%2f..%2fbackend%2fconfig.py",
])
def test_app_fallback_never_leaves_dist(user, path):
    r = user.get(path)
    # Either the SPA shell (fallback route) or a 404 (assets mount) — never the file asked for.
    index = (main.FRONTEND_DIST / "index.html").read_bytes()
    assert (r.status_code, r.content) == (200, index) or r.status_code == 404


@needs_dist
def test_app_fallback_public_assets_are_top_level_only(anon):
    assert anon.get("/app/favicon%2f..%2findex.html").status_code == 307  # login redirect
    assert anon.get("/app/some/page").status_code == 307


def test_is_plain_id():
    assert all(is_plain_id(x) for x in ("itaa-1997", "usa", "nz-it-2007", "aml_ctf.v2"))
    assert not any(is_plain_id(x) for x in ("..", ".", "", "a/b", "../x", ".env", "a..b", "a/", None))


def test_safe_next():
    assert _safe_next("/app/x?y=1") == "/app/x?y=1"
    assert all(_safe_next(x) is None for x in ("//evil.example", "/\\evil.example", "https://evil.example", "javascript:1", None, 5))
    # browsers strip tab/CR/LF before parsing: "/\t/evil" is "//evil"
    assert all(_safe_next(x) is None for x in ("/\t/evil.example", "/\n/evil.example", "/\r/evil.example",
                                               "/ /evil.example", "/app\\..\\x", "/\x00"))
    assert _safe_next("/oauth/authorize?client_id=a&redirect_uri=https%3A%2F%2Fx%2Fcb") is not None


# ── OAuth ────────────────────────────────────────────────────────────────────

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture()
def oauth_client():
    # Straight to the provider: POST /oauth/register is limited to 10 an hour per IP.
    info = provider.register_client({"client_name": "t", "redirect_uris": [REDIRECT]})
    assert "client_secret" not in info
    return info["client_id"]


def _authorize(user, client_id, challenge):
    r = user.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 200, r.text
    token = r.text.split("name='consent_token' value=\"")[1].split("\"")[0]
    r = user.post("/oauth/authorize/consent", data={"consent_token": token})
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["s1"]
    return q["code"][0]


@pytest.mark.parametrize("body", [[], "x", {"redirect_uris": "https://a.example/cb"}, {"redirect_uris": [5]},
                                  {"redirect_uris": ["http://evil.example/cb"]}, {"redirect_uris": []},
                                  {"redirect_uris": ["https://a.example/cb"] * 11}])
def test_register_rejects_bad_metadata(body):
    with pytest.raises(ValueError):
        provider.register_client(body)


def test_authorize_requires_pkce(user, oauth_client):
    r = user.get("/oauth/authorize", params={"client_id": oauth_client, "redirect_uri": REDIRECT, "response_type": "code"})
    # 400, never a redirect: /oauth/register is open, so redirect_uri can be anyone's site
    assert r.status_code == 400 and "location" not in r.headers and r.json()["error"] == "invalid_request"
    r = user.get("/oauth/authorize", params={"client_id": oauth_client, "redirect_uri": REDIRECT, "response_type": "code",
                                             "code_challenge": "x", "code_challenge_method": "plain"})
    assert r.status_code == 400 and "location" not in r.headers


def test_authorize_signed_out_goes_to_login_with_relative_next(anon, oauth_client):
    _, challenge = _pkce()
    r = anon.get("/oauth/authorize", params={"client_id": oauth_client, "redirect_uri": REDIRECT, "response_type": "code",
                                             "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/login.html" and not loc.netloc
    nxt = parse_qs(loc.query)["next"][0]
    assert _safe_next(nxt) == nxt and nxt.startswith("/oauth/authorize?")


def test_code_flow_and_single_use(user, anon, oauth_client):
    verifier, challenge = _pkce()
    code = _authorize(user, oauth_client, challenge)
    form = {"grant_type": "authorization_code", "client_id": oauth_client, "code": code, "code_verifier": verifier}
    r = anon.post("/oauth/token", data=form)
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert provider.load_access_token(tokens["access_token"])["user_email"] == TEST_EMAIL
    assert anon.post("/oauth/token", data=form).status_code == 400  # code redeems once

    refresh = {"grant_type": "refresh_token", "client_id": oauth_client, "refresh_token": tokens["refresh_token"]}
    r = anon.post("/oauth/token", data=refresh)
    assert r.status_code == 200
    assert anon.post("/oauth/token", data=refresh).status_code == 400  # rotated: old one is dead
    other = {**refresh, "refresh_token": r.json()["refresh_token"], "client_id": "lk_someone_else"}
    assert anon.post("/oauth/token", data=other).status_code == 400  # bound to the issuing client


@pytest.mark.parametrize("tamper", [{"code_verifier": "wrong"}, {"code_verifier": ""}, {"redirect_uri": "https://evil.example/cb"},
                                    {"client_id": "lk_someone_else"}])
def test_code_refused_on_mismatch(user, anon, oauth_client, tamper):
    verifier, challenge = _pkce()
    code = _authorize(user, oauth_client, challenge)
    form = {"grant_type": "authorization_code", "client_id": oauth_client, "code": code, "code_verifier": verifier, **tamper}
    assert anon.post("/oauth/token", data=form).status_code == 400


def test_revoke_all_for_user(user, anon, oauth_client):
    verifier, challenge = _pkce()
    code = _authorize(user, oauth_client, challenge)
    tokens = anon.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": oauth_client,
                                             "code": code, "code_verifier": verifier}).json()
    provider.revoke_all_for_user(TEST_EMAIL)
    assert provider.load_access_token(tokens["access_token"]) is None
    assert provider.exchange_refresh_token(tokens["refresh_token"]) is None


# ── Login codes (Postgres) ───────────────────────────────────────────────────

def _pg_up() -> bool:
    if not db.DATABASE_URL:
        return False
    try:
        db.init_db()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(), reason="LAWKITTY_DATABASE_URL not reachable")


@pytest.fixture()
def email():
    addr = f"hardening-{os.urandom(4).hex()}@lawkitty.local"
    yield addr
    with db.cursor() as cur:
        cur.execute("DELETE FROM magic_tokens WHERE email = %s", (addr,))


@needs_pg
def test_code_and_link_are_single_use(email):
    token, code = store.create_magic_token(email)
    assert store.consume_magic_token_by_code(email, code)["email"] == email
    assert store.consume_magic_token_by_code(email, code) is None
    assert store.consume_magic_token_row(token) is None
    token, _ = store.create_magic_token(email, remember=True, next_url="/app/x")
    assert store.consume_magic_token_row(token) == {"email": email, "remember": True, "next_url": "/app/x"}
    assert store.consume_magic_token_row(token) is None


@needs_pg
def test_wrong_codes_lock_the_code_but_not_the_link(email):
    token, code = store.create_magic_token(email)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(store.MAX_CODE_ATTEMPTS):
        assert store.consume_magic_token_by_code(email, wrong) is None
    assert store.consume_magic_token_by_code(email, code) is None  # right code, too late
    assert store.consume_magic_token_row(token)["email"] == email  # the emailed link still works


@needs_pg
def test_outstanding_codes_are_capped_per_address(email):
    for _ in range(store.MAX_LIVE_TOKENS):
        assert store.create_magic_token(email) is not None
    assert store.create_magic_token(email) is None


@needs_pg
def test_expired_token_is_refused(email):
    token, code = store.create_magic_token(email)
    with db.cursor() as cur:
        cur.execute("UPDATE magic_tokens SET expires_at = %s WHERE token = %s", ("2000-01-01T00:00:00+00:00", token))
    assert store.consume_magic_token_by_code(email, code) is None
    assert store.consume_magic_token_row(token) is None
