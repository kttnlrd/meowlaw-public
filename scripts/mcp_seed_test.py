#!/usr/bin/env python3
"""Seeded random MCP-call test for LawKitty (streamable-HTTP MCP).

Fires reproducible, randomly-selected calls against the streamable-HTTP MCP
endpoint, covering every read-only tool, and asserts each call returns a
structurally-valid (non-error) result. Same seed => same call sequence => same
outcome, so a failure replays exactly with `--seed <n>`.

Methodology (randomised audit): the default seed is today's date (YYYYMMDD), so
every run explores a fresh input combination; `--seed N` pins a sequence for
exact replay. A sweep that never varies is a fixed-value test, not an audit.

Tools covered (11 of 12): search, fetch, resolve_alias, get_definition,
get_info, list_rulings, outline, related, search_private_rulings,
graph_neighbourhood, graph_path. `report_issue` is excluded deliberately — it
writes to the issue queue (a side effect) and belongs to a dedicated run.

Usage:
  export LAWKITTY_MCP_TOKEN=...
  python scripts/mcp_seed_test.py --n 60                 # today's seed
  python scripts/mcp_seed_test.py --seed 42 --n 100      # replay exact sequence
  python scripts/mcp_seed_test.py --seed 0 --n 50        # non-deterministic
  python scripts/mcp_seed_test.py --url http://127.0.0.1:8769/mcp --n 20

Exit 0 = every call passed; 1 = transport/parse failure; 2 = tool returned an
error for a known-valid input (a contract/data regression).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import httpx

# ── input pools: known-valid references, verified against prod ─────────────

_FETCH_REFS = [
    # party names + citations (cases)
    "Wakim", "McNally", "Ready Mixed Concrete", "Evolution Mining",
    "[1999] HCA 27", "[1969] HCA 12", "[2007] FCA 51", "[2026] FCA 935",
    # public rulings
    "TR 2023/1", "TD 2014/26", "TR 2002/5", "PCG 2023/1",
    # treaties
    "dta:japan:12", "dta:switzerland:23", "dta:germany:10", "dta:united-kingdom:7",
    # legislation
    "leg:itaa-1997:8-1", "leg:corporations-act-2001:912A",
]

_SEARCH_QUERIES = [
    "cryptocurrency capital gains", "foreign resident CGT discount",
    "fringe benefits car logbook", "superannuation death benefit tax",
    "trust income assessment", "capital allowance depreciation",
    "residency source income", "GST supply going concern",
    "research and development tax offset", "thin capitalisation debt",
    "non-commercial loss", "general anti-avoidance Part IVA",
]
_SEARCH_TYPES = [
    None, ["case"], ["ruling"], ["legislation"], ["treaty"], ["case", "ruling"],
]
_SEARCH_STATUS = ["all", "current"]

_ALIAS_REFS = [
    "FBTAA section 49", "ITAA 1997 s 8-1", "ITAA 1936 s 100A",
    "CGT event A1", "Division 7A loan", "GST Act", "Corporations Act s 912A",
    "TAA 1953", "s 995-1 dictionary", "FBT car parking",
]

_DEFINITIONS = [
    ("income-tax-assessment-act-1997", "business"),
    ("income-tax-assessment-act-1997", "capital gain"),
    ("income-tax-assessment-act-1997", "associate"),
    ("fringe-benefits-tax-assessment-act-1986", "car"),
    ("a-new-tax-system-gst-act-1999", "supply"),
    ("income-tax-assessment-act-1936", "resident"),
]

_GRAPH_KEYS = [
    "section:itaa-1997:8-1", "section:itaa-1997:50-50", "section:fbt-1986:49",
    "section:itaa-1936:100A", "section:itaa-1997:995-1",
    "section:itaa-1997:118-110", "public_ruling:TR 2023/1", "case:[2015] HCA 48",
]
_GRAPH_PATHS = [
    ("section:itaa-1997:8-1", "section:itaa-1997:50-50"),
    ("section:fbt-1986:49", "section:itaa-1997:8-1"),
    ("section:itaa-1936:100A", "section:itaa-1997:8-1"),
]

_PR_QUERIES = [
    "cryptocurrency staking", "trust distribution", "negative gearing",
    "carry forward losses", "CGT main residence",
]

_RELATED_IDS = [
    "leg:itaa-1997:8-1", "leg:itaa-1997:50-50", "leg:itaa-1997:995-1",
    "leg:corporations-act-2001:912A",
]

_LIST_RULINGS_YEARS = [2022, 2023, 2024, 2025]


# ── MCP wire calls ──────────────────────────────────────────────────────────

def mcp_call(url: str, tool: str, arguments: dict, _id: int = 1):
    """One JSON-RPC tools/call over streamable HTTP. Returns (ok, parsed_text)."""
    payload = {
        "jsonrpc": "2.0", "id": _id, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    try:
        r = httpx.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=60,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        return False, {"_transport": f"{type(e).__name__}: {e}"}

    body = r.json()
    result = body.get("result", {})
    if result.get("isError"):
        content = result.get("content", [])
        txt = content[0].get("text", "") if content else str(content)
        return False, {"_isError": txt[:200]}

    content = result.get("content", [])
    if not content:
        return False, {"_empty": "no content"}
    text = content[0].get("text", "")
    try:
        return True, json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return True, {"_raw": text}


# ── per-tool structural validators ──────────────────────────────────────────

def _sig(data) -> str:
    """Short structural summary for the report line."""
    if isinstance(data, dict):
        if "_transport" in data or "_isError" in data or "_empty" in data:
            return str(data)[:60]
        keys = list(data.keys())
        return f"keys={keys[:6]}"
    if isinstance(data, str):
        return f"len={len(data)}ch"
    return f"type={type(data).__name__}"


_VALIDATORS = {
    "search": lambda d: "results" in d and "error" not in d,
    "fetch": lambda d: any(k in d for k in ("id", "title", "content", "court", "frontmatter")) and "error" not in d,
    "resolve_alias": lambda d: isinstance(d, dict) and "error" not in d,
    "get_definition": lambda d: any(k in d for k in ("term", "text", "section")) and "error" not in d,
    "get_info": lambda d: isinstance(d, dict) and "version" in d,
    "list_rulings": lambda d: isinstance(d, dict) and "by_year" in d,
    "outline": lambda d: any(k in d for k in ("collections", "acts", "spec")),
    "related": lambda d: isinstance(d, dict) and "cites" in d,
    "search_private_rulings": lambda d: any(k in d for k in ("results", "rulings", "items", "total")) and "error" not in d,
    "graph_neighbourhood": lambda d: isinstance(d, dict) and "text" in d,
    "graph_path": lambda d: isinstance(d, dict) and "path" in d,
}


def build_calls(rng):
    """Yield (tool, arguments) pairs for the whole run (generator)."""
    def fetch():
        return {"ref": rng.choice(_FETCH_REFS)}

    def search():
        q = rng.choice(_SEARCH_QUERIES)
        a = {"query": q, "status": rng.choice(_SEARCH_STATUS)}
        t = rng.choice(_SEARCH_TYPES)
        if t:
            a["types"] = t
        return a

    def resolve_alias():
        return {"reference": rng.choice(_ALIAS_REFS)}

    def get_definition():
        act, term = rng.choice(_DEFINITIONS)
        return {"act": act, "term": term}

    def get_info():
        return {}

    def list_rulings():
        a = {"counts_only": rng.random() < 0.5}
        if rng.random() < 0.5:
            a["year"] = rng.choice(_LIST_RULINGS_YEARS)
        return a

    def outline():
        return {}

    def related():
        return {"id": rng.choice(_RELATED_IDS), "limit": rng.choice([3, 5, 10])}

    def search_private_rulings():
        return {"query": rng.choice(_PR_QUERIES)}

    def graph_neighbourhood():
        return {"key": rng.choice(_GRAPH_KEYS), "depth": rng.choice([1, 2])}

    def graph_path():
        frm, to = rng.choice(_GRAPH_PATHS)
        return {"from_key": frm, "to_key": to}

    tools = [
        ("search", search), ("fetch", fetch), ("resolve_alias", resolve_alias),
        ("get_definition", get_definition), ("get_info", get_info),
        ("list_rulings", list_rulings), ("outline", outline),
        ("related", related), ("search_private_rulings", search_private_rulings),
        ("graph_neighbourhood", graph_neighbourhood), ("graph_path", graph_path),
    ]

    # Ensure every tool is exercised at least once, then random for the rest.
    order = list(range(len(tools)))
    rng.shuffle(order)
    for i in order:
        name, fn = tools[i]
        yield name, fn()
    while True:
        name, fn = rng.choice(tools)
        yield name, fn()


def main() -> int:
    ap = argparse.ArgumentParser(description="Seeded random MCP-call test for LawKitty.")
    ap.add_argument("--url", default="https://lawkitty.app/mcp",
                    help="MCP base URL (default: https://lawkitty.app/mcp)")
    ap.add_argument("--token", default=None, help="MCP token (or $LAWKITTY_MCP_TOKEN)")
    ap.add_argument("--seed", type=int,
                    default=int(date.today().strftime("%Y%m%d")),
                    help="random seed (default: today YYYYMMDD; 0 = non-deterministic)")
    ap.add_argument("--n", type=int, default=60, help="number of calls (default 60)")
    args = ap.parse_args()

    token = args.token or __import__("os").environ.get("LAWKITTY_MCP_TOKEN", "")
    if not token:
        print("error: set --token or $LAWKITTY_MCP_TOKEN", file=sys.stderr)
        return 1
    url = f"{args.url.rstrip('/')}/{token}"

    seed = args.seed if args.seed else None  # 0 -> non-deterministic
    rng = __import__("random").Random(seed)
    print(f"MCP seed test: url={args.url}/<token> seed={args.seed} n={args.n}\n")

    calls = build_calls(rng)
    passed = failed = 0
    failures = []
    for i in range(args.n):
        tool, arguments = next(calls)
        ok, data = mcp_call(url, tool, arguments)
        if ok and _VALIDATORS[tool](data):
            passed += 1
            print(f"  PASS  {tool:24s} {_sig(data)}")
        else:
            failed += 1
            failures.append((tool, arguments, _sig(data)))
            print(f"  FAIL  {tool:24s} args={arguments} -> {_sig(data)}")

    print(f"\n=== {passed} pass, {failed} fail (seed={args.seed}, n={args.n}) ===")
    if failures:
        print("\nFailures (replay with --seed %d):" % args.seed)
        for tool, arguments, detail in failures:
            print(f"  {tool} {json.dumps(arguments)} -> {detail}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
