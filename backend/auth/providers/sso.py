"""Google and Microsoft sign-in (OpenID Connect, authorization code + PKCE).

A provider is offered only when its client id and secret are set:

    GOOGLE_CLIENT_ID      GOOGLE_CLIENT_SECRET
    MICROSOFT_CLIENT_ID   MICROSOFT_CLIENT_SECRET   MICROSOFT_TENANT (default "common")

Redirect URI to register with each provider: {APP_BASE_URL}/auth/callback/<provider>.

Accounts are keyed by email (backend/auth/store.py) and admin rights follow the
email, so a sign-in is accepted only when the provider vouches for the address:

- Google: `email_verified` must be true.
- Microsoft: the `email` claim on its own is not proof ("This value isn't guaranteed
  to be correct ... Never use it for authorization", ID token claims reference), and
  with a multi-tenant authority any tenant's admin can mint a token for any address.
  We therefore require the `xms_edov` optional claim (email domain owner verified) to
  be true. The app registration must add the `email` and `xms_edov` optional claims
  to the ID token, otherwise every work/school sign-in is refused. Personal accounts
  are the exception: their tokens carry the Microsoft-run consumer tenant
  (MSA_CONSUMER_TENANT), which no third party administers, and Microsoft verifies the
  account's sign-in address at sign-up, so xms_edov (never issued to them) is not required.

authlib validates signature, audience, expiry and nonce; this module adds the
issuer/tenant and verified-email checks on top.
"""
from __future__ import annotations

import os
import re
from typing import Any

from authlib.integrations.starlette_client import OAuth

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

MICROSOFT_TENANT = os.environ.get("MICROSOFT_TENANT", "").strip().lower() or "common"
# Fixed tenant id on every token issued to a personal Microsoft account (outlook.com etc.).
MSA_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"

oauth = OAuth()
LABELS: dict[str, str] = {}  # configured providers only, in display order


class SSOError(Exception):
    """A provider sign-in we refuse; the message is for the log, not the user."""


def _register(name: str, label: str, metadata_url: str) -> None:
    client_id = os.environ.get(f"{name.upper()}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"{name.upper()}_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return
    oauth.register(
        name,
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=metadata_url,
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    LABELS[name] = label


_register("google", "Google", "https://accounts.google.com/.well-known/openid-configuration")
_register("microsoft", "Microsoft",
          f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/v2.0/.well-known/openid-configuration")


def client(name: str):
    """The authlib client for a configured provider, else None."""
    return oauth.create_client(name) if name in LABELS else None


def token_kwargs(name: str) -> dict[str, Any]:
    """Extra arguments for authorize_access_token.

    Microsoft's multi-tenant metadata gives the issuer as a template
    (".../{tenantid}/v2.0"), which no real token matches, so authlib's own issuer
    comparison is replaced by `verified_identity`'s check against the token's `tid`.
    """
    return {"claims_options": {"iss": {"essential": True}}} if name == "microsoft" else {}


def _str(claims: dict[str, Any], key: str) -> str:
    value = claims.get(key)
    return value.strip() if isinstance(value, str) else ""


def verified_identity(name: str, claims: dict[str, Any]) -> dict[str, str]:
    """{email, name, subject} from validated ID-token claims, or raise SSOError."""
    email = _str(claims, "email").lower()
    if not email or "@" not in email:
        raise SSOError(f"{name}: no email claim")

    if name == "google":
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise SSOError("google: unexpected issuer")
        if claims.get("email_verified") is not True:
            raise SSOError("google: email not verified")
        subject = _str(claims, "sub")
    elif name == "microsoft":
        tid = _str(claims, "tid").lower()
        if not _GUID_RE.match(tid) or claims.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0":
            raise SSOError("microsoft: issuer does not match the token's tenant")
        if _GUID_RE.match(MICROSOFT_TENANT) and tid != MICROSOFT_TENANT:
            raise SSOError("microsoft: token is from another tenant")
        # Personal Microsoft accounts all carry the consumer tenant id. Microsoft
        # verifies their sign-in address itself and never issues xms_edov for them,
        # so the domain-owner check applies only to work/school tenants.
        if tid != MSA_CONSUMER_TENANT and claims.get("xms_edov") not in (True, "true", "True", "1", 1):
            raise SSOError("microsoft: email domain not verified (xms_edov missing or false)")
        subject = f"{tid}:{_str(claims, 'oid') or _str(claims, 'sub')}"
    else:
        raise SSOError(f"unknown provider {name!r}")

    if not subject.strip(":"):
        raise SSOError(f"{name}: no subject claim")
    return {"email": email, "name": _str(claims, "name")[:200], "subject": subject}
