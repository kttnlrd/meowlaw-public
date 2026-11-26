"""Stripe-ready billing stub.

Dependency-free: no `stripe` package. Every call returns `not_configured`
until STRIPE_SECRET_KEY is set; after that the TODOs mark where the Stripe
API calls go.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICES = {
    "solo": os.environ.get("STRIPE_PRICE_SOLO", ""),
    "power": os.environ.get("STRIPE_PRICE_POWER", ""),
}

# mcp_daily: None = no daily limit. 5000 = flat daily cap (trial & solo during beta).
TIERS: dict[str, dict[str, Any]] = {
    "trial": {"name": "Kitten", "price": "$0", "mcp_daily": 5000, "note": "full corpus, 5,000 calls/day"},
    "solo": {"name": "House cat", "price": "$25", "mcp_daily": 5000},
    "power": {"name": "Power", "price": "$100", "mcp_daily": None},
}

_NOT_CONFIGURED = {"status": "not_configured", "message": "Billing isn't linked yet."}


def billing_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def plan_info(tier: str) -> dict[str, Any]:
    key = tier if tier in TIERS else "trial"
    return {"key": key, **TIERS[key]}


def create_checkout_session(user: dict[str, Any], tier: str) -> dict[str, Any]:
    if tier not in STRIPE_PRICES:
        return {"status": "error", "message": f"Unknown tier: {tier}"}
    if not billing_enabled():
        return dict(_NOT_CONFIGURED)
    # TODO: call Stripe Checkout here —
    #   POST https://api.stripe.com/v1/checkout/sessions
    #   mode=subscription, line_items[0][price]=STRIPE_PRICES[tier],
    #   customer_email=user["email"], client_reference_id=user["id"],
    #   success_url/cancel_url -> {APP_BASE_URL}/app/
    # and return {"status": "ok", "url": session.url}.
    return {"status": "stub", "tier": tier, "price_id": STRIPE_PRICES[tier], "url": None}


def create_portal_session(user: dict[str, Any]) -> dict[str, Any]:
    if not billing_enabled():
        return dict(_NOT_CONFIGURED)
    # TODO: call Stripe Billing Portal here —
    #   POST https://api.stripe.com/v1/billing_portal/sessions
    #   customer=<stripe customer id for user>, return_url={APP_BASE_URL}/app/
    # and return {"status": "ok", "url": session.url}.
    return {"status": "stub", "url": None}


WEBHOOK_TOLERANCE_S = 300
_REFUSED = {"error": "invalid_signature", "message": "Missing or invalid Stripe-Signature."}


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, now: float | None = None) -> bool:
    """Stripe's scheme: header "t=<ts>,v1=<hex>[,v1=...]", v1 = HMAC-SHA256(secret, f"{t}.{payload}")."""
    if not secret or not sig_header:
        return False
    parts = [p.split("=", 1) for p in sig_header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k.strip() == "t"), "")
    sigs = [v.strip() for k, v in parts if k.strip() == "v1"]
    if not ts.isdigit() or not sigs:
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > WEBHOOK_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def handle_webhook(payload: bytes, sig_header: str) -> tuple[int, dict[str, Any]]:
    """POST /api/billing/webhook (public; Stripe calls it).

    Request: raw Stripe event JSON + `Stripe-Signature` header.
    Responses: 200 {"received": true} for a verified event;
               400 {"error": "invalid_signature", "message": ...} otherwise.

    Fails closed: with no STRIPE_WEBHOOK_SECRET nothing can verify, so every
    request gets the same 400 a bad signature gets (no hint whether Stripe is set up).

    SECURITY: a user's `tier` must only ever come from a *verified* Stripe event,
    looked up via the event's customer/subscription — never from the request body
    or anything a caller can send unsigned.
    """
    if not verify_stripe_signature(payload, sig_header, os.environ.get("STRIPE_WEBHOOK_SECRET", "")):
        return 400, dict(_REFUSED)
    # TODO: on checkout.session.completed / customer.subscription.* update users.tier
    # (see SECURITY note above).
    return 200, {"received": True}
