"""Magic-link email delivery via Resend.

Uses stdlib urllib.request only (no new deps). If RESEND_API_KEY is unset,
logs the magic-link URL instead of sending (dev mode) — never raises.
Returns True only when the email was actually handed to Resend successfully.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def _from_address() -> str:
    addr = os.environ.get("RESEND_FROM") or "meow@lawkitty.app"
    return f"lawkitty <{addr}>"


def send_magic_link(email: str, url: str, code: str = "") -> bool:
    """Send (or dev-log) a magic-link login email. Returns True if actually sent."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.info("Magic link (dev mode, no RESEND_API_KEY set) for %s: %s", email, url)
        return False

    body = {
        "from": _from_address(),
        "to": [email],
        "subject": "Your lawkitty sign-in",
        "html": (
            f'<p>Two ways to sign in to lawkitty:</p>'
            f'<p><strong>1. Enter this code</strong> on the sign-in page:</p>'
            f'<p style="font-size:32px;font-weight:700;letter-spacing:10px;margin:16px 0">{code}</p>'
            f'<p><strong>2. Or click this link</strong> to sign in on this device:</p>'
            f'<p><a href="{url}">{url}</a></p>'
            f'<p style="margin-top:20px;font-size:13px;color:#8a8a8a">Both expire in 15 minutes. If you did not request this, you can ignore this email.</p>'
        ),
    }

    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "lawkitty/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger.info("Magic link sent via Resend to %s", email)
        return True
    except urllib.error.URLError:
        logger.exception("Failed to send magic-link email via Resend to %s", email)
        return False
