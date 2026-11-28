"""Shared test helpers for the SSO session-cookie auth model.

After commit 4a60ca291 every data route under ``/api/*`` requires a valid
session (``Depends(require_user)``), so tests must mint a signed session
cookie instead of toggling the now-dead ``config.BEARER_TOKEN``.

Admin-gated content (e.g. ``commentary``) is visible only to an email in
``config.ADMIN_EMAILS``; use ``admin_token()`` for those tests.
"""

from __future__ import annotations

from backend import config
from backend.auth import create_session_token

TEST_EMAIL = "test@lawkitty.local"


def session_token(email: str = TEST_EMAIL, tier: str = "trial") -> str:
    """A signed session-cookie value for a user."""
    return create_session_token(
        {"id": 1, "sub": "", "name": "Test User", "email": email, "tier": tier}
    )


def authed_cookie(email: str = TEST_EMAIL, tier: str = "trial") -> dict[str, str]:
    """``Cookie`` header for an authenticated request."""
    return {"Cookie": f"session={session_token(email, tier)}"}


def admin_email() -> str:
    """A configured admin email (content-access checks, not the JWT tier)."""
    return next(iter(config.ADMIN_EMAILS))


def admin_token() -> str:
    """A session cookie for an admin user (sees admin-gated content)."""
    return session_token(admin_email(), tier="admin")
