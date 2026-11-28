from datetime import datetime, timedelta, timezone

from backend import billing
from backend.auth import store


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_trial_status():
    s = store.trial_status({"trial_started_at": _ago(0)})
    assert (s["days_left"], s["expired"]) == (14, False)
    s = store.trial_status({"trial_started_at": _ago(13)})
    assert (s["days_left"], s["expired"]) == (1, False)
    s = store.trial_status({"trial_started_at": _ago(20)})
    assert (s["days_left"], s["expired"]) == (0, True)


def test_billing_stub():
    assert billing.plan_info("solo")["mcp_daily"] == 5000
    assert billing.plan_info("power")["mcp_daily"] is None
    assert billing.plan_info("trial")["mcp_daily"] == 5000
    assert billing.create_checkout_session({}, "enterprise")["status"] == "error"
    assert billing.plan_info("bogus")["key"] == "trial"
    if not billing.billing_enabled():
        assert billing.create_checkout_session({}, "solo")["status"] == "not_configured"
