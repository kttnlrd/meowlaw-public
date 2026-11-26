"""Content-type availability: per-user view + admin toggles/grants (services/content_access.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.auth import require_user
from backend.auth.store import email_exists
from backend.auth import require_admin
from backend.services import content_access as ca

router = APIRouter()


class Toggle(BaseModel):
    enabled: bool


class Grants(BaseModel):
    keys: list[str]


def _rows(available: set[str]) -> list[dict]:
    settings = ca.global_settings()
    return [{"key": k, "label": ca.label(k), "auth": ca.auth_of(k),
             "enabled": settings[k], "available": k in available}
            for k in ca.all_type_keys()]


@router.get("/api/content-types")
def my_content_types(request: Request):
    return _rows(ca.available_keys(require_user(request)))


def _save(fn, *args) -> None:
    """Run a write; unknown key -> 400, DB failure -> 503 (not a raw 500)."""
    try:
        fn(*args)
    except KeyError as e:
        raise HTTPException(400, f"Unknown content type {e.args[0]!r}")
    except Exception:
        raise HTTPException(503, "Save failed: content settings database unavailable")


def _known_user(email: str) -> str:
    try:
        ok = email_exists(email)
    except Exception:
        raise HTTPException(503, "User database unavailable")
    if not ok:
        raise HTTPException(404, f"No user {email!r}")
    return email.lower()


@router.get("/api/admin/content-types")
def admin_content_types(request: Request):
    user = require_admin(request)
    if ca._settings() is None:  # don't report a guessed all-On state
        raise HTTPException(503, "Content settings unavailable")
    return _rows(ca.available_keys(user))


@router.put("/api/admin/content-types/{key}")
def admin_toggle(key: str, body: Toggle, request: Request):
    require_admin(request)
    try:
        ca._check(key)
    except KeyError:
        raise HTTPException(404, f"Unknown content type {key!r}")
    _save(ca.set_global, key, body.enabled)
    return {"key": key, "enabled": body.enabled}


@router.get("/api/admin/users/{email}/content-types")
def admin_user_grants(email: str, request: Request):
    require_admin(request)
    email = _known_user(email)
    granted = ca._grants(email)
    if granted is None:  # an empty set here would invite a destructive save
        raise HTTPException(503, "Grants unavailable")
    return {"email": email, "granted": sorted(granted)}


@router.put("/api/admin/users/{email}/content-types")
def admin_set_user_grants(email: str, body: Grants, request: Request):
    require_admin(request)
    email = _known_user(email)
    _save(ca.set_grants, email, body.keys)
    return {"email": email, "granted": sorted(set(body.keys))}


@router.post("/api/admin/users/{email}/content-types/{key}")
def admin_grant(email: str, key: str, request: Request):
    require_admin(request)
    _save(ca.grant, _known_user(email), key)
    return {"email": email.lower(), "key": key, "granted": True}


@router.delete("/api/admin/users/{email}/content-types/{key}")
def admin_revoke(email: str, key: str, request: Request):
    require_admin(request)
    _save(ca.revoke, _known_user(email), key)
    return {"email": email.lower(), "key": key, "granted": False}
