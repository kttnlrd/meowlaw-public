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


class PinnedSources(BaseModel):
    sources: list[str]


def _current_user(request: Request) -> dict[str, Any]:
    """Session claims -> fresh DB row (session JWT may be stale or deleted)."""
    session = require_user(request)
    user = store.get_user_by_id(session.get("id")) if session.get("id") is not None else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _payload(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": user["email"],
        "name": user.get("name") or "",
        "provider": user.get("provider") or "",
    }


@router.get("/api/account")
def get_account(request: Request):
    return _payload(_current_user(request))


@router.put("/api/account")
def update_account(body: NameUpdate, request: Request):
    user = _current_user(request)
    return _payload(store.update_user_name(user["id"], body.name[:200]))


@router.get("/api/account/pinned-sources")
def get_pinned_sources(request: Request):
    """The sidebar sources this user pinned, in their order."""
    from backend.db import cursor
    email = str(require_user(request).get("email") or "").lower()
    if not email:
        return {"sources": []}
    with cursor() as cur:
        cur.execute("SELECT source_id FROM user_pinned_sources WHERE email = %s ORDER BY position, source_id",
                    (email,))
        return {"sources": [r["source_id"] for r in cur.fetchall()]}


@router.put("/api/account/pinned-sources")
def put_pinned_sources(body: PinnedSources, request: Request):
    """Replace this user's pinned sources. Ids are only labels for the sidebar: a pin never
    grants anything, and a pinned source the user cannot open is simply not listed."""
    import re
    from backend.db import cursor
    email = str(require_user(request).get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ids: list[str] = []
    for s in body.sources:
        s = str(s).strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", s) and s not in ids:
            ids.append(s)
    ids = ids[:50]
    with cursor() as cur:
        cur.execute("DELETE FROM user_pinned_sources WHERE email = %s", (email,))
        for i, s in enumerate(ids):
            cur.execute("INSERT INTO user_pinned_sources (email, source_id, position) VALUES (%s, %s, %s)",
                        (email, s, i))
    return {"sources": ids}


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
