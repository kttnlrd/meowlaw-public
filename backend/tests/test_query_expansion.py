from backend.services.search_service import _expand_query


def test_synonym_group_and_quoted_phrase():
    assert _expand_query("main residence exemption") == (
        '("main residence" OR "principal place of residence" OR "PPOR" OR "family home") AND "exemption"'
    )
    # Quoted phrases are never expanded.
    assert _expand_query('"main residence" exemption') == '"main residence" AND "exemption"'
    # Whole words only — "scgt" must not trigger the CGT group.
    assert _expand_query("scgt") == '"scgt"'
    # A bare word already covered by a group is dropped, not AND-ed back in.
    assert _expand_query("employee share scheme ESS") == (
        '("employee share scheme" OR "ESS")'
    )
