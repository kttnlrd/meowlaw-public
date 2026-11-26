from backend.auth import _safe_next


def test_safe_next_only_allows_relative_paths():
    assert _safe_next("/app") == "/app"
    assert _safe_next("/app/search?q=x") == "/app/search?q=x"
    for bad in ["//evil.com", "/\\evil.com", "https://evil.com", "app", "", None, 5]:
        assert _safe_next(bad) is None
