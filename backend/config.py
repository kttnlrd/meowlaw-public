"""Centralised configuration."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

# Base paths
BASE = Path(__file__).resolve().parents[1]
DATA_DIR = BASE / "data"
FRONTEND_DIST = BASE / "frontend" / "dist"
SEARCH_DB = BASE / "search_index.db"

# Content sources come from the data_sources manifest (plan §3); see
# data_sources.yaml / data_sources.local.yaml and backend/data_sources.py.
from backend import data_sources  # noqa: E402

MANIFEST = data_sources.manifest()
SOURCES = data_sources.resolve(MANIFEST)
INSTANCE = MANIFEST.get("instance") or "lawkitty"


def _src(key: str) -> Path:
    # ponytail: an unconfigured type gets a path that never exists, so the
    # existing .exists()/.glob() call sites stay empty instead of crashing.
    return SOURCES[key].path or BASE / ".not_configured" / key


CASE_DIR = _src("case")
# ATO_RULING_DIR (td/tr/pcg/ps_la subdirs) has always defaulted to RULING_DIR.
RULING_DIR = ATO_RULING_DIR = _src("ruling")
# New Zealand (IRD Tax Technical) rulings and rulings-shaped publications. Kept in their
# own directory so they can be tagged jurisdiction="NZ" and never satisfy an AU search
# scope. Written by scripts/nz_scrape_rulings.py as <TYPE>_<YEAR>_<N>.txt plus a
# nz_rulings_list.json manifest.
NZ_RULING_DIR = _src("nz-ruling")
TREATIES_DIR = _src("treaty")
# ATO private rulings: <authnum>.json; the original HTML pages sit in ../html.
PRIVATE_RULINGS_DIR = _src("ato-private-ruling")

# Security
BEARER_TOKEN = os.environ.get("LEGISLATION_BEARER_TOKEN")

# The app's own base URL decides "production" — read raw, so an unset value
# (pytest, a bare `python -c`) is not mistaken for the lawkitty.app default.
_BASE_URL_RAW = os.environ.get("APP_BASE_URL", "").strip()
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
BASE_URL_IS_LOCAL = (urlsplit(_BASE_URL_RAW).hostname or "") in _LOCAL_HOSTS
IS_PRODUCTION = _BASE_URL_RAW.startswith("https://") and not BASE_URL_IS_LOCAL

# §3.3: a configured-but-absent source is an ERROR (logged by resolve); in
# production it stops startup rather than silently serving an empty type.
_MISSING_SOURCES = sorted(k for k, s in SOURCES.items() if s.status == "missing")
if IS_PRODUCTION and _MISSING_SOURCES:
    raise RuntimeError(f"data sources missing in production: {_MISSING_SOURCES} "
                       "(python -m backend.data_sources --dump)")


def _is_local_origin(origin: str) -> bool:
    return (urlsplit(origin).hostname or "") in _LOCAL_HOSTS


# Dev-server origins are only trusted when the app itself runs locally — even if
# ALLOWED_ORIGINS lists them, a non-local deployment never accepts them.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://lawkitty.app,https://www.lawkitty.app,http://localhost:5173,http://localhost:3000",
    ).split(",")
    if o.strip() and (BASE_URL_IS_LOCAL or not _is_local_origin(o.strip()))
]

# Admins: comma-separated ADMIN_EMAILS. Empty by default: nobody is admin unless configured.
ADMIN_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
)


def is_admin_email(email: str | None) -> bool:
    return (email or "").strip().lower() in ADMIN_EMAILS

