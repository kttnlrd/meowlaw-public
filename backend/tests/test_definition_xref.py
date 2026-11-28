"""get_definition's cross-reference parser (its regex was once deleted, raising NameError)."""
from backend.fastmcp_server import _definition_cross_reference


def test_cross_act_reference_resolves_to_other_act():
    r = _definition_cross_reference(
        "itaa-1997", "associate has the meaning given by section 318 of the Income Tax Assessment Act 1936.")
    assert r and r["section"] == "318" and r["act"] == "itaa-1936" and not r["same_act"]


def test_plain_definition_has_no_cross_reference():
    assert _definition_cross_reference("itaa-1997", "dividend means any distribution made by a company.") is None


def test_resolve_alias_honours_explicit_act_in_citation():
    """'s 8-1 ITAA 1997' once fell through to full-text search and returned ITAA 1936 s 63G."""
    import asyncio, json
    import pytest
    from backend.fastmcp_server import resolve_alias
    from backend.resolve_ref import resolve_ref
    if resolve_ref("s 8-1 ITAA 1997").get("type") != "legislation":
        pytest.skip("legislation corpus not available")
    r = json.loads(asyncio.run(resolve_alias("s 8-1 ITAA 1997")))
    assert (r.get("act"), r.get("section")) == ("itaa-1997", "8-1")
