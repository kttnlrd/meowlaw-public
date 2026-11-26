"""Email magic-link login — not an OAuth provider.

request_magic_link(email) creates a single-use token and emails the login
link; consume(token) verifies it and returns the associated email.
"""
from __future__ import annotations

import os

from backend.auth.store import create_magic_token, consume_magic_token_row, consume_magic_token_by_code
from backend.auth.emailer import send_magic_link


def request_magic_link(email: str, remember: bool = False, next_url: str | None = None) -> bool:
    """Create a magic token for `email` and email the login code + link.

    `remember` and `next_url` are stored on the token row so the callback can
    issue a 30-day session and redirect to the right place. Returns True if the
    email was actually handed to Resend (False in dev mode or on send failure).
    """
    created = create_magic_token(email, remember, next_url)
    if created is None:  # too many outstanding sign-in emails for this address
        return False
    token, code = created
    app_base_url = os.environ.get("APP_BASE_URL", "https://lawkitty.app")
    url = f"{app_base_url}/auth/callback/magic?token={token}"
    return send_magic_link(email, url, code)


def consume(token: str) -> dict | None:
    """Verify a magic-link token. Returns {email, remember, next_url} if valid, else None."""
    return consume_magic_token_row(token)


def consume_by_code(email: str, code: str) -> dict | None:
    """Verify a typed login code. Returns {email, remember, next_url} if valid, else None."""
    return consume_magic_token_by_code(email, code)
