"""Tests for the workflow registry (procedural knowledge layer)."""
from __future__ import annotations

from backend.services.workflow_registry import WorkflowRegistry


def _reg() -> WorkflowRegistry:
    reg = WorkflowRegistry()
    reg.load(known_nodes=set())
    return reg


def test_loads_cgt():
    reg = _reg()
    assert "cgt-analysis" in reg.workflows
    assert reg.errors == []


def test_loads_all_workflows():
    reg = _reg()
    expected = {
        "cgt-analysis",
        "trust-distributions",
        "gst",
        "psi",
        "deceased-estates",
        "div-7a",
        "tax-losses",
        "ess",
    }
    assert set(reg.workflows.keys()) == expected
    assert reg.errors == []


def test_entry_nodes():
    reg = _reg()
    wf = reg.get("cgt-analysis")
    assert wf is not None
    assert set(wf.entry) == {"asset", "event"}
    slices = reg.entry_slices("cgt-analysis")
    assert {s["node"] for s in slices} == {"asset", "event"}


def test_node_slice_shape():
    reg = _reg()
    sl = reg.node_slice("cgt-analysis", "event")
    assert sl is not None
    assert sl["node"] == "event"
    assert sl["question"]
    assert sl["fetch"]  # has section anchors
    assert sl["traverse"]  # has edge types
    assert len(sl["branches"]) > 5  # event has many branches
    assert sl["terminal"] is False


def test_terminal_nodes():
    reg = _reg()
    for nid in ("stop_no_cgt", "stop_rollover", "stop_loss", "stop_done"):
        sl = reg.node_slice("cgt-analysis", nid)
        assert sl is not None, nid
        assert sl["terminal"] is True, nid


def test_all_branches_resolve():
    reg = _reg()
    wf = reg.get("cgt-analysis")
    assert wf is not None
    for nid, node in wf.nodes.items():
        for b in node.branches:
            assert b["then"] in wf.nodes, f"{nid} -> {b['then']}"


def test_reachability():
    reg = _reg()
    wf = reg.get("cgt-analysis")
    assert wf is not None
    # every non-terminal node must lead somewhere
    for nid, node in wf.nodes.items():
        if not node.terminal:
            assert node.branches, f"{nid} has no branches and is not terminal"


def test_detect():
    reg = _reg()
    assert reg.detect("What is the CGT on selling my main residence?") == [
        "cgt-analysis"
    ]
    assert reg.detect("capital loss on shares") == ["cgt-analysis"]
    assert reg.detect("FBT on car benefits") == []
    assert reg.detect("gst on imported services") == ["gst"]
    assert reg.detect("trustee distributing to a minor beneficiary") == [
        "trust-distributions"
    ]
    assert reg.detect("consultant through a company, PSI or PSB") == ["psi"]
    assert reg.detect("loan from my private company to buy a car") == [
        "div-7a"
    ]
    assert reg.detect("can the company carry forward last year's tax loss") == [
        "tax-losses"
    ]
    assert reg.detect("employee shares under an employee share scheme") == ["ess"]


def test_multi_workflow_detection():
    """Death + property should attach both CGT and deceased-estates."""
    reg = _reg()
    hits = reg.detect("Dad died and left the house to us — CGT on the estate?")
    assert "cgt-analysis" in hits
    assert "deceased-estates" in hits


def test_unknown_workflow_node():
    reg = _reg()
    assert reg.node_slice("cgt-analysis", "nope") is None
    assert reg.node_slice("nope", "asset") is None
    assert reg.entry_slices("nope") is None


def test_anchor_check_reports_missing():
    """With an empty graph, check_anchors reports all fetch anchors."""
    reg = _reg()
    wf = reg.get("cgt-analysis")
    assert wf is not None
    missing = wf.check_anchors(set())
    assert len(missing) == sum(len(n.fetch) for n in wf.nodes.values())
    assert all("not in graph nodes" in m for m in missing)
    # and with a full known-node set, nothing is missing
    all_anchors = {a for n in wf.nodes.values() for a in n.fetch}
    assert wf.check_anchors(all_anchors) == []
