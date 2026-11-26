#!/usr/bin/env python3.12
"""Mint a signed session cookie without SSO. Run on prod with the app env sourced.

    ssh $DEPLOY_HOST
    set -a; . /etc/lawkitty/lawkitty.env; set +a
    /opt/lawkitty/venv/bin/python scripts/mint_session.py [--remember]

Signs an admin session via the same create_session_token() a login callback calls, so it
passes require_user unchanged. Skips only the interactive login. Never prints the secret.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.auth import create_session_token  # noqa: E402
from backend.config import ADMIN_EMAILS  # noqa: E402

# An admin email alone drives full content access (is_admin_email); no DB lookup needed.
claims = {"id": 1, "sub": "", "name": "Prod smoke test",
          "email": sorted(ADMIN_EMAILS)[0], "tier": "admin"}
print("session=" + create_session_token(claims, remember="--remember" in sys.argv))
