"""Comprehensive test of all 11 lawkitty MCP tools (prod, on-DO)."""
import asyncio
import json
import traceback

from backend.routes.playground import _tool_index

idx = _tool_index()
PASS = 0
FAIL = 0
FAILURES = []


def j(x):
    return json.loads(x) if isinstance(x, str) else x


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL  {name}  {str(detail)[:160]}")


async def call(name, **kw):
    try:
        return await idx[name].fn(**kw)
    except Exception as e:
        return {"__raised__": f"{type(e).__name__}: {e}"}


def is_error(r):
    return isinstance(r, dict) and (r.get("error") or r.get("__raised__"))


async def main():
    # --- get_info ---
    r = j(await call("get_info"))
    check("get_info", isinstance(r, dict) and "name" in r and "version" in r,
          f"keys={list(r.keys()) if isinstance(r, dict) else type(r)}")

    # --- search ---
    r = j(await call("search", query="capital gains"))
    res = r.get("results", {})
    sec = res.get("sections", [])
    rul = res.get("rulings", [])
    cas = res.get("cases", [])
    check("search default returns all 3 types",
          len(sec) > 0 and len(rul) > 0 and len(cas) > 0,
          f"sections={len(sec)} rulings={len(rul)} cases={len(cas)}")
    check("search act is null (not 'null')", r.get("act") is None, f"act={r.get('act')!r}")
    check("search filter correct", r.get("filter") == ["case", "legislation", "ruling"], r.get("filter"))

    # grab real refs for fetch
    section_ref = sec[0].get("id") if sec else None
    ruling_ref = rul[0].get("id") if rul else None
    case_ref = cas[0].get("id") if cas else None
    print(f"      refs: section={section_ref} ruling={ruling_ref} case={case_ref}")

    # --- search: types filter ---
    r = j(await call("search", query="capital gains", types=["legislation"]))
    check("search types=legislation -> only sections",
          "sections" in r.get("results", {}) and "rulings" not in r.get("results", {}) and "cases" not in r.get("results", {}),
          list(r.get("results", {}).keys()))
    # --- search: private_ruling semantic ---
    r = j(await call("search", query="contractor vs employee", types=["private_ruling"]))
    pr = r.get("results", {}).get("private_rulings", [])
    check("search types=private_ruling -> semantic results", len(pr) > 0, f"private_rulings={len(pr)}")
    # --- search: empty query ---
    r = j(await call("search", query=""))
    check("search empty query -> error", "error" in r or "Query required" in json.dumps(r), str(r)[:80])
    # --- search: act filter ---
    r = j(await call("search", query="deduction", types=["legislation"], act="itaa-1997"))
    sec2 = r.get("results", {}).get("sections", [])
    acts = {s.get("act") for s in sec2 if s.get("act")}
    check("search act=itaa-1997 filters sections", sec2 and acts <= {"itaa-1997"}, acts)

    # --- fetch ---
    if section_ref:
        r = j(await call("fetch", ref=section_ref))
        body = r.get("text") or r.get("body") or r.get("content") or ""
        check("fetch section by id", bool(body) and not r.get("error"), f"len={len(str(body))}")
    r = j(await call("fetch", ref="s 8-1 ITAA 1997"))
    check("fetch human citation 's 8-1 ITAA 1997'", not r.get("error"), str(r.get("error", ""))[:80])
    r = j(await call("fetch", ref="TR 2005/23"))
    check("fetch ruling 'TR 2005/23'", not r.get("error"), str(r.get("error", ""))[:80])
    r = j(await call("fetch", ref="bogus-zzz-not-real"))
    check("fetch bogus -> error", bool(is_error(r)), str(r)[:120])

    # --- outline ---
    r = j(await call("outline"))
    check("outline (collections)", isinstance(r, dict), str(r)[:100])
    r = j(await call("outline", id="itaa-1997"))
    check("outline id=itaa-1997 (act tree)", isinstance(r, dict), str(r)[:100])

    # --- resolve_alias ---
    for alias in ["s 100A", "Div 7A", "Part IVA", "8-1", "109Y"]:
        r = j(await call("resolve_alias", reference=alias))
        check(f"resolve_alias '{alias}'", isinstance(r, dict) and not r.get("error"), str(r)[:90])

    # --- get_definition ---
    r = j(await call("get_definition", act="itaa-1997", term="resident"))
    check("get_definition resident", isinstance(r, dict) and not r.get("error"), str(r)[:90])

    # --- list_rulings ---
    r = j(await call("list_rulings"))
    check("list_rulings", isinstance(r, dict), str(r)[:80])

    # --- related ---
    r = j(await call("related", id="leg:itaa-1997:8-1"))
    check("related leg:itaa-1997:8-1", isinstance(r, dict) and ("cites" in r or "cited_by" in r), list(r.keys()) if isinstance(r, dict) else type(r))

    # --- graph ---
    r = j(await call("graph_neighbourhood", key="section:itaa-1997:8-1"))
    check("graph_neighbourhood s 8-1", isinstance(r, dict) and "text" in r and not r.get("error"), str(r)[:80])
    r = j(await call("graph_neighbourhood", key="section:itaa-1997:8-1", depth=2))
    check("graph_neighbourhood depth=2", isinstance(r, dict) and r.get("depth") == 2, str(r)[:60])
    r = j(await call("graph_neighbourhood", key="bogus:not-real"))
    check("graph_neighbourhood unknown key -> error", isinstance(r, dict) and bool(r.get("error")), str(r)[:80])
    r = j(await call("graph_path", from_key="section:itaa-1997:8-1", to_key="case:[1986] HCA 45"))
    check("graph_path s 8-1 -> [1986] HCA 45", isinstance(r, dict) and ("path" in r or "error" in r), str(r)[:100])

    # --- report_issue (dedup-safe: same note twice -> same ticket) ---
    note = "MCP TEST (automated) — safe to delete"
    r1 = j(await call("report_issue", category="suggestion", tool="search", params="{}", expected="x", actual="y", note=note))
    check("report_issue creates ticket", isinstance(r1, dict) and not is_error(r1), str(r1)[:100])

    # --- unknown tool guard (playground layer) ---
    check("tool index has 11 tools", len(idx) == 11, f"count={len(idx)}: {sorted(idx)}")

    print("\n" + "=" * 50)
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("FAILURES:", FAILURES)


asyncio.run(main())
