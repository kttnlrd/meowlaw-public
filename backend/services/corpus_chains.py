"""Interpretive chains built from the legislation itself, for acts tax-encode does not encode.

tax-encode holds factual encodings for ITAA 1936 and ITAA 1997 only, so /api/tax/chains returned
404 for every New Zealand provision. These chains are legislation-only, which is what an
interpretive chain is: which defined terms a provision uses and where each term is defined, which
provisions it cross-refers to, and which provisions point back at it. Rulings are not part of a
provision's legislative structure and are deliberately absent: a provision is not less connected
because no ruling happens to cite it.

Two sources:
- data/<act>/definitions_index.json, built from the official legislation.govt.nz markup by
  scripts/build_nz_definitions_index.py (dfn.def-term / a.term anchors, labels from the table of
  contents);
- data/graph.db, for section-to-section citation edges already extracted for the corpus.

Nodes are provisions and defined terms, never conclusions. Nothing here evaluates tax.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from backend.services.data_loader import is_plain_id

BASE = Path(__file__).resolve().parent.parent.parent
GRAPH_DB = BASE / "data" / "graph.db"
DATA_DIR = BASE / "data"

MAX_DEFINITIONS = 60
MAX_REFERENCES = 60
MAX_CITING = 60

NZ_ACTS = {"nz-it-2007", "nz-taa-1994", "nz-gst-1985", "nz-egda-1968", "nz-scd-1971",
           "nz-kiwisaver-2006", "nz-sl-2011"}

_conn: sqlite3.Connection | None = None
_indexes: dict[str, dict] = {}


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, check_same_thread=False)
    return _conn


def covers(act: str) -> bool:
    """True when this module can build chains for the act."""
    return act in NZ_ACTS


def _index(act: str) -> dict:
    if act in _indexes:
        return _indexes[act]
    path = DATA_DIR / act / "definitions_index.json"
    data: dict = {}
    if is_plain_id(act) and path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    _indexes[act] = data
    return data


def _label_variants(section: str) -> list[str]:
    """Corpus ids are hyphenated (BD-1); the official markup writes 'BD 1'."""
    bare = section.replace("-", " ").strip()
    return [bare, section]


def _match_key(mapping: dict, section: str) -> str | None:
    """Find the mapping key for a provision across the label conventions in use."""
    lowered = {k.lower(): k for k in mapping} if mapping else {}
    for cand in _label_variants(section):
        if cand in mapping:
            return cand
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _node_meta(act: str, section: str) -> tuple[str, str]:
    row = _db().execute("select label, meta from nodes where key = ?",
                        (f"section:{act}:{section}",)).fetchone()
    if not row:
        return f"s {section}", ""
    try:
        title = (json.loads(row[1] or "{}") or {}).get("title") or ""
    except Exception:
        title = ""
    return row[0] or f"s {section}", title


def _ref_to_id(ref: str) -> str:
    """'section HD 18' / 'HD 18' -> 'HD-18', matching corpus ids."""
    m = re.match(r"^(?:sections?|ss)?\s*([A-Z]{1,3})\s*(\d+[A-Z]?)$", ref.strip(), re.I)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    return re.sub(r"\s+", "-", ref.strip())


def build(act: str, section: str, depth: int = 1) -> dict:
    """Nodes and edges reachable from one provision, legislation only."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    dropped: dict[str, int] = {}
    linked: set[str] = set()

    def node(nid: str, **kw) -> None:
        if nid not in nodes:
            nodes[nid] = {"id": nid, **kw}

    def link(src: str, dst: str, kind: str, note: str = "") -> None:
        pair = "|".join(sorted((src, dst)))
        if pair in linked:
            return
        linked.add(pair)
        edge = {"source": src, "target": dst, "kind": kind}
        if note:
            edge["note"] = note[:220]
        edges.append(edge)

    def section_node(sec: str, other_act: str | None = None) -> str:
        use_act = other_act or act
        nid = f"section:{use_act}:{sec}"
        node(nid, kind="section", label=f"s {sec}", act=use_act, section=sec,
             title=_node_meta(use_act, sec)[1])
        return nid

    label, title = _node_meta(act, section)
    root = f"section:{act}:{section}"
    node(root, kind="section", label=label, act=act, section=section, title=title)

    idx = _index(act)
    definitions = idx.get("definitions") or {}
    usages = idx.get("usages") or {}
    references = idx.get("references") or {}

    # 1. Defined terms this provision uses, and the provision that defines each of them.
    key = _match_key(usages, section)
    terms = sorted(usages.get(key, [])) if key else []
    for i, term in enumerate(terms):
        if i >= MAX_DEFINITIONS:
            dropped["definition"] = len(terms) - MAX_DEFINITIONS
            break
        defined_in = definitions.get(term) or ""
        did = f"definition:{act}:{term}"
        node(did, kind="definition", label=term, act=act, term=term, section=defined_in)
        link(root, did, "uses_definition")
        if defined_in:
            link(did, section_node(defined_in.replace(" ", "-")), "defined_in")

    # 2. Provisions this section cross-refers to, as written in the official text.
    key = _match_key(references, section)
    refs = sorted(references.get(key, [])) if key else []
    for ref in refs[:MAX_REFERENCES]:
        link(root, section_node(_ref_to_id(ref)), "refers_to")
    if len(refs) > MAX_REFERENCES:
        dropped["reference"] = len(refs) - MAX_REFERENCES

    # 3. Provisions that cross-refer back to this one.
    wanted = section.replace("-", " ").upper()
    citing = [sec for sec, vals in references.items()
              if any(re.sub(r"\s+", " ", v).upper() == wanted for v in vals)]
    for sec in sorted(citing)[:MAX_CITING]:
        link(section_node(sec.replace(" ", "-")), root, "refers_to")
    if len(citing) > MAX_CITING:
        dropped["citing"] = len(citing) - MAX_CITING

    # 4. Citation edges already extracted for the corpus, sections only.
    row = _db().execute("select id from nodes where key = ?", (root,)).fetchone()
    if row:
        for edge_type, key_, lbl, _ntype in _db().execute(
                """select e.edge_type, s.key, s.label, s.node_type
                   from graph_edges e join nodes s on s.id = e.source_id
                   where e.target_id = ? and s.node_type = 'section' limit 400""", (row[0],)):
            if not key_ or key_ == root:
                continue
            parts = key_.split(":")
            if len(parts) < 3:
                continue
            other_act, other_sec = parts[1], parts[2]
            nid = f"section:{other_act}:{other_sec}"
            node(nid, kind="section", label=lbl or f"s {other_sec}", act=other_act,
                 section=other_sec, title=_node_meta(other_act, other_sec)[1])
            link(nid, root, "cited_by" if edge_type == "cites" else f"{edge_type}_by")

    counts: dict[str, int] = {}
    for n in nodes.values():
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    for kind, n in dropped.items():
        counts[f"{kind}_omitted"] = n

    payload = {"act": act, "section": section, "nodes": list(nodes.values()), "edges": edges,
               "counts": counts, "source": "legislation-index"}
    if not idx:
        payload["detail"] = ("no definitions index for this act yet; build it with "
                             "scripts/build_nz_definitions_index.py")
    elif not edges:
        payload["detail"] = "no defined-term uses or cross-references recorded for this provision"
    return payload
