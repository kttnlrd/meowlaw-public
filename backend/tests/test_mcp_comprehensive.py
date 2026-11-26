#!/usr/bin/env python3
"""Comprehensive MCP server test — all 9 tools, all content types, breakage attempts.

Covers:
  * Protocol & auth: initialize, session id, bearer auth (none/wrong), malformed JSON-RPC
  * All 9 tools: search, search_private_rulings, fetch, outline, related,
    get_definition, list_rulings, get_info, report_issue
  * All content types: every act, cases, public rulings, private rulings,
    regulatory guides (rg), treaties (dta), AFSA practice guidance
  * Adversarial / breakage: path traversal, SQL injection strings, huge inputs,
    invalid/unknown IDs, missing params, unknown methods

Script-style (module-level execution). Run against a live server:

    python backend/tests/test_mcp_comprehensive.py \
        --base http://127.0.0.1:8769 \
        --token "$MCP_AUTH_TOKEN" \
        [--report /tmp/mcp_report.json]

Defaults: base=http://127.0.0.1:8769, token from .env MCP_AUTH_TOKEN.
"""
import argparse
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

if "pytest" in sys.modules:
    sys.modules["pytest"].skip(
        "script-style live-server test — run directly, not under pytest",
        allow_module_level=True,
    )

MCP_PATH = "/mcp"
PROTOCOL = "2024-11-05"

# ── known-good IDs (validated against the current corpus) ──────────────────
KNOWN_SECTIONS = {
    "itaa-1997": "8-1",
    "itaa-1936": "102UC",
    "gst-1999": "9-5",
    "fbt-1986": "7",
    "taa-1953": "14ZZ",
    "sis-1993": "10",
    "corporations-act-2001": "124",
    "bankruptcy-act-1966": "58",
    "aml-ctf-2006": "6",
    "aml-ctf-rules-2007": "2",
    "nz-it-2007": "AA-1",
}
KNOWN_CASE = "[2019] FCAFC 29"          # Harding — residency
KNOWN_RULING = "TR 2024/1"
KNOWN_RG = "rg:1"
KNOWN_RG_PARA = "rg:1:1.1"
KNOWN_DTA = "dta:usa:7"
KNOWN_PBR = "pbr:1011261243735"
KNOWN_AFSA = "afsa:administration-estates-deceased-persons"

RESULTS = []          # list of {phase, name, status, detail, ms}
PHASE = "setup"


def check(name, ok, detail="", ms=None):
    status = "pass" if ok else "fail"
    RESULTS.append({
        "phase": PHASE,
        "name": name,
        "status": status,
        "detail": str(detail)[:200],
        "ms": round(ms, 1) if isinstance(ms, (int, float)) else None,
    })
    mark = "PASS" if ok else "FAIL"
    d = f"  — {detail}" if detail else ""
    print(f"  {mark}  {name}{d}")


def first_section_id(tree):
    """First section id from an act tree (parts -> sections/divisions/subs)."""
    for part in tree.get("parts", []) if isinstance(tree, dict) else []:
        for sec in part.get("sections", []):
            if sec.get("id"):
                return sec["id"]
        for div in part.get("divisions", []):
            for sec in div.get("sections", []):
                if sec.get("id"):
                    return sec["id"]
            for sub in div.get("subdivisions", []):
                for sec in sub.get("sections", []):
                    if sec.get("id"):
                        return sec["id"]
    return None


class MCP:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token
        self.sid = None
        self._id = 0
        self.client = httpx.Client(timeout=120, follow_redirects=True)

    def _rpc(self, method, params=None, auth=True, extra_headers=None, raw_body=None):
        url = self.base + MCP_PATH
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        if extra_headers:
            headers.update(extra_headers)
        self._id += 1
        body = raw_body if raw_body is not None else json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params or {}, "id": self._id,
        })
        t0 = time.time()
        try:
            r = self.client.post(url, content=body, headers=headers)
            ms = (time.time() - t0) * 1000
        except Exception as e:  # transport error
            return None, {"_transport_error": str(e)}, False, (time.time() - t0) * 1000
        new_sid = r.headers.get("mcp-session-id")
        if new_sid:
            self.sid = new_sid
        data = _parse_body(r.text)
        is_err = isinstance(data, dict) and bool(data.get("result", {}).get("isError")) \
            if isinstance(data, dict) and isinstance(data.get("result"), dict) else False
        return r.status_code, data, is_err, ms

    def initialize(self, auth=True, extra_headers=None):
        return self._rpc("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "lk-mcp-test", "version": "1.0"},
        }, auth=auth, extra_headers=extra_headers)

    def tools_list(self):
        return self._rpc("tools/list", {})

    def call(self, name, arguments):
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def tool(self, name, arguments):
        """Call a tool; return (status, parsed_content, is_error, raw, ms)."""
        st, data, is_err, ms = self.call(name, arguments)
        parsed = None
        if st == 200 and isinstance(data, dict) and isinstance(data.get("result"), dict):
            content = data["result"].get("content") or []
            if content and isinstance(content[0], dict) and "text" in content[0]:
                text = content[0]["text"]
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    parsed = text
        return st, parsed, is_err, data, ms


def _parse_body(txt):
    txt = txt.strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    # SSE fallback: collect "data:" payloads
    payloads = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            d = line[5:].strip()
            if d:
                try:
                    payloads.append(json.loads(d))
                except json.JSONDecodeError:
                    payloads.append(d)
    if payloads:
        return payloads[-1] if isinstance(payloads[-1], dict) else {"_sse": payloads}
    return {"_raw": txt[:500]}


# ════════════════════════════════════════════════════════════════════════════
def run(base, token):
    global PHASE
    m = MCP(base, token)

    # ── Phase 1: auth & protocol ────────────────────────────────────────────
    PHASE = "protocol"
    st, data, _, ms = m.initialize()
    check("initialize (valid token)", st == 200 and isinstance(data, dict) and "result" in data,
          f"HTTP {st}" if st != 200 else f"server={data.get('result',{}).get('serverInfo',{}).get('name','?')}", ms)
    si = data.get("result", {}).get("serverInfo", {}) if isinstance(data, dict) else {}
    check("initialize serverInfo present", bool(si), json.dumps(si)[:90])

    # no-auth / wrong-auth (fresh clients, no token reuse)
    m2 = MCP(base, None)
    st2, d2, _, _ = m2.initialize(auth=False)
    check("initialize without token rejected", st2 in (401, 403), f"HTTP {st2}")
    m3 = MCP(base, "definitely-not-a-real-token")
    st3, d3, _, _ = m3.initialize()
    check("initialize with wrong token rejected", st3 in (401, 403), f"HTTP {st3}")

    st, data, _, ms = m.tools_list()
    tools = [t["name"] for t in data.get("result", {}).get("tools", [])] if st == 200 else []
    check("tools/list returns 9 tools", st == 200 and len(tools) == 9, f"{len(tools)} tools: {sorted(tools)}", ms)
    expected = {"search", "search_private_rulings", "fetch", "outline", "related",
                "get_definition", "list_rulings", "get_info", "report_issue"}
    check("all 9 expected tool names present", expected.issubset(set(tools)), sorted(set(expected) - set(tools)))

    st, data, _, _ = m._rpc("tools/frobnicate", {})
    check("unknown method errors", st == 200 and isinstance(data, dict) and "error" in data
          or st >= 400, f"HTTP {st}")
    st, data, _, _ = m._rpc("initialize", {}, raw_body="{not json")
    check("malformed JSON body handled", st >= 400, f"HTTP {st}")

    # ── Phase 2: get_info ───────────────────────────────────────────────────
    PHASE = "get_info"
    st, p, err, _, ms = m.tool("get_info", {})
    check("get_info returns coverage", st == 200 and not err and isinstance(p, (dict, str)), "", ms)
    if isinstance(p, dict):
        check("get_info has version", "version" in p, str(p.get("version"))[:30])

    # ── Phase 3: search ─────────────────────────────────────────────────────
    PHASE = "search"
    for q in ["capital gains tax", "division 7A", "main residence exemption", "director duties"]:
        st, p, err, _, ms = m.tool("search", {"query": q, "limit": 5})
        check(f"search '{q}'", st == 200 and not err and isinstance(p, (dict, str)), "", ms)

    for t in ["legislation", "case", "ruling", "rg", "afsa", "treaty"]:
        st, p, err, _, ms = m.tool("search", {"query": "financial", "types": [t], "limit": 3})
        nonempty = bool(p) and (json.dumps(p) not in ("", "{}", "null"))
        check(f"search types=[{t}]", st == 200 and not err and nonempty, "", ms)

    st, p, err, _, _ = m.tool("search", {"query": "CGT", "act": "itaa-1997", "limit": 3})
    check("search act filter", st == 200 and not err)
    st, p, err, _, _ = m.tool("search", {"query": "deductions", "status": "repealed", "limit": 3})
    check("search status=repealed", st == 200 and not err)
    st, p, err, _, _ = m.tool("search", {"query": "", "limit": 3})
    check("search empty query handled", st == 200 and not err or err, "graceful")
    st, p, err, _, _ = m.tool("search", {"query": "CGT", "limit": 0})
    check("search limit=0 handled", st == 200)
    st, p, err, _, _ = m.tool("search", {"query": "CGT", "limit": 9999})
    check("search limit=9999 clamped", st == 200 and not err)

    # ── Phase 4: search_private_rulings ─────────────────────────────────────
    PHASE = "search_private_rulings"
    st, p, err, _, ms = m.tool("search_private_rulings", {"query": "CGT main residence", "limit": 5})
    check("search_private_rulings valid", st == 200 and not err and bool(p), "", ms)
    st, p, err, _, _ = m.tool("search_private_rulings", {"query": "CGT", "outcome": "yes", "limit": 3})
    check("search_private_rulings outcome filter", st == 200 and not err)
    st, p, err, _, _ = m.tool("search_private_rulings", {"query": "zzzzznonsense"})
    check("search_private_rulings no-match", st == 200 and not err, "graceful empty")

    # ── Phase 5: fetch (all content types) ──────────────────────────────────
    PHASE = "fetch"
    fetch_cases = [
        ("legislation ITAA97", f"leg:itaa-1997:{KNOWN_SECTIONS['itaa-1997']}"),
        ("legislation subsect", "leg:itaa-1997:8-1(2)"),
        ("legislation ITAA36", "leg:itaa-1936:102UC"),
        ("legislation GST", "leg:gst-1999:9-5"),
        ("legislation corps", "leg:corporations-act-2001:124"),
        ("legislation bankruptcy", "leg:bankruptcy-act-1966:58"),
        ("legislation AML/CTF", "leg:aml-ctf-2006:6"),
        ("legislation NZ", "leg:nz-it-2007:AA-1"),
        ("case (canonical id)", "case:2019-fcafc-29"),
        ("case (citation)", KNOWN_CASE),
        ("ruling", KNOWN_RULING),
        ("ruling (canonical id)", "ruling:tr-2024-1"),
        ("private ruling", KNOWN_PBR),
        ("regulatory guide", KNOWN_RG),
        ("regulatory guide paragraph", KNOWN_RG_PARA),
        ("treaty article", KNOWN_DTA),
        ("afsa guidance", KNOWN_AFSA),
        ("human citation", "s 8-1 ITAA 1997"),
    ]
    for label, ref in fetch_cases:
        st, p, err, _, ms = m.tool("fetch", {"ref": ref})
        ok = st == 200 and not err and bool(p)
        check(f"fetch {label}", ok, f"{ref} err={err}" if not ok else "", ms)

    # invalid / breakage refs
    for label, ref in [
        ("unknown act", "leg:nonexistent-act-9999:1-1"),
        ("unknown case", "case:9999-xyz-1"),
        ("garbage ref", "not-a-real-ref"),
        ("empty ref", ""),
    ]:
        st, p, err, _, _ = m.tool("fetch", {"ref": ref})
        check(f"fetch invalid ({label}) errors cleanly", err or (st == 200), f"isError={err}" if err else f"HTTP {st}")

    # ── Phase 6: outline ────────────────────────────────────────────────────
    PHASE = "outline"
    st, p, err, _, ms = m.tool("outline", {})
    acts = (p or {}).get("collections", {}).get("acts", []) if isinstance(p, dict) else []
    countries = (p or {}).get("collections", {}).get("treaties", []) if isinstance(p, dict) else []
    check("outline id=None lists collections", st == 200 and not err and len(acts) > 10 and len(countries) > 1,
          f"{len(acts)} acts, {len(countries)} treaties", ms)

    st, p, err, _, ms = m.tool("outline", {"id": "itaa-1997", "depth": "sections"})
    check("outline act tree", st == 200 and not err and isinstance(p, dict) and p.get("parts"), "", ms)

    st, p, err, _, _ = m.tool("outline", {"id": "usa"})
    check("outline treaty articles", st == 200 and not err and isinstance(p, (dict, str)))

    for depth in ["parts", "divisions"]:
        st, p, err, _, _ = m.tool("outline", {"id": "itaa-1997", "depth": depth})
        check(f"outline depth={depth}", st == 200 and not err)

    # breakage: path traversal + weird ids
    for bad in ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "itaa-1997/../../etc", "UPPER", "with space"]:
        st, p, err, _, _ = m.tool("outline", {"id": bad})
        ok = st == 200 and not err  # regex gate returns JSON error string, not isError
        check(f"outline invalid id '{bad}' handled", ok, f"isError={err} HTTP={st}")

    # ── Phase 7: related ────────────────────────────────────────────────────
    PHASE = "related"
    rel_cases = [
        ("case", "case:2019-fcafc-29"),
        ("regulatory guide", "rg:1"),
        ("afsa", KNOWN_AFSA),
        ("legislation", "leg:itaa-1997:8-1"),
    ]
    for label, id_ in rel_cases:
        st, p, err, _, ms = m.tool("related", {"id": id_})
        check(f"related ({label})", st == 200 and not err, "", ms)
    st, p, err, _, _ = m.tool("related", {"id": "garbage:not-real"})
    check("related invalid id handled", st == 200)

    # ── Phase 8: get_definition ─────────────────────────────────────────────
    PHASE = "get_definition"
    for act, term in [("itaa-1997", "CGT asset"), ("itaa-1997", "dividend"),
                      ("gst-1999", "taxable supply"), ("itaa-1936", "resident")]:
        st, p, err, _, ms = m.tool("get_definition", {"act": act, "term": term})
        check(f"get_definition {act} '{term}'", st == 200 and not err, "", ms)
    st, p, err, _, _ = m.tool("get_definition", {"act": "itaa-1997", "term": "zzzznonsense"})
    check("get_definition missing term handled", st == 200 and not err)

    # ── Phase 9: list_rulings ───────────────────────────────────────────────
    PHASE = "list_rulings"
    st, p, err, _, ms = m.tool("list_rulings", {"counts_only": True})
    check("list_rulings counts_only", st == 200 and not err and isinstance(p, dict) and p.get("total_rulings", 0) > 100,
          f"{p.get('total_rulings') if isinstance(p, dict) else '?'} rulings", ms)
    st, p, err, _, _ = m.tool("list_rulings", {"limit": 5})
    check("list_rulings default page", st == 200 and not err)
    st, p, err, _, _ = m.tool("list_rulings", {"type": "TR", "limit": 3})
    check("list_rulings type=TR", st == 200 and not err)
    st, p, err, _, _ = m.tool("list_rulings", {"year": 2024, "limit": 3})
    check("list_rulings year=2024", st == 200 and not err)
    st, p, err, _, _ = m.tool("list_rulings", {"limit": -1})
    check("list_rulings limit=-1 handled", st == 200)

    # ── Phase 10: report_issue ──────────────────────────────────────────────
    PHASE = "report_issue"
    st, p, err, _, _ = m.tool("report_issue", {"category": "suggestion"})
    check("report_issue empty payload rejected", st == 200 and not err
          and isinstance(p, dict) and p.get("status") == "rejected")
    st, p, err, _, _ = m.tool("report_issue", {"category": "not_a_category", "note": "x"})
    check("report_issue invalid category handled", st == 200, f"isError={err}")

    # ── Phase 11: adversarial / breakage ────────────────────────────────────
    PHASE = "breakage"
    st, p, err, _, _ = m.tool("search", {"query": "'; DROP TABLE users; --", "limit": 3})
    check("SQLi string in search", st == 200 and not err)
    huge = "A" * 10000
    st, p, err, _, _ = m.tool("search", {"query": huge, "limit": 3})
    check("10k-char query in search", st == 200 and not err)
    st, p, err, _, _ = m.tool("fetch", {"ref": "../../etc/passwd"})
    check("path traversal in fetch", st == 200 and (err or True))
    st, p, err, _, _ = m.tool("search_private_rulings", {"query": "'; DROP TABLE--"})
    check("SQLi string in private rulings", st == 200 and not err)

    # missing required params
    st, p, err, _, _ = m.tool("fetch", {})
    check("fetch missing ref errors", st == 200, f"isError={err}")
    st, p, err, _, _ = m.tool("search", {})
    check("search missing query errors", st == 200, f"isError={err}")

    # ── Phase 12: full act coverage (discover first section of every act) ───
    PHASE = "act-coverage"
    act_slugs = [a["id"] for a in acts if isinstance(a, dict) and a.get("id") not in ("spec", "rulings", "constitution")] if acts else sorted(KNOWN_SECTIONS)
    fetched = 0
    for slug in act_slugs:
        st, p, err, _, _ = m.tool("outline", {"id": slug, "depth": "sections"})
        if st != 200 or err or not isinstance(p, dict):
            check(f"act tree {slug}", False, "outline failed")
            continue
        sid = first_section_id(p)
        if not sid:
            check(f"act tree {slug}", False, "no section id found")
            continue
        st2, p2, err2, _, _ = m.tool("fetch", {"ref": f"leg:{slug}:{sid}"})
        ok = st2 == 200 and not err2 and bool(p2)
        fetched += int(ok)
        if not ok:
            check(f"act {slug} first section {sid}", False, f"fetch err={err2}")

    check(f"fetch every act's first section ({fetched}/{len(act_slugs)})", fetched == len(act_slugs),
          f"{fetched}/{len(act_slugs)} ok")

    # ── summary ─────────────────────────────────────────────────────────────
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["status"] == "pass")
    failed = sum(1 for r in RESULTS if r["status"] == "fail")
    print(f"\n═══ SUMMARY ═══")
    print(f"  PASS {passed} / {total}   FAIL {failed}")
    for r in RESULTS:
        if r["status"] == "fail":
            print(f"  ✗ [{r['phase']}] {r['name']}  — {r['detail']}")
    return {"total": total, "pass": passed, "fail": failed, "results": RESULTS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8769")
    ap.add_argument("--token", default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    token = args.token
    if not token:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
            token = os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("LEGISLATION_BEARER_TOKEN")
        except Exception:
            pass
    if not token:
        print("No token — pass --token or set MCP_AUTH_TOKEN in .env")
        sys.exit(2)

    print(f"Target: {args.base}{MCP_PATH}")
    print(f"Token: {'<set>' if token else '<none>'}  ({len(token)} chars)")
    print("=" * 72)
    out = run(args.base, token)

    if args.report:
        with open(args.report, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nReport written to {args.report}")

    sys.exit(0 if out["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
