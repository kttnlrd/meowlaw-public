"""Red-team regressions (2026-11-24): ruling resolution shapes + the NZ-ruling MCP gate."""
import pytest

from backend import fastmcp_server as fs
from backend.resolve_ref import resolve_ref


@pytest.mark.parametrize("ref,cit", [
    ("ruling:tr-2024-1", "TR 2024/1"), ("TR 2024/1", "TR 2024/1"), ("TR_2024_1", "TR 2024/1"),
    ("PS LA 2011/12", "PS LA 2011/12"), ("BR Pub 24/01", "BR Pub 24/01"),
    ("BR Prd 04/01", "BR Prd 04/01"), ("ruling:brpub-2024-1", "BR Pub 24/01"),
])
def test_ruling_shapes_resolve(ref, cit):
    r = resolve_ref(ref)
    assert r.get("citation") == cit and r.get("text"), r


def test_ruling_display_form_carries_section_refs():
    assert resolve_ref("TR 2024/1")["referenced_sections"]


def test_nz_ruling_gate_engages(monkeypatch):
    nz, au = resolve_ref("BR Pub 24/01"), resolve_ref("TR 2024/1")
    assert nz["jurisdiction"] == "NZ" and au["jurisdiction"] == "AU"
    monkeypatch.setattr(fs, "_mcp_avail", lambda: {"ruling"})  # caller without nz-ruling
    fs._deny_resolved(au)
    with pytest.raises(Exception, match="nz-ruling is not available"):
        fs._deny_resolved(nz)


@pytest.mark.parametrize("ref,slug", [
    ("leg:master-tax-guide:4-200", "entities-div-7a"),
    ("leg:nz-master-tax-guide:4-200", "filing-requirements-for-disputes-review"),
])
def test_commentary_paragraph_ids_resolve(ref, slug):
    assert resolve_ref(ref).get("section") == slug


def test_leg_gate_runs_before_existence(monkeypatch):
    monkeypatch.setattr(fs, "_mcp_avail", lambda: {"legislation"})
    for ref in ("leg:master-tax-guide:4-200", "leg:master-tax-guide:99-999"):
        with pytest.raises(Exception, match="commentary is not available"):
            fs._deny_ref(ref)
    fs._deny_ref("leg:itaa-1997:8-1")


@pytest.mark.parametrize("ref,section", [
    ("leg:nz-it-2007:CB1", "CB-1"), ("leg:nz-it-2007:cb1", "CB-1"),
    ("leg:nz-it-2007:cb-1", "CB-1"), ("s CB 1 NZ Income Tax Act", "CB-1"),
    ("CB-1", "CB-1"), ("CB1", "CB-1"), ("CB 1", "CB-1"), ("CB_1", "CB-1"),
    ("cb 1", "CB-1"), ("CB-15B", "CB-15B"),
])
def test_nz_section_shapes(ref, section):
    assert resolve_ref(ref).get("section") == section


def test_residency_pin():
    rows = [{"act": "itaa-1997", "section": "295-95"}]
    assert fs._pin_concepts("residency central management and control", None, rows, 5)[0]["section"] == "6"
    assert fs._pin_concepts("superannuation fund residency", None, rows, 5) == rows
    assert fs._pin_concepts("residency", "itaa-1997", rows, 5) == rows
