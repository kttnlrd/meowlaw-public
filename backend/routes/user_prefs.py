"""API routes for user preferences."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.services.user_prefs import get_prefs, update_prefs, reset_prefs

router = APIRouter()


def _get_user(request: Request) -> dict | None:
    try:
        from backend.auth import require_user
        return require_user(request)
    except Exception:
        return None


class PrefsUpdate(BaseModel):
    display_name: str | None = None
    default_act: str | None = None
    theme: str | None = None
    accent_color: str | None = None
    heading_font: str | None = None
    body_font: str | None = None


@router.get("/api/user/prefs")
def get_user_prefs(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    email = user.get("email") or user.get("name", "")
    return get_prefs(email)


@router.put("/api/user/prefs")
def update_user_prefs(body: PrefsUpdate, request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    email = user.get("email") or user.get("name", "")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return update_prefs(email, updates)


@router.post("/api/user/prefs/reset")
def reset_user_prefs(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)
    email = user.get("email") or user.get("name", "")
    return reset_prefs(email)