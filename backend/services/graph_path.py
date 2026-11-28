"""Shortest-path queries (graph spec §6.3).

`GET /api/graph/path?from=KEY&to=KEY` answers "how does this ruling connect
to this case". Bidirectional BFS over the undirected projection of
graph_edges (traversal may walk either direction; the recorded edge type is
kept on each hop), with visited-set cycle guards and a hop cap.

Why Python BFS instead of the spec's suggested recursive CTE: SQLite
recursive CTEs blow up combinatorially on hub nodes (s 8-1 ≈ 18k edges) —
the spec's own §4 hub guard mandates per-hop limits. Bidirectional BFS with
batched level expansion (no N+1, the Phase 1 lesson) keeps hub-to-hub under
the G3 2s budget. Levels expand the smaller frontier first; a per-level
frontier cap bounds worst-case work on pathological pairs.

Return contract (service level): (path, hops) where path is a list of
(node_id, edge_type_in) tuples — edge_type_in is the edge connecting the
node to its predecessor (None for the root). Unreachable within max_hops
returns (None, None). Raises FrontierExceeded when a level's frontier
exceeds the cap.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MAX_HOPS = 10
FRONTIER_CAP = 25_000


class FrontierExceeded(Exception):
    """A BFS level's frontier exceeded the cap — path search aborted."""


def _expand(
    conn: sqlite3.Connection,
    frontier: set[int],
    visited: set[int],
    parent: dict[int, tuple[int | None, str | None]],
    other_visited: set[int],
    frontier_cap: int,
) -> int | None:
    """Expand one BFS level in place. Returns a meeting node id, or None."""
    if not frontier:
        return None
    if len(frontier) > frontier_cap:
        raise FrontierExceeded(len(frontier))

    ids = list(frontier)
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT source_id, target_id, edge_type FROM graph_edges "
        f"WHERE source_id IN ({ph}) OR target_id IN ({ph})",
        ids + ids,
    ).fetchall()
    id_set = set(ids)

    new_frontier: set[int] = set()
    for s, t, et in rows:
        nbr, prev = (t, s) if s in id_set else (s, t)
        if nbr in visited:
            continue
        if nbr not in parent:
            parent[nbr] = (prev, et)
        if nbr in other_visited:
            return nbr
        new_frontier.add(nbr)

    visited.update(new_frontier)
    frontier.clear()
    frontier.update(new_frontier)
    return None


def _reconstruct(
    f_parent: dict[int, tuple[int | None, str | None]],
    b_parent: dict[int, tuple[int | None, str | None]],
    meet: int,
    from_id: int,
    to_id: int,
) -> tuple[list[tuple[int, str | None]], int]:
    f_nodes: list[tuple[int, str | None]] = []
    node = meet
    while node is not None:
        prev, et = f_parent[node]
        f_nodes.append((node, et))
        node = prev
    f_nodes.reverse()  # [(from_id, None), ..., (meet, et_into_meet)]

    # chain = [(meet, et_a), (p1, et_b), ..., (pN, et_z)] where
    #   meet discovered from p1 via et_a, ..., pN discovered from to_id via et_z.
    # Path after meet: p1 (via et_a), p2 (via et_b), ..., to_id (via et_z).
    chain: list[tuple[int, str | None]] = []
    node = meet
    while True:
        prev, et = b_parent[node]
        if prev is None:
            break
        chain.append((node, et))
        node = prev

    b_rev: list[tuple[int, str | None]] = [
        (chain[i + 1][0], chain[i][1]) for i in range(len(chain) - 1)
    ]
    if chain:
        b_rev.append((to_id, chain[-1][1]))
    # when meet == to_id, chain is empty and to_id is already f_nodes[-1]

    path = f_nodes + b_rev
    return path, len(path) - 1


def find_path(
    conn: sqlite3.Connection,
    from_id: int,
    to_id: int,
    max_hops: int = MAX_HOPS,
    frontier_cap: int = FRONTIER_CAP,
) -> tuple[list[tuple[int, str | None]] | None, int | None]:
    """Shortest path between two node ids. See module docstring."""
    if from_id == to_id:
        return [], 0

    f_parent: dict[int, tuple[int | None, str | None]] = {from_id: (None, None)}
    b_parent: dict[int, tuple[int | None, str | None]] = {to_id: (None, None)}
    f_visited = {from_id}
    b_visited = {to_id}
    f_frontier = {from_id}
    b_frontier = {to_id}

    for _ in range(1, max_hops + 1):
        if len(f_frontier) <= len(b_frontier):
            meet = _expand(conn, f_frontier, f_visited, f_parent, b_visited, frontier_cap)
            if meet is not None:
                return _reconstruct(f_parent, b_parent, meet, from_id, to_id)
        else:
            meet = _expand(conn, b_frontier, b_visited, b_parent, f_visited, frontier_cap)
            if meet is not None:
                return _reconstruct(f_parent, b_parent, meet, from_id, to_id)

        if not f_frontier or not b_frontier:
            return None, None

    return None, None
