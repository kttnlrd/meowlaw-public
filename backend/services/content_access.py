"""Runtime content-type availability: admin global toggles + per-user grants.

A layer on top of source resolution (backend/data_sources.py), not a replacement:
a type that is configured and loaded can still be switched off here at runtime.

available iff global_enabled(key) AND (open-to-users OR admin OR granted), where
open-to-users = auth_of(key) == "user" (registry auth, or PSEUDO_AUTH for features).
DB outage: global toggles fail open (search keeps working) but admin-only types fail
closed for non-admins, so an outage never turns private content public.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from backend.config import is_admin_email
from backend.db import cursor, safe
from content.registry import REGISTRY

# Features that are not registry content types but are switchable alongside them.
PSEUDO = {"maps": "Procedural maps", "workflows": "Workflows", "chains": "Interpretive chains"}
# All three are private: admins and granted users only.
PSEUDO_AUTH = {"maps": "admin", "workflows": "admin", "chains": "admin"}


def all_type_keys() -> list[str]:
    return sorted(REGISTRY) + list(PSEUDO)


def label(key: str) -> str:
    return REGISTRY[key]["label"] if key in REGISTRY else PSEUDO[key]


def auth_of(key: str) -> str:
    return REGISTRY[key]["auth"] if key in REGISTRY else PSEUDO_AUTH[key]


@safe(None)
def _settings() -> dict[str, bool]:
    with cursor() as cur:
        cur.execute("SELECT key, enabled FROM content_type_settings")
        return {r["key"]: bool(r["enabled"]) for r in cur.fetchall()}


@safe(None)
def _grants(email: str) -> set[str]:
    with cursor() as cur:
        cur.execute("SELECT content_key FROM user_content_grants WHERE email = %s", (email.lower(),))
        return {r["content_key"] for r in cur.fetchall()}


def global_enabled(key: str) -> bool:
    return (_settings() or {}).get(key, True)


def global_settings() -> dict[str, bool]:
    s = _settings() or {}
    return {k: s.get(k, True) for k in all_type_keys()}


def grants_for(email: str) -> set[str]:
    return _grants(email) or set()


def _check(key: str) -> None:
    if key not in REGISTRY and key not in PSEUDO:
        raise KeyError(key)


def set_global(key: str, enabled: bool) -> None:
    """Raises on DB error — a failed save must not look like success."""
    _check(key)
    with cursor() as cur:
        cur.execute("INSERT INTO content_type_settings (key, enabled) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET enabled = EXCLUDED.enabled", (key, bool(enabled)))


def grant(email: str, key: str) -> None:
    _check(key)
    with cursor() as cur:
        cur.execute("INSERT INTO user_content_grants (email, content_key) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (email.lower(), key))


def revoke(email: str, key: str) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM user_content_grants WHERE email = %s AND content_key = %s",
                    (email.lower(), key))


def set_grants(email: str, keys: list[str]) -> None:
    """Replace a user's grant set in one transaction."""
    for k in keys:
        _check(k)
    with cursor() as cur:
        # Serialise overlapping replaces for one email; ON CONFLICT keeps the insert idempotent.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (email.lower(),))
        cur.execute("DELETE FROM user_content_grants WHERE email = %s", (email.lower(),))
        for k in sorted(set(keys)):
            cur.execute("INSERT INTO user_content_grants (email, content_key) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (email.lower(), k))


def compute(keys: list[str], settings: dict[str, bool], grants: set[str], admin: bool) -> set[str]:
    """Pure availability rule (kept separate so it is testable without Postgres)."""
    return {k for k in keys
            if settings.get(k, True) and (auth_of(k) == "user" or admin or k in grants)}


def available_keys(user: dict | None) -> set[str]:
    # ponytail: two Postgres round trips per call (per search request); add a short TTL cache if it shows up in latency.
    keys = all_type_keys()
    email = ((user or {}).get("email") or "").lower()
    settings = _settings()
    grants = _grants(email) if email else set()
    # ponytail: DB unreachable -> toggles fail open ({}), grants fail closed (set()).
    return compute(keys, settings or {}, grants or set(), is_admin_email(email))


def user_from(request: Request) -> dict | None:
    if request is None:
        return None
    return getattr(request.state, "user", None)


def is_available(request: Request, key: str) -> bool:
    return key in available_keys(user_from(request))


def require_available(request: Request, key: str) -> None:
    """FastAPI dependency body: 404 when `key` is off for this user."""
    if not is_available(request, key):
        raise HTTPException(404, f"{label(key)} is not available")


def gate(key: str):
    """Router/route dependency: `APIRouter(dependencies=[Depends(gate("maps"))])`."""
    def dep(request: Request) -> None:
        require_available(request, key)
    return dep


def hit_key(row: dict) -> str | None:
    """A hit's content key; 'legislation' rows resolve by act so commentary-as-act is caught."""
    ct = row.get("content_type")
    if ct == "legislation" and row.get("act"):
        return act_key(row["act"])
    return ct


def filter_hits(user: dict | None, rows: list[dict]) -> list[dict]:
    """Drop hits whose content_type is unavailable; untagged rows are kept."""
    avail = available_keys(user)
    return [r for r in rows if not hit_key(r) or hit_key(r) in avail]



# --- mapping other id vocabularies onto content keys (for gating non-search surfaces) ---

# Graph node kinds (graph.db `node_type` / key prefix) and smart-link item types.
NODE_TYPE_KEY = {
    "section": "legislation", "part": "legislation", "definition": "legislation",
    "public_ruling": "ruling", "ruling": "ruling", "nz_ruling": "nz-ruling",
    "private_ruling": "ato-private-ruling", "case": "case",
    "regulatory_guide": "regulatory-guide", "afsa_guide": "afsa-guide",
    "commentary": "commentary", "treaty": "treaty",
    "insolvency": "insolvency", "nz_mtg": "nz-mtg",
    # MCP `related` item types
    "legislation": "legislation", "rg": "regulatory-guide", "afsa": "afsa-guide",
}

# Pseudo-act ids used by /api/acts, /api/tree/{act}, /api/section/{act}/...
_PSEUDO_ACTS = {
    "rulings": "ruling", "nz-rulings": "nz-ruling", "tax-cases": "case",
    "private-rulings": "ato-private-ruling", "regulatory-guides": "regulatory-guide",
    "afsa-guides": "afsa-guide", "treaties": "treaty",
}


def act_key(act: str) -> str:
    """Content key for an act id: pseudo-acts, optional types by data_markers, else legislation.

    Case-insensitive: lookups downstream match lower(key), so "KEAYS" must gate like "keays".
    """
    act = (act or "").lower()
    if act in _PSEUDO_ACTS:
        return _PSEUDO_ACTS[act]
    for k, t in REGISTRY.items():
        if act in (m.lower() for m in t.get("data_markers", [])):
            return k
    return "legislation"


def node_key(key_or_type: str) -> str | None:
    """Content key for a graph key ('section:itaa-1997:8-1') or bare node type; None = untyped.

    The prefix is lower-cased: graph lookups fall back to lower(key), so an upper-cased
    "INSOLVENCY:..." must not read as an unknown (ungated) type.
    """
    kind, _, rest = (key_or_type or "").partition(":")
    kind = kind.lower()
    if kind == "section" and rest:
        return act_key(rest.split(":", 1)[0])
    return NODE_TYPE_KEY.get(kind)


def node_allowed(avail: set[str], key_or_type: str) -> bool:
    k = node_key(key_or_type)
    return k is None or k in avail


def denied_node_types(user: dict | None) -> set[str]:
    avail = available_keys(user)
    return {nt for nt, k in NODE_TYPE_KEY.items() if k not in avail}


def require_act(request: Request, act: str) -> None:
    require_available(request, act_key(act))

if __name__ == "__main__":
    keys = all_type_keys()
    assert len(keys) == len(REGISTRY) + 3 and {"maps", "workflows", "chains"} <= set(keys)
    admin_only = [k for k in keys if auth_of(k) == "admin"]
    assert {"maps", "workflows", "chains"} <= set(admin_only)
    s = {"case": False}
    plain = compute(keys, s, set(), admin=False)
    assert "case" not in plain and not set(admin_only) & plain
    assert "maps" in compute(keys, s, {"maps"}, admin=False)
    assert "case" not in compute(keys, s, {"case"}, admin=True)  # global off beats grant + admin
    if admin_only:
        assert admin_only[0] in compute(keys, s, {admin_only[0]}, admin=False)
        assert set(admin_only) <= compute(keys, s, set(), admin=True)
    outage = compute(keys, {}, set(), admin=False)  # what available_keys() uses on DB failure
    assert not set(admin_only) & outage
    assert node_key("insolvency") == "insolvency" and node_key("nz_mtg") == "nz-mtg"
    assert node_key("INSOLVENCY:keays:ch-12") == "insolvency" and node_key("Commentary:x") == "commentary"
    assert not node_allowed(outage, "NZ_MTG:x") and not node_allowed(outage, "Insolvency:keays:ch-1")
    assert act_key("rulings") == "ruling" and act_key("itaa-1997") == "legislation"
    assert node_key("public_ruling:TR 2024/1") == "ruling" and node_key("section:itaa-1997:8-1") == "legislation"
    assert node_key("private_ruling") == "ato-private-ruling" and node_key("weird:x") is None
    if "master-tax-guide" in REGISTRY.get("commentary", {}).get("data_markers", []):
        assert act_key("master-tax-guide") == "commentary"
    print("ok", sorted(plain))
