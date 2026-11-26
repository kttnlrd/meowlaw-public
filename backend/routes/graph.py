"""Graph data endpoint — returns nodes + edges for force-directed visualization.

Supports three source types:
  - section:  /api/graph/data?type=section&act=itaa-1997&section=8-1
  - ruling:   /api/graph/data?type=ruling&citation=TR%202025/1
  - case:     /api/graph/data?type=case&citation=[2015]%20HCA%2048

Returns { nodes: [{id, label, group, url}], edges: [{source, target, label}] }
where group controls colour: section, ruling, case, definition, commentary.
"""
from __future__ import annotations

import logging
import sqlite3
import json
import re as _re

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend.config import BASE
from backend.services.graph_alias import lookup as alias_lookup
from backend.services.content_access import (
    available_keys, denied_node_types, node_allowed, user_from,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _section_url(act: str, section: str) -> str:
    return f"/sections/{act}/{section}"


def _ruling_url(citation: str) -> str:
    from urllib.parse import quote
    return f"/rulings/{quote(citation)}"


def _case_url(citation: str) -> str:
    from urllib.parse import quote
    return f"/tax-cases/{quote(citation)}"


# ── resolve functions (graph.db) ─────────────────────────────────────────────

GRAPH_DB = BASE / "data" / "graph.db"

_EDGE_LABELS = {
    "applies": "applies",
    "interpreted_by": "interpreted by",
    "cites": "cites",
    "considered_in": "considered in",
    "explained_in": "explained in",
    "defines": "defines",
    "consistent_with": "consistent with",
}

# Node types we never expand through — private rulings are anonymous leaves.
_LEAF_TYPES = {"private_ruling"}

_MAX_NODES = 300
_MAX_PRIVATE = 60
_MAX_EDGES = 2000


def _norm(s: str) -> str:
    return _re.sub(r"[\s_]+", "", s or "").upper()


class _GraphIndex:
    """Lazy normalized key -> (node_type, key) index over graph.db nodes."""

    def __init__(self):
        self._keys: dict[str, tuple[str, str]] | None = None

    def _load(self):
        conn = sqlite3.connect(str(GRAPH_DB))
        try:
            self._keys = {
                _norm(k): (nt, k)
                for nt, k in conn.execute("SELECT node_type, key FROM nodes").fetchall()
            }
        finally:
            conn.close()

    def lookup(self, key: str) -> tuple[str, str] | None:
        if self._keys is None:
            self._load()
        return self._keys.get(_norm(key))


_GRAPH_INDEX = _GraphIndex()


# ── Citation metadata (case name / ruling year) ─────────────────────────────
# The graph stores only citations in its node labels; names and years live in
# the case/ruling JSON data. Loaded lazily once and cached.

_CITATION_META: dict[str, dict] | None = None
_RULING_YEAR_RE = _re.compile(r"(\d{4})")
# Product Rulings (PR series) cite a provision for a specific product
# arrangement; they do not interpret a section generally.
_PRODUCT_RULING_RE = _re.compile(r"^PR[\s_]*\d")


def _clean_case_name(title: str, citation: str) -> str:
    """Strip the embedded citation + trailing date from a case title,
    leaving the party names (e.g. 'Hui and Commissioner of Taxation')."""
    name = (title or "").strip()
    if citation and citation in name:
        name = name.replace(citation, " ")
    name = _re.sub(r"\(\d{1,2}\s+\w+\s+\d{4}\)\s*$", " ", name)
    name = _re.sub(r"\s+", " ", name).strip(" -—")
    return name


def _citation_meta() -> dict[str, dict]:
    global _CITATION_META
    if _CITATION_META is not None:
        return _CITATION_META
    meta: dict[str, dict] = {}
    for fname in ("aata_tax_cases.json", "fca_tax_cases.json", "fcafc_tax_cases.json"):
        p = BASE / "data" / fname
        if not p.exists():
            continue
        try:
            for c in json.loads(p.read_text(encoding="utf-8")):
                cit = c.get("citation")
                if cit:
                    meta[cit] = {"name": _clean_case_name(c.get("title", ""), cit), "year": c.get("year", 0)}
        except Exception:
            continue
    mp = BASE / "data" / "ruling_manifest.json"
    if mp.exists():
        try:
            for r in json.loads(mp.read_text(encoding="utf-8")):
                cit = r.get("citation")
                if cit:
                    meta[cit] = {"name": r.get("title", ""), "year": r.get("year", 0)}
        except Exception:
            pass
    _CITATION_META = meta
    return meta


def _ruling_year(citation: str) -> int:
    m = _RULING_YEAR_RE.search(citation or "")
    return int(m.group(1)) if m else 0


def _node_payload(nt: str, key: str, label: str) -> dict:
    """Map a graph.db node to the API node shape."""
    if nt == "section":
        # key: section:{act}:{sec}
        _, act, sec = key.split(":", 2)
        return {"id": key, "label": label, "short_label": sec,
                "group": "section", "url": _section_url(act, sec)}
    if nt == "public_ruling":
        cit = key.split(":", 1)[1]
        return {"id": key, "label": cit, "short_label": cit,
                "group": "ruling", "url": _ruling_url(cit)}
    if nt == "private_ruling":
        auth = key.rsplit("/", 1)[-1]
        return {"id": key, "label": f"EV/{auth}", "short_label": f"EV\u2026{auth[-6:]}",
                "group": "private_ruling", "url": ""}
    if nt == "case":
        cit = key.split(":", 1)[1]
        return {"id": key, "label": cit, "short_label": cit,
                "group": "case", "url": _case_url(cit)}
    if nt == "definition":
        term = label or key.rsplit(":", 1)[-1]
        return {"id": key, "label": term, "short_label": term,
                "group": "definition", "url": ""}
    if nt == "regulatory_guide":
        # key: regulatory_guide:{n} or regulatory_guide:{n}:{para}
        parts = key.split(":")
        n = parts[1] if len(parts) > 1 else ""
        para = parts[2] if len(parts) > 2 else None
        short = para or f"RG {n}"
        return {"id": key, "label": label, "short_label": short,
                "group": "regulatory_guide", "url": f"/regulatory-guides/{n}"}
    if nt == "afsa_guide":
        # key: afsa_guide:{slug} or afsa_guide:{slug}:{para}
        parts = key.split(":")
        slug = parts[1] if len(parts) > 1 else ""
        para = parts[2] if len(parts) > 2 else None
        short = para or "AFSA"
        return {"id": key, "label": label, "short_label": short,
                "group": "afsa_guide", "url": f"/afsa-guides/{slug}"}
    # commentary
    return {"id": key, "label": label, "short_label": key.rsplit(":", 1)[-1],
            "group": "commentary", "url": ""}


def _resolve_from_graph(start_key: str, depth: int, denied_types: set[str] = set()) -> dict:
    """BFS ego-graph from a graph.db node key. Private rulings are leaves.

    denied_types: node_type values the caller cannot see; pruned during the walk so a
    depth>=2 expansion cannot route through a hidden node to reach visible ones.
    """
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row
    try:
        start = conn.execute(
            "SELECT id, node_type, key, label FROM nodes WHERE key = ?",
            (start_key,),
        ).fetchone()
        if start is None:
            return {"nodes": [], "edges": []}

        seen: dict[int, dict] = {start["id"]: {
            "node_type": start["node_type"], "key": start["key"], "label": start["label"]}}
        edge_set: set[tuple] = set()
        edge_list: list[dict] = []
        frontier = [start["id"]]
        private_total = 0

        for _level in range(depth):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"""SELECT e.edge_type, e.weight,
                           sn.id AS sid, sn.key AS skey, sn.node_type AS stype, sn.label AS slabel,
                           tn.id AS tid, tn.key AS tkey, tn.node_type AS ttype, tn.label AS tlabel
                    FROM graph_edges e
                    JOIN nodes sn ON sn.id = e.source_id
                    JOIN nodes tn ON tn.id = e.target_id
                    WHERE e.source_id IN ({ph}) OR e.target_id IN ({ph})
                    LIMIT 4000""",
                frontier + frontier,
            ).fetchall()

            next_frontier: list[int] = []
            for r in rows:
                ekey = (r["skey"], r["tkey"], r["edge_type"])
                if ekey not in edge_set:
                    edge_set.add(ekey)
                    edge_list.append({
                        "source": r["skey"],
                        "target": r["tkey"],
                        "label": _EDGE_LABELS.get(r["edge_type"], r["edge_type"]),
                        "weight": r["weight"] or 1.0,
                        "type": r["edge_type"],
                    })
                for nid, nt, nkey, nlabel in (
                    (r["sid"], r["stype"], r["skey"], r["slabel"]),
                    (r["tid"], r["ttype"], r["tkey"], r["tlabel"]),
                ):
                    if nt in denied_types or nid in seen or len(seen) >= _MAX_NODES:
                        continue
                    if nt == "private_ruling":
                        private_total += 1
                        if private_total > _MAX_PRIVATE:
                            continue
                    seen[nid] = {"node_type": nt, "key": nkey, "label": nlabel}
                    if nt not in _LEAF_TYPES:
                        next_frontier.append(nid)

            frontier = next_frontier

        # Only emit edges whose endpoints both made it into the response
        node_keys = {n["key"] for n in seen.values()}
        edges = [e for e in edge_list
                 if e["source"] in node_keys and e["target"] in node_keys]
        # Cap edges; keep edges touching the center node first
        if len(edges) > _MAX_EDGES:
            edges.sort(key=lambda e: 0 if e["source"] == start_key or e["target"] == start_key else 1)
            edges = edges[:_MAX_EDGES]

        nodes = [_node_payload(n["node_type"], n["key"], n["label"]) for n in seen.values()]
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


def _resolve_section(act: str, section: str, depth: int, denied_types: set[str] = set()) -> dict:
    key = f"section:{act}:{section}"
    if _GRAPH_INDEX.lookup(key) is None:
        return {"nodes": [], "edges": []}
    return _resolve_from_graph(key, depth, denied_types)


def _resolve_ruling(citation: str, depth: int, denied_types: set[str] = set()) -> dict:
    key = f"public_ruling:{citation}"
    if _GRAPH_INDEX.lookup(key) is None:
        return {"nodes": [], "edges": []}
    return _resolve_from_graph(key, depth, denied_types)


def _gated(result: dict, avail: set[str]) -> dict:
    """Drop nodes of unavailable content types and any edge touching them.

    ponytail: post-BFS filter, so a depth>=2 walk may still route through a hidden
    node to reach visible ones; prune inside the BFS if that relation itself is sensitive.
    """
    nodes = [n for n in result["nodes"] if node_allowed(avail, n["id"])]
    keep = {n["id"] for n in nodes}
    edges = [e for e in result["edges"] if e["source"] in keep and e["target"] in keep]
    return {**result, "nodes": nodes, "edges": edges}


def _deny_404(avail: set[str], *keys: str):
    for k in keys:
        if not node_allowed(avail, k):
            return JSONResponse({"error": f"unknown graph key: {k}"}, status_code=404)
    return None


def _resolve_case(citation: str, depth: int, denied_types: set[str] = set()) -> dict:
    key = f"case:{citation}"
    if _GRAPH_INDEX.lookup(key) is None:
        return {"nodes": [], "edges": []}
    return _resolve_from_graph(key, depth, denied_types)


@router.get("/api/graph/serialize")
def graph_serialize(
    request: Request,
    key: str = Query(...),
    depth: int = Query(default=1, ge=1, le=2),
):
    """LLM-facing serialization of a graph node (graph spec §6.2).

    Query params:
      key:    canonical graph key, e.g. "section:itaa-1997:118-110"
      depth:  1 = node neighbourhood (≈80 tokens); 2 = + aggregated
              neighbourhood-of-neighbourhood (≤400 tokens, deduped).

    Returns {key, label, depth, tokens, text} where `text` is a token-lean
    block an LLM can read directly (counts + top exemplars per edge type).
    """
    from backend.services.graph_serialize import serialize as _serialize

    user = user_from(request)
    denied = _deny_404(available_keys(user), key)
    if denied:
        return denied
    out = _serialize(key, depth=depth, deny=frozenset(denied_node_types(user)))
    if out is None:
        return JSONResponse({"error": f"unknown graph key: {key}"}, status_code=404)
    return out


@router.get("/api/graph/path")
def graph_path(
    request: Request,
    from_key: str = Query(alias="from"),
    to_key: str = Query(alias="to"),
    max_hops: int = Query(default=10, ge=1, le=20),
):
    """Shortest path between two graph nodes (graph spec §6.3).

    Query params:
      from, to: canonical graph keys, e.g. "section:itaa-1997:118-110"
      max_hops: hop cap (1-20, default 10)

    Returns {from, to, hops, path, edges} where path is the ordered node
    list and edges give the typed connection per hop (undirected traversal,
    edge type preserved). Unreachable pairs return path: null with a reason.
    """
    from backend.services.graph_path import FRONTIER_CAP, FrontierExceeded, find_path

    avail = available_keys(user_from(request))
    denied = _deny_404(avail, from_key, to_key)
    if denied:
        return denied
    conn = sqlite3.connect(str(GRAPH_DB))
    try:
        def _resolve(k: str) -> int | None:
            row = conn.execute("SELECT id FROM nodes WHERE key=?", (k,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id FROM nodes WHERE lower(key)=?", (k.lower(),)).fetchone()
            return row[0] if row else None

        f_id = _resolve(from_key)
        t_id = _resolve(to_key)
        if f_id is None or t_id is None:
            missing = from_key if f_id is None else to_key
            return JSONResponse({"error": f"unknown graph key: {missing}"}, status_code=404)

        try:
            path, hops = find_path(conn, f_id, t_id, max_hops=max_hops)
        except FrontierExceeded as exc:
            return {"from": from_key, "to": to_key, "path": None, "hops": None,
                    "reason": f"frontier exceeded {exc} nodes at a level (cap {FRONTIER_CAP})"}

        if path is None:
            return {"from": from_key, "to": to_key, "path": None, "hops": None,
                    "reason": f"no path within {max_hops} hops"}

        ids = [nid for nid, _ in path]
        meta: dict[int, tuple[str, str]] = {}
        if ids:
            ph = ",".join("?" * len(ids))
            meta = {r[0]: (r[1], r[2]) for r in conn.execute(
                f"SELECT id, key, label FROM nodes WHERE id IN ({ph})", ids).fetchall()}
        # ponytail: a path through a hidden type is withheld, not re-routed around it.
        if not all(node_allowed(avail, meta.get(nid, ("", ""))[0] or "") for nid in ids):
            return {"from": from_key, "to": to_key, "path": None, "hops": None,
                    "reason": f"no path within {max_hops} hops"}

        return {
            "from": from_key,
            "to": to_key,
            "hops": hops,
            "path": [
                {"key": meta.get(nid, (None, str(nid)))[0],
                 "label": meta.get(nid, (None, str(nid)))[1]}
                for nid in ids
            ],
            "edges": [
                {"type": et, "from": i, "to": i + 1}
                for i, (_, et) in enumerate(path[1:], start=0)
                if et is not None
            ],
        }
    finally:
        conn.close()


def _key_type(key: str) -> str:
    """Map a canonical graph key to the /api/graph/data `type` vocabulary."""
    prefix = key.split(":", 1)[0]
    if prefix == "public_ruling":
        return "ruling"
    return prefix


@router.get("/api/graph/data")
def graph_data(
    request: Request,
    type: str | None = Query(default=None, alias="type"),
    act: str | None = Query(default=None),
    section: str | None = Query(default=None),
    citation: str | None = Query(default=None),
    ref: str | None = Query(default=None),
    depth: int = Query(default=1, ge=1, le=3),
):
    """Return nodes and edges for a force-directed graph centered on an item.

    Query params:
      type:      "section", "ruling", or "case"
      act:       act key (required for type=section), e.g. "itaa-1997"
      section:   section id (required for type=section), e.g. "8-1"
      citation:  citation (required for type=ruling or case), e.g. "TR 2025/1"
      ref:       raw ref resolved via the entity-alias map (alternative to
                 type+params), e.g. "FBTAA section 49"
      depth:     expansion depth (1-3, default 1). Higher = more neighbours.
    """
    if ref:
        key = alias_lookup(ref)
        if key is None:
            return JSONResponse({"error": f"could not resolve ref: {ref}"}, status_code=404)
        avail = available_keys(user_from(request))
        if not node_allowed(avail, key):
            return JSONResponse({"error": f"could not resolve ref: {ref}"}, status_code=404)
        result = _gated(_resolve_from_graph(key, depth, denied_node_types(user_from(request))), avail)
        result["meta"] = {"type": _key_type(key), "depth": depth,
                          "node_count": len(result["nodes"]), "edge_count": len(result["edges"])}
        return result

    denied_types = denied_node_types(user_from(request))
    if type == "section":
        if not act or not section:
            return JSONResponse({"error": "act and section required for type=section"}, status_code=400)
        result = _resolve_section(act, section, depth, denied_types)
    elif type == "ruling":
        if not citation:
            return JSONResponse({"error": "citation required for type=ruling"}, status_code=400)
        result = _resolve_ruling(citation, depth, denied_types)
    elif type == "case":
        if not citation:
            return JSONResponse({"error": "citation required for type=case"}, status_code=400)
        result = _resolve_case(citation, depth, denied_types)
    elif type == "private-ruling":
        if not citation:
            return JSONResponse({"error": "citation required for type=private-ruling"}, status_code=400)
        result = _resolve_from_graph(f"private_ruling:EV/{citation}", depth, denied_types)
    else:
        if type is None:
            return JSONResponse({"error": "type or ref required"}, status_code=400)
        return JSONResponse({"error": f"Unknown type: {type}"}, status_code=400)

    avail = available_keys(user_from(request))
    center = {"section": f"section:{act}:{section}", "ruling": "public_ruling",
              "case": "case", "private-ruling": "private_ruling"}[type]
    if not node_allowed(avail, center):
        return JSONResponse({"error": f"{type} is not available"}, status_code=404)
    result = _gated(result, avail)
    result["meta"] = {"type": type, "depth": depth, "node_count": len(result["nodes"]), "edge_count": len(result["edges"])}
    return result


# Edge type → panel display name (SmartLinkPanel groups).
_EDGE_DISPLAY = {
    "applies": "Applies To",  # ruling/case node → sections
    "applies_private": "Private Rulings",
    "interpreted_by": "Public Rulings",
    "considered_in": "Cases",
    "explained_in": "Commentary",
    "cites": "Citations",
    "consistent_with": "Consistent With",
    "defines": "Definitions",
}


def _related_item_url(node_type: str, key: str) -> str | None:
    """Frontend navigation URL for a graph neighbour (null = not navigable)."""
    if node_type == "section":
        _, act, sec = key.split(":", 2)
        return f"/{act}/{sec}"
    if node_type == "public_ruling":
        return f"/rulings/{key.split(':', 1)[1]}"
    if node_type == "private_ruling":
        return f"/private-rulings/{key.rsplit('/', 1)[-1]}"
    if node_type == "case":
        return f"/tax-cases/{key.split(':', 1)[1]}"
    if node_type == "regulatory_guide":
        # regulatory_guide:{n} or regulatory_guide:{n}:{para}
        return f"/regulatory-guides/{key.split(':')[1]}"
    if node_type == "afsa_guide":
        # afsa_guide:{slug} or afsa_guide:{slug}:{para}
        return f"/afsa-guides/{key.split(':')[1]}"
    if node_type == "commentary":
        # commentary:master-tax-guide:ch-10/exempt-entities-listing -> /master-tax-guide/exempt-entities-listing
        try:
            _, pub, path = key.split(":", 2)
            slug = path.split("/", 1)[1] if "/" in path else ""
            if slug:
                return f"/{pub}/{slug}"
        except Exception:
            pass
        return None  # chapter-level commentary has no single-section target
    return None  # definition has no standalone view yet


@router.get("/api/graph/related")
def graph_related(
    request: Request,
    key: str = Query(...),
    limit: int = Query(default=5, ge=1, le=100),
    edge_types: str | None = Query(default=None, description="Comma-separated filter, e.g. 'applies,interpreted_by'"),
):
    """Graph-driven related lists for the frontend SmartLinkPanel.

    For a graph node, returns per-edge-type groups with the total related-item
    count (distinct neighbours, not provenance-duplicated edge rows) and the
    top-N neighbours ranked by edge-row count. Product Rulings are excluded
    from a section's "interpreted_by" group. Each item carries key, label,
    node_type and a frontend navigation URL when one exists.

    Query params:
      key:        canonical graph key, e.g. "section:itaa-1997:8-1"
      limit:      items per group (1-100, default 5)
      edge_types: optional comma-separated filter of edge types

    Returns {key, label, groups: [{edge_type, display, total, items}]}.
    Unknown keys return 404.
    """
    avail = available_keys(user_from(request))
    denied = _deny_404(avail, key)
    if denied:
        return denied
    conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, timeout=10)
    try:
        center = conn.execute(
            "SELECT id, label, node_type FROM nodes WHERE key=? OR lower(key)=?",
            (key, key.lower()),
        ).fetchone()
        if center is None:
            return JSONResponse({"error": f"unknown graph key: {key}"}, status_code=404)
        cid, clabel, ctype = center

        filter_types = None
        if edge_types:
            filter_types = [et.strip() for et in edge_types.split(",") if et.strip()]

        sql = """
            SELECT e.edge_type, n.key AS nkey, n.label AS nlabel, n.node_type AS ntype,
                   COUNT(*) AS row_cnt
            FROM graph_edges e
            JOIN nodes n ON n.id = CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
            WHERE (e.source_id = ? OR e.target_id = ?)
        """
        params: list = [cid, cid, cid]
        if filter_types:
            sql += f" AND e.edge_type IN ({','.join('?' * len(filter_types))})"
            params += filter_types
        sql += " GROUP BY e.edge_type, n.id ORDER BY e.edge_type, row_cnt DESC"
        rows = conn.execute(sql, params).fetchall()

        # Merge edge types into panel groups. `applies` carries both public
        # and private rulings; the public ones are parallel edges to the same
        # pairs `interpreted_by` already covers (verified: identical
        # source/target pairs carry both types), so they're skipped to avoid
        # double-counting. Private ones form "Private Rulings".
        # `total` counts DISTINCT neighbours (one per grouped node row), not
        # provenance-duplicated edge rows — the same ruling reaching a section
        # from two extraction sources is one related item, not two.
        def _group_key(et: str, ntype: str) -> str | None:
            if et == "applies" and ntype == "public_ruling":
                return None  # parallel edge — already in interpreted_by
            if et == "applies" and ntype == "private_ruling":
                return "applies_private"
            return et

        groups: dict[str, dict] = {}
        meta = _citation_meta()
        for et, nkey, nlabel, ntype, row_cnt in rows:
            if ntype == "commentary":
                continue  # commentary is retired — no longer surfaced
            if not node_allowed(avail, nkey):
                continue
            # A Product Ruling names a provision for a specific product
            # arrangement; it does not interpret the section generally.
            if et == "interpreted_by" and ctype == "section" and _PRODUCT_RULING_RE.match(nlabel or ""):
                continue
            gk = _group_key(et, ntype)
            if gk is None:
                continue
            display = _EDGE_DISPLAY.get(gk, _EDGE_DISPLAY.get(et, et))
            g = groups.setdefault(gk, {"edge_type": gk, "display": display,
                                       "total": 0, "items": []})
            g["total"] += 1
            if len(g["items"]) < limit:
                item = {
                    "key": nkey,
                    "label": nlabel if ntype != "private_ruling" else f"EV/{nkey.rsplit('/', 1)[-1]}",
                    "node_type": ntype,
                    "url": _related_item_url(ntype, nkey),
                }
                if ntype == "case":
                    m = meta.get(nlabel)
                    if m:
                        item["name"] = m["name"]
                        item["year"] = m["year"]
                elif ntype == "public_ruling":
                    item["year"] = _ruling_year(nlabel)
                    m = meta.get(nlabel)
                    if m and m.get("name") and m["name"].strip() and m["name"] != nlabel:
                        item["name"] = m["name"]
                g["items"].append(item)

        return {"key": key, "label": clabel, "groups": list(groups.values())}
    finally:
        conn.close()