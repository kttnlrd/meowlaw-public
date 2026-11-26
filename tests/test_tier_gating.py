"""MCP tier caps."""
from backend import mcp_token_manager as m


def test_mcp_tier_caps(monkeypatch):
    monkeypatch.setattr(m, "_daily_calls", {})
    monkeypatch.setattr(m.TokenManager, "record_use", lambda self, t: None)
    monkeypatch.setattr(m.TokenManager, "log_call", lambda self, t: None)
    monkeypatch.setattr(m, "DAILY_CAPS", {"solo": 3, "power": None})
    tm = m.TokenManager()
    for tier in ("trial", "anonymous"):
        assert tm.check_rate_limit("t", owner="a@x", tier=tier) == (False, "upgrade_required")
    assert all(tm.check_rate_limit("t", owner="s@x", tier="solo")[0] for _ in range(3))
    ok, reason = tm.check_rate_limit("t2", owner="s@x", tier="solo")  # cap is per owner, not per token
    assert not ok and "Daily MCP limit" in reason
    assert all(tm.check_rate_limit("p", owner="p@x", tier="power")[0] for _ in range(10))

