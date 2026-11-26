"""Account + billing routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import billing
from backend.auth import require_user, store

router = APIRouter()


class NameUpdate(BaseModel):
    name: str


class CheckoutRequest(BaseModel):
    tier: str


def _current_user(request: Request) -> dict[str, Any]:
    """Session claims -> fresh DB row (session JWT may be stale or deleted)."""
    session = require_user(request)
    user = store.get_user_by_id(session.get("id")) if session.get("id") is not None else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _payload(user: dict[str, Any]) -> dict[str, Any]:
    tier = user.get("tier") or "trial"
    return {
        "email": user["email"],
        "name": user.get("name") or "",
        "provider": user.get("provider") or "",
        "tier": tier,
        "plan": billing.plan_info(tier),
        "trial": store.trial_status(user),
        "billing_enabled": billing.billing_enabled(),
    }


@router.get("/api/account")
def get_account(request: Request):
    return _payload(_current_user(request))


@router.put("/api/account")
def update_account(body: NameUpdate, request: Request):
    user = _current_user(request)
    return _payload(store.update_user_name(user["id"], body.name[:200]))


@router.post("/api/account/delete")
def delete_account(request: Request):
    user = _current_user(request)
    store.delete_user(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie("session", path="/")
    return response


@router.post("/api/billing/checkout")
def billing_checkout(body: CheckoutRequest, request: Request):
    return billing.create_checkout_session(_current_user(request), body.tier)


@router.post("/api/billing/portal")
def billing_portal(request: Request):
    return billing.create_portal_session(_current_user(request))


@router.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    # Public: Stripe calls this. Unsigned/unverifiable requests are refused (see handle_webhook).
    status, body = billing.handle_webhook(await request.body(), request.headers.get("stripe-signature", ""))
    return JSONResponse(body, status_code=status)
