"""Workflow registry — procedural knowledge layer.

Loads workflow YAML files once at startup, validates them against the
graph node table, and serves token-lean node slices. The LLM never sees
the full map — only the current node's question, fetch anchors, and
branches ("where to go next").

Spec: docs/specs/procedural-knowledge.md
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from backend.config import BASE

logger = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "data" / "workflows"

# Topic detection: regex signal per workflow id. First match wins at
# routing time; ambiguous queries surface entry slices instead.
TOPIC_SIGNALS: dict[str, list[str]] = {
    "cgt-analysis": [
        r"\bcgt\b",
        r"capital gain",
        r"capital loss",
        r"cost base",
        r"capital proceeds",
        r"main residence",
        r"cgt event",
        r"disposal of (a |an |the )?asset",
        r"small business (concession|relief)",
        r"rollover",
        r"indexation",
        r"pre-21 sep",
        r"discount capital gain",
        r"50% discount",
        r"12.?month holding",
    ],
    "trust-distributions": [
        r"trust (estate|distribution|income)",
        r"presently entitled",
        r"beneficiary",
        r"trustee assessable",
        r"\bs99a\b|section 99a",
        r"section 100a|reimbursement agreement",
        r"streaming",
        r"div 6aa|minor.*(trust|unearned)",
        r"discretionary trust",
    ],
    "gst": [
        r"\bgst\b",
        r"taxable supply",
        r"input.?taxed",
        r"gst.?free",
        r"creditable acquisition",
        r"input tax credit",
        r"margin scheme",
        r"activity statement",
        r"reverse charge",
        r"registered for gst",
    ],
    "psi": [
        r"personal services income",
        r"\bpsi\b",
        r"personal services business",
        r"results test",
        r"unrelated clients test",
        r"employment test",
        r"business premises test",
        r"alienation of personal services",
        r"80% rule",
    ],
    "deceased-estates": [
        r"deceased estate",
        r"death of (a |an |the )?(taxpayer|owner)",
        r"\bdied\b",
        r"date.?of.?death",
        r"testamentary trust",
        r"inherited (asset|property|dwelling|house)",
        r"inheritance",
        r"estate (income|distribution|administration)",
        r"beneficiary.*(estate|deceased)",
        r"will.*(asset|property)",
        r"left (the |a |an )?(house|property|asset) to",
    ],
    "div-7a": [
        r"div ?7a",
        r"division 7a",
        r"unpaid present entitlement",
        r"\bupe\b",
        r"loan (to|from|by) (a |the )?(shareholder|associate|company)",
        r"loan.*(shareholder|associate)",
        r"deemed dividend",
        r"private company (loan|payment)",
        r"(loan|payment) (from|by) (my |the )?private company",
        r"benchmark interest",
        r"109d|109j|109xa",
    ],
    "tax-losses": [
        r"tax loss",
        r"carry.?forward.*loss",
        r"continuity of ownership",
        r"same business test",
        r"recoupment",
        r"deduct.*prior.?year loss",
        r"\bcot\b.*(company|loss)",
        r"\bsbt\b",
        r"loss year",
    ],
    "ess": [
        r"employee share scheme",
        r"\bess\b",
        r"share scheme",
        r"deferred taxing point",
        r"taxed.?upfront",
        r"discount.*(share|option|right)",
        r"83a[- ]?\d+",
    ],
}


class WorkflowError(Exception):
    """Workflow definition or validation error."""


class WorkflowNode:
    __slots__ = ("id", "question", "fetch", "traverse", "branches", "terminal")

    def __init__(self, node_id: str, data: dict):
        self.id = node_id
        self.question = data.get("question", "")
        self.fetch = list(data.get("fetch", []))
        self.traverse = list(data.get("traverse", []))
        self.terminal = bool(data.get("terminal", False))
        branches = data.get("branches", []) or []
        self.branches = [
            {"if": b.get("if", ""), "then": b.get("then", "")} for b in branches
        ]

    def slice(self) -> dict:
        """Token-lean representation of this node (~200 tokens)."""
        return {
            "node": self.id,
            "question": self.question,
            "fetch": self.fetch,
            "traverse": self.traverse,
            "terminal": self.terminal,
            "branches": self.branches,
        }


class Workflow:
    def __init__(self, data: dict, path: Path):
        self.id = data.get("id")
        self.name = data.get("name", self.id)
        self.area = data.get("area", "")
        self.version = str(data.get("version", "0"))
        self.path = path
        entry = data.get("entry", [])
        self.entry = [entry] if isinstance(entry, str) else list(entry)
        raw_nodes = data.get("nodes", {}) or {}
        self.nodes: dict[str, WorkflowNode] = {
            nid: WorkflowNode(nid, ndata) for nid, ndata in raw_nodes.items()
        }
        self.rules = list(data.get("rules", []) or [])

    def node(self, node_id: str) -> WorkflowNode | None:
        return self.nodes.get(node_id)

    def entry_slices(self) -> list[dict]:
        return [self.nodes[n].slice() for n in self.entry if n in self.nodes]

    def validate(self, known_nodes: set[str] | None = None) -> list[str]:
        """Structural validation — gates loading.

        Checks entry nodes, branch targets, reachability. Fetch-anchor
        resolution against the graph is a separate, non-blocking check
        (see check_anchors) — the graph ETL may not exist yet.
        """
        errors: list[str] = []
        if not self.id:
            errors.append(f"{self.path.name}: missing id")
        if not self.entry:
            errors.append(f"{self.path.name}: no entry node(s)")
        for e in self.entry:
            if e not in self.nodes:
                errors.append(f"{self.path.name}: entry '{e}' not in nodes")
        for nid, node in self.nodes.items():
            for b in node.branches:
                if b["then"] not in self.nodes:
                    errors.append(
                        f"{self.path.name}: node '{nid}' branch -> '{b['then']}' "
                        f"not in nodes"
                    )
        # Reachability from entry
        reachable: set[str] = set()
        frontier = list(self.entry)
        while frontier:
            cur = frontier.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            node = self.nodes.get(cur)
            if node:
                for b in node.branches:
                    if b["then"] not in reachable:
                        frontier.append(b["then"])
        for nid in self.nodes:
            if nid not in reachable:
                errors.append(
                    f"{self.path.name}: node '{nid}' unreachable from entry"
                )
        return errors

    def check_anchors(self, known_nodes: set[str]) -> list[str]:
        """Graph-consistency check — run by the ETL after nodes are built.

        Non-blocking: reports unresolved fetch anchors as warnings.
        """
        missing: list[str] = []
        for nid, node in self.nodes.items():
            for a in node.fetch:
                if a not in known_nodes:
                    missing.append(
                        f"{self.path.name}: node '{nid}' fetch anchor '{a}' "
                        f"not in graph nodes"
                    )
        return missing


class WorkflowRegistry:
    """In-memory registry, loaded once at startup. Never refetched."""

    def __init__(self, workflows_dir: Path | None = None):
        self.workflows_dir = workflows_dir or WORKFLOWS_DIR
        self.workflows: dict[str, Workflow] = {}
        self.errors: list[str] = []
        self.anchor_warnings: list[str] = []

    def load(self, known_nodes: set[str] | None = None) -> None:
        """Load and validate all workflow files."""
        self.workflows.clear()
        self.errors.clear()
        self.anchor_warnings.clear()
        known_nodes = known_nodes or set()
        if not self.workflows_dir.exists():
            self.errors.append(f"workflows dir missing: {self.workflows_dir}")
            return
        for path in sorted(self.workflows_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
            except yaml.YAMLError as e:
                self.errors.append(f"{path.name}: YAML parse error: {e}")
                continue
            if not isinstance(data, dict):
                self.errors.append(f"{path.name}: not a mapping")
                continue
            wf = Workflow(data, path)
            if not wf.id:
                self.errors.append(f"{path.name}: missing id")
                continue
            wf_errors = wf.validate(known_nodes)
            if wf_errors:
                self.errors.extend(wf_errors)
                logger.error("workflow %s failed validation", wf.id)
                continue
            self.workflows[wf.id] = wf
            if known_nodes:
                self.anchor_warnings.extend(wf.check_anchors(known_nodes))
            logger.info(
                "workflow %s v%s loaded (%d nodes)",
                wf.id,
                wf.version,
                len(wf.nodes),
            )
        if self.errors:
            logger.warning("workflow registry: %d validation error(s)", len(self.errors))

    # ── runtime ──────────────────────────────────────────────────────

    def detect(self, text: str) -> list[str]:
        """Topic detection: return workflow ids whose signals match the text."""
        text_l = text.lower()
        hits = []
        for wf_id, patterns in TOPIC_SIGNALS.items():
            if wf_id not in self.workflows:
                continue
            for pat in patterns:
                if re.search(pat, text_l):
                    hits.append(wf_id)
                    break
        return hits

    def get(self, wf_id: str) -> Workflow | None:
        return self.workflows.get(wf_id)

    def node_slice(self, wf_id: str, node_id: str) -> dict | None:
        """Return a node slice, or None if workflow/node unknown."""
        wf = self.workflows.get(wf_id)
        if not wf:
            return None
        node = wf.node(node_id)
        if not node:
            return None
        return node.slice()

    def entry_slices(self, wf_id: str) -> list[dict] | None:
        wf = self.workflows.get(wf_id)
        return wf.entry_slices() if wf else None


_registry: WorkflowRegistry | None = None


def get_registry() -> WorkflowRegistry:
    """Module-level singleton; load once."""
    global _registry
    if _registry is None:
        _registry = WorkflowRegistry()
        # known_nodes filled by graph ETL; until then, validate structure only
        _registry.load(known_nodes=set())
    return _registry
