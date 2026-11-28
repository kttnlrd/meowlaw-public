"""Graph lookups match lower(key), so content gating must ignore case too."""
from backend.services.content_access import act_key, node_allowed, node_key

STANDARD = {"legislation", "case", "ruling"}  # no admin-only types


def test_upper_cased_gated_prefixes_stay_gated():
    for key in ("INSOLVENCY:keays:ch-12", "Insolvency:keays:ch-12", "NZ_MTG:x", "Commentary:mtg:1"):
        assert node_key(key) in {"insolvency", "nz-mtg", "commentary"}
        assert not node_allowed(STANDARD, key)


def test_act_ids_gate_case_insensitively():
    assert act_key("KEAYS") == act_key("keays")
    assert act_key("Nz-Master-Tax-Guide") == act_key("nz-master-tax-guide")
    assert act_key("ITAA-1997") == "legislation"
