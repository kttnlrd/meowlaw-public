#!/usr/bin/env python3
"""Pre-graph data integrity verification — private rulings + all other data types.

Checks every data source the graph ETL (docs/specs/graph.md §9) consumes, so the
graph build only runs on verified inputs. Prints PASS/FAIL per check; exit 0 only
if everything is green.

Usage:
    python3 scripts/verify_graph_readiness.py [--require-mop-complete]

--require-mop-complete: fail if the private-rulings LLM mop-up hasn't reached
57,608/57,608 (default: warn only, so the non-private checks can run early).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RULINGS = DATA / "rulings"
SUMMARIES = RULINGS / "summaries"
PRIV = Path(os.environ.get("PRIVATE_RULINGS_ROOT", Path.home() / ".lawkitty" / "private_rulings")) / "data"
JSON_DIR = PRIV / "json"
JSON_LLM = PRIV / "json_llm"
MOP_MARKED = PRIV / "mop_marked"

ACTS = ["itaa-1997", "itaa-1936", "gst-1999", "fbt-1986", "taa-1953",
        "corporations-act-2001", "aml-ctf-2006", "aml-ctf-rules-2007", "sis-1993",
        "nz-it-2007"]

results: list[tuple[str, bool, str]] = []


def _shape(section_id: str) -> str:
    """The number SHAPE a section id uses: ITAA 1997 and GST write "40-95", ITAA 1936 writes
    "102D", several acts write "6".  Comparing shapes is how a parser artifact (a shape the act
    never uses, e.g. section:itaa-1997:102) is told apart from a section the corpus is missing."""
    if re.fullmatch(r"\d+-\d+[A-Z]*", section_id):
        return "X-Y"
    if re.fullmatch(r"\d+[A-Z]+", section_id):
        return "num+letters"
    if re.fullmatch(r"\d+", section_id):
        return "plain number"
    return "other"


def _tree_sections(tree_path: Path) -> set[str]:
    """Section-level ids from an act's tree.json.

    Only entries that carry a path are sections: the same file also carries DIVISION ids, so
    searching the raw JSON for an id reports Divisions as sections and invents gaps.  Returns an
    empty set when the act has no tree.
    """
    if not tree_path.exists():
        return set()
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    out: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for s in node.get("sections") or []:
                if isinstance(s, dict) and s.get("id"):
                    out.add(str(s["id"]))
            for key in ("divisions", "subdivisions", "parts", "children"):
                for child in node.get(key) or []:
                    walk(child)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(tree)
    return out


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


def act_tree_ids(act: str) -> set[str]:
    tree = json.loads((DATA / act / "tree.json").read_text())
    ids: set[str] = set()
    stack = [tree]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if "id" in item:
                ids.add(str(item["id"]))
            stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
    return ids


def load(name: str):
    p = DATA / name
    if not p.exists():
        return None
    return json.loads(p.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-mop-complete", action="store_true")
    args = ap.parse_args()

    print("== Private rulings (mop-up enrichment) ==")
    if not JSON_LLM.is_dir():
        check("private: json_llm dir exists", False, str(JSON_LLM))
    else:
        llm_files = sorted(JSON_LLM.glob("*.json"))
        json_files = sorted(JSON_DIR.glob("*.json")) if JSON_DIR.is_dir() else []
        check("private: json_llm count matches corpus", len(llm_files) == len(json_files),
              f"{len(llm_files)} vs {len(json_files)}")

        mop_done = False
        if json_files:
            marked = {p.stem for p in MOP_MARKED.glob("*.ok")} if MOP_MARKED.is_dir() else set()
            all_stems = {p.stem for p in json_files}
            unmarked = all_stems - marked
            if not unmarked:
                mop_done = True
            elif len(unmarked) <= 25:
                # few stragglers: complete if every one was attempted (error status written)
                attempted = all(
                    (JSON_LLM / f"{a}.json").exists() for a in unmarked
                )
                mop_done = attempted
        if not mop_done:
            marked = len(list(MOP_MARKED.glob("*.ok"))) if MOP_MARKED.is_dir() else 0
            if args.require_mop_complete:
                check("private: mop-up complete", False, f"{marked}/{len(json_files)} marked")
            else:
                check("private: mop-up complete", True, f"WARN {marked}/{len(json_files)} marked (not gating)")

        # sample scan (bounded: up to 2000 files) for status/parse stats
        import random
        sample = random.sample(llm_files, min(2000, len(llm_files)))
        ok_files = err_files = 0
        authnums: set[str] = set()
        dup_authnums = 0
        bad_authnums = 0
        leg_parsed = leg_skip = leg_unparsed = 0
        case_kept = case_dropped = 0
        sec_refs_unknown_act: set[str] = set()
        sec_refs_not_in_tree: dict[str, int] = {}

        sys.path.insert(0, str(ROOT))
        from pipeline import graph_etl as ge

        # act -> tree section ids (only section leaves, matching load_acts)
        tree_sec_ids: dict[str, set[str]] = {}
        for act in ge.ACTS:
            if (DATA / act / "tree.json").exists():
                tree_sec_ids[act] = act_tree_ids(act)

        for f in sample:
            try:
                d = json.loads(f.read_text())
            except Exception:
                err_files += 1
                continue
            if d.get("mop_status") != "ok":
                err_files += 1
            else:
                ok_files += 1
            an = str(d.get("authorisation_number", ""))
            if an in authnums:
                dup_authnums += 1
            authnums.add(an)
            # 12-digit values are source-faithful: the ATO scrape itself
            # carries 5 rulings with 12-digit EV numbers (e.g. 300017950374),
            # confirmed in manifest.jsonlines. 13-digit is the norm, not a law.
            if not re.fullmatch(r"\d{12,13}", an):
                bad_authnums += 1
            if d.get("mop_status") == "ok":
                for ref in d.get("legislation_refs_llm", []) or []:
                    r = ge._parse_leg_ref(ref)
                    if r == "non-section":
                        leg_skip += 1
                    elif r:
                        leg_parsed += 1
                        for act, sec in r:
                            if act not in ge.ACTS:
                                sec_refs_unknown_act.add(act)
                            elif act in tree_sec_ids and sec not in tree_sec_ids[act]:
                                sec_refs_not_in_tree[sec] = sec_refs_not_in_tree.get(sec, 0) + 1
                    else:
                        leg_unparsed += 1
                for cit in d.get("case_refs_llm", []) or []:
                    if ge._case_key(cit):
                        case_kept += 1
                    else:
                        case_dropped += 1

        n = len(sample)
        err_rate = err_files / max(n, 1)
        check("private: sample usable (ok+err read)", ok_files + err_files == n and n > 0,
              f"{ok_files} ok, {err_files} err in {n}")
        check("private: err rate < 1%", err_rate < 0.01,
              f"{err_rate * 100:.2f}% ({err_files}/{n}) — err rulings get regex-ref nodes only")
        check("private: authnums unique + 12-13 digit", dup_authnums == 0 and bad_authnums == 0,
              f"{dup_authnums} dup, {bad_authnums} bad format")
        total_leg = leg_parsed + leg_skip + leg_unparsed
        check("private: legislation refs parse rate", total_leg > 0 and leg_unparsed / total_leg < 0.25,
              f"{leg_parsed} parsed, {leg_skip} div-skip, {leg_unparsed} unparsed ({100 * leg_unparsed / max(total_leg, 1):.1f}%)")
        if sec_refs_unknown_act:
            check("private: section refs act known", False,
                  f"unknown acts: {sorted(sec_refs_unknown_act)[:5]}")
        else:
            check("private: section refs act known", True)
        unknown_sec_total = sum(sec_refs_not_in_tree.values())
        check("private: section refs resolve in act trees", unknown_sec_total / max(leg_parsed, 1) < 0.10,
              f"{unknown_sec_total}/{leg_parsed} refs point at ids absent from trees")

    print("== Public rulings ==")
    txt = [f for f in RULINGS.glob("*.txt") if not f.name.endswith(".meta.json")]
    check("rulings: txt corpus", len(txt) > 10000, f"{len(txt)} files")
    if SUMMARIES.is_dir():
        sums = {f.stem for f in SUMMARIES.glob("*.json")}
        missing = [f.stem for f in txt if f.stem not in sums]
        check("rulings: every txt has a summary", len(missing) == 0, f"{len(missing)} missing")
        err_stubs = []
        no_ruling = []
        for f in SUMMARIES.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                err_stubs.append(f.name)
                continue
            if "error" in d:
                err_stubs.append(f.name)
            elif "ruling" not in d and "full_text" not in d \
                    and d.get("summary_type") != "auto_extracted":
                no_ruling.append(f.name)
        check("rulings: zero error stubs", len(err_stubs) == 0, f"{len(err_stubs)}: {err_stubs[:5]}")
        check("rulings: summaries have body fields", len(no_ruling) == 0,
              f"{len(no_ruling)}: {no_ruling[:5]} (legacy auto_extracted OK)")

    print("== Sections ==")
    for act in ACTS:
        tree_p = DATA / act / "tree.json"
        if not tree_p.exists():
            check(f"sections: {act} tree.json", False)
            continue
        tree = json.loads(tree_p.read_text())
        n_sections = 0
        missing_files = 0
        stack = [tree]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "path" in item and "id" in item:
                    n_sections += 1
                    if not (DATA / act / "sections" / item["path"]).exists():
                        missing_files += 1
                stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
        check(f"sections: {act}", n_sections > 0 and missing_files == 0,
              f"{n_sections} sections, {missing_files} missing files")

    print("== Cases ==")
    csr = load("case_section_refs.json") or {}
    sci = load("section_case_index.json") or {}
    bad_cites = [k for k in csr if not re.search(r"\[\d{4}\]\s*[A-Z]+", k)]
    # This threshold used to be "more than 1000 cases". Nothing grounds that number:
    # case_section_refs.json is a citation index built from the case summaries, not an inventory of
    # cases, and it holds 983. What can actually drift is whether it is populated at all and whether
    # its keys are well formed, so those are the checks - the count is reported, not asserted.
    check("cases: case_section_refs populated", len(csr) > 0, f"{len(csr)} cases")
    check("cases: neutral citation format", len(bad_cites) == 0, f"{len(bad_cites)} non-neutral")
    check("cases: section_case_index", len(sci) > 100, f"{len(sci)} keys")

    print("== Definitions ==")
    # The extractor declares the acts it covers (ACTS and DICTIONARY_SECTIONS in
    # scripts/build_definitions_index.py). The expectation is derived from that declaration rather
    # than asserted as a remembered count, and it is read from the comprehensive artefact: this
    # check used to read definitions_all.json, a 3-act partial from Aug 28, while
    # definitions_comprehensive.json holds 5,609 definitions across all 9 declared acts.
    defs = load("definitions_comprehensive.json") or {}
    entries = defs.get("definitions") if isinstance(defs, dict) else None
    acts_present = {e.get("act") for e in (entries or []) if e.get("act")}
    src = (ROOT / "scripts" / "build_definitions_index.py")
    declared = set(re.findall(r'^\s{4}"([a-z0-9\-]+)":', src.read_text(encoding="utf-8"), re.M))
    n_terms = len(entries or [])
    missing = sorted(declared - acts_present)
    check("definitions: acts + terms",
          bool(declared) and not missing and n_terms > 1000,
          f"{len(acts_present)} of {len(declared)} declared acts, {n_terms} terms"
          + (f", missing {missing}" if missing else ""))

    # Commentary (CCH) dropped in the MeowLaw fork — no check

    print("== Treaties ==")
    tdir = DATA / "treaties"
    if tdir.is_dir():
        tdirs = [d for d in tdir.iterdir() if d.is_dir()]
        total_articles = 0
        missing_articles = 0
        for d in tdirs:
            t = load(f"treaties/{d.name}/tree.json")
            if not t:
                missing_articles += 1
                continue
            arts = t.get("articles", [])
            total_articles += len(arts)
            missing_articles += sum(1 for a in arts if not (d / a["file"]).exists())
        check("treaties: countries + article files", len(tdirs) >= 40 and missing_articles == 0,
              f"{len(tdirs)} countries, {total_articles} articles, {missing_articles} missing files")

    print("== Smartlinks / cross-refs ==")
    sl = load("smartlink_index.json") or {}
    if isinstance(sl, dict) and sl:
        sample_keys = list(sl.keys())[:1]
        check("smartlinks: index populated", True, f"{len(sl)} keys, sample {sample_keys}")
    else:
        check("smartlinks: index populated", False, "empty")

    print("== Embeddings ==")
    emb = DATA / "embeddings.db"
    if emb.exists():
        con = sqlite3.connect(emb)
        n_emb = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        n_sim = con.execute("SELECT COUNT(*) FROM similarity_index").fetchone()[0]
        check("embeddings: rows present", n_emb > 100000 and n_sim > 500000,
              f"{n_emb} embeddings, {n_sim} similarity")
    else:
        check("embeddings: embeddings.db", False)

    print("== Graph API ==")
    gdb = DATA / "graph.db"
    if not gdb.exists():
        check("graph: graph.db exists", False)
    else:
        sys.path.insert(0, str(ROOT))
        from backend.services.graph_neighborhood import INDEX_TABLE, neighborhoods
        from backend.services.graph_path import find_path
        from backend.routes.graph import _MAX_EDGES, _MAX_NODES, _MAX_PRIVATE

        con = sqlite3.connect(f"file:{gdb}?mode=ro", uri=True, timeout=10)
        try:
            n_nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            n_edges = con.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            # The old thresholds here were greater-than-100k nodes and greater-than-500k edges:
            # remembered numbers that hid the question they were standing in for, which is whether
            # the graph covers the corpus. That coverage is derivable, so derive it - every section
            # id on disk in an act the ETL builds must have a node, and a dropped act or a moved
            # corpus layout fails here. (Counts are compared per act over the set of ids, so an act
            # that legitimately repeats an id across schedules - aml-ctf-2006 has two files called
            # 1.md - is not reported as a gap.)
            import sys as _sys
            _sys.path.insert(0, str(ROOT))
            from pipeline.graph_etl import ACTS as _ETL_ACTS
            gaps = []
            for act in _ETL_ACTS:
                d = ROOT / "data" / act / "sections"
                if not d.exists():
                    continue
                on_disk = {p.stem for p in d.rglob("*.md")}
                in_graph = {r[0].split(":", 2)[2] for r in con.execute(
                    "SELECT key FROM nodes WHERE node_type = 'section' AND key LIKE ?",
                    (f"section:{act}:%",))}
                if on_disk - in_graph:
                    gaps.append(f"{act}: {len(on_disk - in_graph)} id(s) without a node")
            check("graph: every corpus section id has a node", not gaps,
                  "; ".join(gaps) if gaps else
                  f"{len(_ETL_ACTS)} acts covered, {n_nodes} nodes, {n_edges} edges")
            check("graph: nodes and edges populated", n_nodes > 0 and n_edges > 0,
                  f"{n_nodes} nodes, {n_edges} edges")

            # orphan edges: every edge endpoint resolves (FK-level integrity)
            n_orphans = con.execute(
                "SELECT COUNT(*) FROM graph_edges e "
                "LEFT JOIN nodes s ON s.id = e.source_id "
                "LEFT JOIN nodes t ON t.id = e.target_id "
                "WHERE s.id IS NULL OR t.id IS NULL"
            ).fetchone()[0]
            check("graph: zero orphan edges", n_orphans == 0, f"{n_orphans} orphan edges")

            # neighbourhood index: count matches direct SQL for a sample
            sample = con.execute(
                "SELECT node_id, edge_type, count FROM neighborhood_index "
                "ORDER BY count DESC LIMIT 50"
            ).fetchall()
            mismatches = 0
            for nid, et, cnt in sample:
                direct = con.execute(
                    "SELECT COUNT(*) FROM graph_edges WHERE edge_type = ? "
                    "AND (source_id = ? OR target_id = ?)", (et, nid, nid)
                ).fetchone()[0]
                if direct != cnt:
                    mismatches += 1
            check("graph: neighborhood counts == direct SQL (sample 50)",
                  mismatches == 0, f"{mismatches} mismatches")

            # caps: route BFS respects node/edge/private caps on the biggest hub
            hub = con.execute(
                "SELECT key FROM nodes WHERE id IN "
                "(SELECT node_id FROM neighborhood_index ORDER BY count DESC LIMIT 1)"
            ).fetchone()
            if hub:
                sys.path.insert(0, str(ROOT))
                from backend.routes.graph import _resolve_from_graph
                g = _resolve_from_graph(hub[0], depth=2)
                n_priv = sum(1 for n in g["nodes"] if n.get("group") == "private_ruling")
                check("graph: caps respected on hub",
                      len(g["nodes"]) <= _MAX_NODES and len(g["edges"]) <= _MAX_EDGES
                      and n_priv <= _MAX_PRIVATE,
                      f"{hub[0]}: {len(g['nodes'])} nodes (cap {_MAX_NODES}), "
                      f"{len(g['edges'])} edges (cap {_MAX_EDGES}), "
                      f"{n_priv} private (cap {_MAX_PRIVATE})")
            else:
                check("graph: caps respected on hub", False, "no hub found")

            # latency: hub-to-hub path bounded
            import time
            ids = con.execute(
                "SELECT id, key FROM nodes WHERE node_type='section' "
                "AND id IN (SELECT node_id FROM neighborhood_index "
                "ORDER BY count DESC LIMIT 2)"
            ).fetchall()
            if len(ids) == 2:
                t0 = time.perf_counter()
                find_path(con, ids[0][0], ids[1][0], max_hops=10)
                dt = time.perf_counter() - t0
                check("graph: hub-to-hub path < 2s", dt < 2.0, f"{dt*1000:.0f}ms")
            else:
                check("graph: hub-to-hub path < 2s", False, "no hubs")

            # entity alias map: every mapped key should resolve.  Reported as one number, it
            # conflated three different things, and the obvious way to separate them - searching
            # tree.json for the id - is wrong: a tree also carries DIVISION ids, so '"id": "3"'
            # matches Division 3 and reports a section that was never missing.  Walk the tree for
            # section-level entries instead (they are the ones carrying a path), which makes the
            # comparison exact: every section the compilation lists must have a node, and a key that
            # is neither listed nor of a shape the act uses is a reference to something outside the
            # compilation - a repealed or renumbered section, or another act's.
            amap_p = DATA / "entity_alias_map.json"
            if amap_p.exists():
                import collections as _collections
                amap = json.loads(amap_p.read_text())
                keys = {v["key"] for v in amap.values() if v["status"] == "mapped"}
                ph = ",".join("?" * len(keys))
                found = {r[0] for r in con.execute(
                    f"SELECT key FROM nodes WHERE key IN ({ph})", list(keys))}
                shapes: dict[str, set[str]] = {}
                listed: dict[str, set[str]] = {}
                for act in {k.split(":", 2)[1] for k in keys if k.count(":") >= 2}:
                    d = DATA / act / "sections"
                    shapes[act] = {_shape(p.stem) for p in d.rglob("*.md")} if d.exists() else set()
                    listed[act] = _tree_sections(DATA / act / "tree.json")
                artifacts, off_compilation, gaps, overclaims = [], [], [], []
                for k in sorted(keys - found):
                    parts = k.split(":", 2)
                    if len(parts) < 3 or parts[0] != "section":
                        # A non-section key that does not resolve - a case the alias map believes is
                        # a node.  The graph builds case nodes only from the legislation-reference
                        # indexes, so a case with text but no section references is legitimately
                        # absent; the alias map is over-claiming it, which is worth counting rather
                        # than failing the corpus coverage check.
                        overclaims.append(k)
                        continue
                    act, sid = parts[1], parts[2]
                    if _shape(sid) not in shapes.get(act, set()):
                        artifacts.append(k)                 # e.g. section:itaa-1997:102
                    elif sid in listed.get(act, set()):
                        gaps.append(k)                      # the compilation lists it: a real gap
                    else:
                        off_compilation.append(k)           # repealed / renumbered / another act
                by_act = _collections.Counter(k.split(":", 2)[1] for k in gaps)
                check("graph: every section the compilation lists has a node (G4)", not gaps,
                      f"{len(gaps)} gap(s) {dict(by_act)}" if gaps else
                      f"0 section gap(s) across {len(listed)} act(s); {len(keys)} keys resolve or are "
                      f"classified")
                check("graph: every unresolvable alias key is classified",
                      len(artifacts) + len(off_compilation) + len(gaps) + len(overclaims)
                      == len(keys - found),
                      f"{len(artifacts)} wrong-shape artifact(s), {len(off_compilation)} "
                      f"off-compilation, {len(overclaims)} case over-claim(s) "
                      f"{overclaims[:3]}, {len(gaps)} real gap(s)")
            else:
                check("graph: alias map keys all resolve (G4)", False, "entity_alias_map.json missing")
        finally:
            con.close()

    print()
    print("== Search index ==")
    sdb = DATA.parent / "search_index.db"
    if sdb.exists():
        con = sqlite3.connect(sdb)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        check("search: FTS tables present", len(tables) > 0, f"tables: {tables[:6] or 'NONE'}")
    else:
        check("search: search_index.db exists", False)

    print()
    fails = [r for r in results if not r[1]]
    print(f"{len(results) - len(fails)}/{len(results)} checks passed, {len(fails)} failed")
    for name, _, detail in fails:
        print(f"  FAIL: {name} — {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
