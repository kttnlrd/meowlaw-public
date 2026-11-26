"""Workflow audit — completeness checks beyond structural validation.

Three passes:
  1. STRUCTURAL   — branch targets exist, entry valid, reachability
                   (already in registry.validate, re-run here for output)
  2. DEAD FETCH   — every fetch anchor is actually used in the node's
                   question/branches; unused anchors = stale or wrong
  3. COVERAGE     — every section in the workflow's act+division exists
                   in the corpus; sections NOT referenced by any workflow
                   node are surfaced for human review ("missed branch or
                   genuinely irrelevant?")

Usage:
  python -m backend.scripts.workflow_audit div-7a
  python -m backend.scripts.workflow_audit --all
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.workflow_registry import WorkflowRegistry  # noqa: E402

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# act -> list of corpus act dirs (same key as workflow fetch anchors)
ACT_DIRS = {
    "itaa-1936": "itaa-1936",
    "itaa-1997": "itaa-1997",
    "gst-1999": "gst-1999",
}


def corpus_sections() -> dict[str, set[str]]:
    """act -> {section key} from section frontmatter (division aware)."""
    out: dict[str, set[str]] = {}
    for act, d in ACT_DIRS.items():
        sections: set[str] = set()
        meta: dict[str, tuple[str, str]] = {}  # section -> (division, title)
        base = DATA_DIR / d / "sections"
        if not base.exists():
            continue
        for f in base.rglob("*.md"):
            fm = _frontmatter(f)
            if not fm:
                continue
            sec = fm.get("section") or f.stem
            sections.add(f"{act}:{sec}")
            meta[f"{act}:{sec}"] = (
                str(fm.get("division", "")),
                str(fm.get("section_title", "")),
            )
            _SUBDIV[f"{act}:{sec}"] = str(fm.get("subdivision", ""))
        out[act] = sections
        # stash meta for coverage output via module attr
        _META.update(meta)
    return out


_META: dict[str, tuple[str, str]] = {}
_SUBDIV: dict[str, str] = {}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(errors="ignore")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def audit_workflow(reg: WorkflowRegistry, wf_id: str, sections: dict[str, set[str]]):
    wf = reg.get(wf_id)
    if wf is None:
        print(f"  {wf_id}: NOT LOADED (validation errors — see registry.errors)")
        return
    print(f"\n{'='*60}")
    print(f"WORKFLOW: {wf.id} v{wf.version} — {wf.name}")
    print(f"{'='*60}")

    # ── 1. STRUCTURAL ────────────────────────────────────────────────
    struct = wf.validate(set())
    print(f"\n[1] STRUCTURAL: {'OK' if not struct else 'FAIL'}")
    for e in struct:
        print(f"    {e}")

    # ── 2. DEAD FETCH ────────────────────────────────────────────────
    print(f"\n[2] DEAD FETCH (anchors never mentioned in question/branches):")
    dead = []
    for nid, node in wf.nodes.items():
        haystack = (node.question + " " + " ".join(b["if"] for b in node.branches)).lower()
        for a in node.fetch:
            # match the tail of the key, e.g. '109n' for 'itaa-1936:109n'
            tail = a.split(":")[-1].lower()
            if tail not in haystack:
                dead.append((nid, a))
    if not dead:
        print("    none")
    else:
        for nid, a in dead:
            print(f"    {nid}: fetch '{a}' never mentioned in question/branches")

    # ── 3. COVERAGE vs corpus ────────────────────────────────────────
    print(f"\n[3] COVERAGE — sections in the workflow's act+division not referenced:")
    referenced: set[str] = set()
    for node in wf.nodes.values():
        referenced.update(node.fetch)
    # Scope: the division of the workflow's first anchor (e.g. itaa-1936:109d
    # -> division 7A). Fall back to the whole act if no division is found.
    # Note: corpus keys may differ in case from workflow anchors
    # (itaa-1936:109D vs itaa-1936:109d) — compare case-insensitively.
    referenced: set[str] = {a.lower() for a in referenced}
    # lowercase -> original-case key, for metadata lookups
    meta_lower: dict[str, tuple[str, str]] = {
        k.lower(): v for k, v in _META.items()
    }
    # Coverage per (act, division) pair touched by the workflow's anchors —
    # a workflow may span multiple acts/divisions (e.g. deceased-estates
    # uses itaa-1997 div 128 + itaa-1936 div 6). Unreferenced sections are
    # grouped by subdivision so a human can see "you cited 110-25 but not
    # 110-20 — deliberate?" vs "div 115-B: irrelevant to this workflow".
    scopes: dict[tuple[str, str], list[str]] = {}
    for a in sorted(referenced):
        act = a.split(":")[0]
        div, _ = meta_lower.get(a, ("", ""))
        if div:
            scopes.setdefault((act, div), []).append(a)
    missing: list[tuple[str, str, str, str]] = []  # key, act, subdivision, title
    for (tact, tdiv), _anchors in scopes.items():
        for key in sections.get(tact, set()):
            if key.lower() not in referenced:
                div, title = _META.get(key, ("", ""))
                if div == tdiv:
                    subdiv = _SUBDIV.get(key, "")
                    missing.append((key, tact, subdiv, title))
    missing.sort(key=lambda t: (t[1], t[2], t[0]))
    if not missing:
        scopes_str = ", ".join(f"{a} div {d}" for a, d in sorted(scopes))
        print(f"    every corpus section in {scopes_str} is referenced")
    else:
        last_sub = None
        for key, tact, subdiv, title in missing:
            sub = subdiv or "(no subdivision)"
            if sub != last_sub:
                print(f"    -- {tact} {sub}")
                last_sub = sub
            print(f"       {key:26s} {title}")
        print(f"    ({len(missing)} unreferenced across {len(scopes)} division(s))")

    # ── 4. TERMINAL REACHABILITY ─────────────────────────────────────
    print(f"\n[4] TERMINALS: ")
    for nid, node in wf.nodes.items():
        if node.terminal:
            print(f"    {nid} (terminal)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", nargs="?", help="workflow id, or --all")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    sections = corpus_sections()
    reg = WorkflowRegistry()
    reg.load(known_nodes=set())

    if args.all:
        for wf_id in sorted(reg.workflows):
            audit_workflow(reg, wf_id, sections)
    elif args.workflow:
        audit_workflow(reg, args.workflow, sections)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
