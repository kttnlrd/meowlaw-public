"""Security hardening: session secret fails closed, Origin check, CORS pairing, CSP, webhook."""
import hashlib
import hmac
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ["RATE_LIMIT_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from backend import billing  # noqa: E402
from backend.auth import create_session_token  # noqa: E402
from backend.config import ALLOWED_ORIGINS, is_admin_email  # noqa: E402
from backend.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
client = TestClient(app)
COOKIE = {"Cookie": f"session={create_session_token({'email': 'sec-test@example.invalid', 'sub': ''})}"}


def _import_auth(env):
    code = "import backend.auth as a; print(len(a.SESSION_SECRET), a.SESSION_SECRET != 'change-me')"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], **env})


def test_session_secret_fails_closed_in_production():
    r = _import_auth({"APP_BASE_URL": "https://lawkitty.app"})
    assert r.returncode != 0 and "SESSION_SECRET is missing or shorter than 32" in r.stderr
    r = _import_auth({"APP_BASE_URL": "https://lawkitty.app", "SESSION_SECRET": "change-me"})
    assert r.returncode != 0


def test_session_secret_random_outside_production():
    for env in ({}, {"APP_BASE_URL": "http://localhost:8769"}):
        r = _import_auth(env)
        assert r.returncode == 0 and r.stdout.split() == ["64", "True"], r.stderr
        assert "random per-process secret" in r.stderr


def test_origin_check_on_cookie_writes():
    def put(headers):
        return client.post("/api/account/delete-nope", headers={**COOKIE, **headers}).status_code
    assert put({"Origin": "https://evil.example"}) == 403
    assert put({"Origin": "null"}) == 403
    # Allowed origin / same-origin fetch metadata / no Origin all get past the check to routing.
    if ALLOWED_ORIGINS:
        assert put({"Origin": ALLOWED_ORIGINS[0]}) != 403
    assert put({"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}) != 403
    assert put({}) != 403
    # No session cookie: not a CSRF target, left alone.
    assert client.post("/api/account/delete-nope", headers={"Origin": "https://evil.example"}).status_code != 403


def test_cors_credentials_paired_and_csp_enforced():
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    assert "content-security-policy" in r.headers
    assert "content-security-policy-report-only" not in r.headers


def test_webhook_refuses_unverified():
    for headers in ({}, {"Stripe-Signature": "t=1,v1=deadbeef"}):
        r = client.post("/api/billing/webhook", content=b'{"type":"x"}', headers=headers)
        assert r.status_code == 400 and r.json()["error"] == "invalid_signature"


def test_stripe_signature_scheme():
    body, secret, t = b'{"id":"evt_1"}', "whsec_test", int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    ok = f"t={t},v1={sig}"
    assert billing.verify_stripe_signature(body, ok, secret)
    assert not billing.verify_stripe_signature(body, ok, "")  # unconfigured: nothing verifies
    assert not billing.verify_stripe_signature(body + b" ", ok, secret)
    assert not billing.verify_stripe_signature(body, ok, secret, now=t + 301)


def test_admin_allowlist_from_env(monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "ADMIN_EMAILS", frozenset({"admin@example.com"}))
    assert is_admin_email("Admin@Example.com")
    assert not is_admin_email("someone@example.com") and not is_admin_email(None)


def test_admin_default_is_empty():
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_EMAILS"}
    code = "from backend.config import ADMIN_EMAILS, is_admin_email; print(len(ADMIN_EMAILS), is_admin_email('a@b.c'))"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["0", "False"], out.stderr
