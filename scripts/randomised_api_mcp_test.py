"""Randomised audit — API level first, then MCP (Harry's process).

Seed, sample, checks, findings. Statuses:
  OK   — check passed
  FAIL — API/MCP contract violation (this LAYER is broken)
  FIND — data gap (layer behaves correctly, underlying data missing)
API phase gates the MCP phase on FAIL only; FINDs are recorded at both layers.
"""
import argparse, json, os, random, glob, sys, urllib.parse, unicodedata, re
import httpx
from datetime import date

os.chdir(f"{Path(__file__).resolve().parent.parent}")
BASE = "http://127.0.0.1:8765"
H = {"Authorization": "Bearer mcpLiv3", "Accept": "application/json"}

# Seed: explicit --seed for reproducibility; default = today (new seed per run,
# per the randomised-audit methodology — "100% pass is suspicious").
_ap = argparse.ArgumentParser(description="Randomised data audit (API first, then MCP).")
_ap.add_argument("--seed", type=int, default=int(date.today().strftime("%Y%m%d")))
_args = _ap.parse_args()
SEED = _args.seed
rng = random.Random(SEED)

results = []

def norm(s):
    return unicodedata.normalize("NFKC", str(s)).strip()

def rec(phase, check, status, detail=""):
    results.append((phase, check, status, detail))
    print(f"[{status:4s}] {phase:4s} {check} {detail}")

def api(path, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    r = httpx.get(url, headers=H, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None

def tree_comp(act):
    try:
        return json.load(open(f"data/{act}/tree.json")).get("compilation_no")
    except Exception:
        return None

def tree_leaves(act):
    try:
        t = json.load(open(f"data/{act}/tree.json"))
    except Exception:
        return None
    n = 0
    def walk(node):
        nonlocal n
        if isinstance(node, dict):
            n += len(node.get("sections", []))
            for d in node.get("divisions", []):
                walk(d)
            for sub in node.get("subdivisions", []):
                walk(sub)
        elif isinstance(node, list):
            for i in node:
                walk(i)
    for p in t.get("parts", []):
        walk(p)
    return n

# ══════════════ PHASE 1 — API LEVEL ══════════════
print("═══ PHASE 1: API LEVEL (seed=%d) ═══" % SEED)

rc, _ = api("/health")
rec("api", "health", "OK" if rc == 200 else "FAIL", f"status={rc}")

rc, acts = api("/api/acts")
ok = rc == 200 and isinstance(acts, list) and len(acts) > 5
rec("api", "acts", "OK" if ok else "FAIL", f"status={rc} n={len(acts) if isinstance(acts, list) else '?'}")
if ok:
    by_id = {a["id"]: a for a in acts}
    for aid, exp_comp in (("itaa-1997", 266), ("gst-1999", 96)):
        a = by_id.get(aid, {})
        rec("api", f"acts[{aid}].compilation_no", "OK" if a.get("compilation_no") == exp_comp else "FAIL",
            f"got={a.get('compilation_no')} expected={exp_comp}")

rc, t = api("/api/tree/itaa-1997")
exp_leaves = tree_leaves("itaa-1997")
rec("api", "tree itaa comp 266", "OK" if rc == 200 and isinstance(t, dict) and t.get("compilation_no") == 266 else "FAIL",
    f"status={rc} comp={t.get('compilation_no') if isinstance(t, dict) else '?'}")


def _count_paths(node) -> int:
    """Count .md leaves in a tree payload (served API response or tree.json - same shape)."""
    n = 0
    if isinstance(node, dict):
        p = node.get("path")
        if isinstance(p, str) and p.endswith(".md"):
            n += 1
        for v in node.values():
            n += _count_paths(v)
    elif isinstance(node, list):
        for v in node:
            n += _count_paths(v)
    return n


# This check used to compare tree.json's leaf count against a hardcoded 4648 and never consult the
# API at all, despite its name. That made it two failures in one: it could not catch production
# serving a stale tree (the regression it exists for), and it broke the moment the corpus legitimately
# changed size - reinstating s 900-5 took tree.json to 4,649 and the audit reported an API contract
# failure for a corpus that was correct. It now compares the served tree against tree.json, so it
# still fails if the two disagree, and no longer fails when they agree.
api_leaves = _count_paths(t) if rc == 200 else -1
rec("api", "api tree leaves==corpus tree.json leaves",
    "OK" if rc == 200 and api_leaves == exp_leaves else "FAIL",
    f"api={api_leaves} tree.json={exp_leaves}" + ("" if api_leaves == exp_leaves else " MISMATCH"))
for part, sids in (("3-1", ["102-6", "119-1", "115-102"]), ("2-10", ["40-291A"])):
    rc, t = api("/api/tree/itaa-1997", part=part, depth="sections")
    flat = json.dumps(t) if t else ""
    for sid in sids:
        rec("api", f"tree itaa part={part} has {sid}", "OK" if rc == 200 and sid in flat else "FAIL", f"status={rc}")

# random section sample (exclude spec fixture; canonical frontmatter ids)
acts_dirs = [d for d in os.listdir("data") if os.path.isdir(f"data/{d}/sections") and d != "spec"]
rng.shuffle(acts_dirs)
sample = []
for act in acts_dirs:
    files = glob.glob(f"data/{act}/sections/**/*.md", recursive=True)
    for p in rng.sample(files, min(3, len(files))):
        m = re.search(r"^section:\s*[\"']?([^\"'\n]+)", open(p, encoding="utf-8").read(), re.M)
        if m:
            sample.append((act, m.group(1).strip()))
targeted = [("itaa-1997", "102-6"), ("itaa-1997", "119-1"), ("gst-1999", "117-5"),
            ("gst-1999", "5-5"), ("itaa-1997", "1-1"), ("itaa-1997", "104-107A")]
sample = targeted + sample
print(f"(sampling {len(sample)} sections)")
for act, sec in sample:
    rc, d = api(f"/api/section/{act}/{urllib.parse.quote(sec)}")
    if rc != 200 or not isinstance(d, dict):
        rec("api", f"section {act}/{sec}", "FAIL", f"status={rc} body={str(d)[:60]}")
        continue
    fm, body = d.get("frontmatter", {}), d.get("body", "")
    ok = bool(body.strip()) and norm(fm.get("section")) == norm(sec)
    detail = f"body={len(body)}ch fm.section={fm.get('section')}"
    tcomp = tree_comp(act)
    mismatch = None
    if tcomp:
        fcomp = fm.get("compilation_no")
        if fcomp is not None and int(fcomp) != int(tcomp):
            ok = False
            mismatch = f" COMP-MISMATCH fm={fcomp} tree={tcomp}"
            detail += mismatch
    # data gaps (compilation mismatch) are FINDs — probed again at MCP layer,
    # not contract FAILs
    rec("api", f"section {act}/{sec}", "OK" if ok else ("FIND" if mismatch else "FAIL"), detail)

# definitions
for act, term, expect in (("itaa-1997", "retail fuel", "995-1"),
                          ("itaa-1997", "non-share distribution", "995-1"),
                          ("itaa-1936", "assessment", "6"),
                          ("gst-1999", "input tax credit", "195-1"),
                          ("itaa-1997", "subordinated debt interest", "995-1")):
    rc, d = api(f"/api/definition/{act}/{urllib.parse.quote(term)}")
    sec = norm((d or {}).get("section") or (d or {}).get("definition_section"))
    rec("api", f"definition {act} '{term}'", "OK" if rc == 200 and sec == expect else "FAIL",
        f"status={rc} section={sec}")

# search
rc, d = api("/api/search", q="104-107A")
top = None
if rc == 200 and isinstance(d, dict):
    hits = d.get("results") or d.get("hits") or []
    top = hits[0] if hits else None
rec("api", "search exact 104-107A", "OK" if rc == 200 and top and norm(top.get("section")) == "104-107A" else "FAIL",
    f"status={rc} top={top.get('section') if top else None}")
rc, d = api("/api/search", q="capital gains CGT events")
rec("api", "search capital gains", "OK" if rc == 200 and isinstance(d, dict) and bool((d.get("results") or d.get("hits") or [])) else "FAIL",
    f"status={rc}")

# cases — existing + known-missing probe
rc, d = api("/api/case/%5B2009%5D%20FCAFC%2029")
rec("api", "case [2009] FCAFC 29", "OK" if rc == 200 and isinstance(d, dict) and "body" in d else "FAIL", f"status={rc}")
rc, d = api("/api/case/%5B2019%5D%20FCAFC%2029")
rec("api", "case [2019] FCAFC 29 (Harding)", "FIND" if rc != 200 else "OK",
    f"status={rc} — data gap: no [2019]_FCAFC_29.json in CASE_DIR" if rc != 200 else "")

# rulings (tree-shaped) + private rulings
rc, d = api("/api/rulings-list", limit=3)
rul_cit = None
if rc == 200 and isinstance(d, dict):
    for part in d.get("parts") or []:
        for div in part.get("divisions", []):
            for s in div.get("sections", []):
                rul_cit = s.get("id")
                break
            if rul_cit:
                break
        if rul_cit:
            break
rec("api", "rulings-list", "OK" if rc == 200 and rul_cit else "FAIL", f"status={rc} first={rul_cit}")
if rul_cit:
    rc, d = api(f"/api/ruling/{urllib.parse.quote(rul_cit)}")
    rec("api", f"ruling {rul_cit}", "OK" if rc == 200 and isinstance(d, dict) else "FAIL", f"status={rc}")
rc, d = api("/api/private-rulings", limit=3)
prl = d.get("rulings") or d.get("items") or d.get("private_rulings") or [] if isinstance(d, dict) else []
auth = prl[0].get("authnum") if prl else None
rec("api", "private-rulings", "OK" if rc == 200 and bool(prl) else "FAIL", f"status={rc}")
if auth:
    rc, d = api(f"/api/private-ruling/{auth}")
    rec("api", f"private-ruling {auth}", "OK" if rc == 200 and isinstance(d, dict) else "FAIL", f"status={rc}")

# graph
rc, d = api("/api/graph/related", key="section:itaa-1997:8-1", limit=3)
rec("api", "graph related 8-1", "OK" if rc == 200 and isinstance(d, dict) and "groups" in d else "FAIL", f"status={rc}")

# ── corpus-wide scan dimensions (exhaustive, not sampled) ──
lig = [f for f in glob.glob("data/**/*.md", recursive=True)
       if any(ord(ch) > 127 for ch in os.path.basename(f))]
rec("api", "scan: non-ASCII filenames", "FIND" if lig else "OK", f"{len(lig)} files (master-tax-guide ligatures)" if lig else "0")
low = []
for f in glob.glob("data/*/sections/**/*.md", recursive=True):
    base = os.path.basename(f)[:-3]
    m = re.search(r"^section:\s*[\"']?([^\"'\n]+)", open(f, encoding="utf-8").read(), re.M)
    if not m:
        continue
    fm_id = norm(m.group(1))
    if base != fm_id and base.upper() == fm_id.upper():
        low.append((os.path.relpath(f, "data"), base, fm_id))
rec("api", "scan: filename-vs-frontmatter case mismatch", "FIND" if low else "OK",
    f"{len(low)} files: {low[:6]}" if low else "0")

fails_api = [r for r in results if r[0] == "api" and r[2] == "FAIL"]
finds_api = [r for r in results if r[0] == "api" and r[2] == "FIND"]
print(f"\n── API PHASE: {len(results)} checks | FAIL {len(fails_api)} | FIND {len(finds_api)}")

if fails_api:
    print("❌ STOP: API contract failures — not running MCP phase (per process).")
    sys.exit(1)

# ══════════════ PHASE 2 — MCP LEVEL ══════════════
print("\n═══ PHASE 2: MCP LEVEL ═══")

def mcp(name, arguments):
    r = httpx.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}, "id": 1}, headers=H, timeout=30)
    try:
        return r.json()["result"]["content"][0]["text"]
    except Exception:
        return None

def tools_list():
    r = httpx.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "method": "tools/list",
                   "params": {}, "id": 1}, headers=H, timeout=30)
    try:
        names = [t.get("name") for t in r.json()["result"]["tools"]]
        return names
    except Exception:
        return []

m = mcp("list_acts", {})
try:
    b = {a["id"]: a.get("compilation_no") for a in json.loads(m)["acts"]}
    rec("mcp", "list_acts itaa=266 gst=96", "OK" if b.get("itaa-1997") == 266 and b.get("gst-1999") == 96 else "FAIL",
        f"got={b.get('itaa-1997')}/{b.get('gst-1999')}")
except Exception as e:
    rec("mcp", "list_acts", "FAIL", str(e)[:80])

mismatch = 0
def strip_md(s):
    """Reduce raw markdown body to comparable text: drop anchors, links
    (keep inner text), footers, emphasis markers."""
    s = re.sub(r'<a id="[^"]*"\s*/?>', "", s)            # anchor open/self-close
    s = re.sub(r"</a>", "", s)                           # anchor close
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    s = re.sub(r"^---\s*$", "", s, flags=re.M)            # hr lines
    s = re.sub(r"^\*Last updated:.*$", "", s, flags=re.M) # footers (before emphasis!)
    s = re.sub(r"[*_]", "", s)                            # emphasis markers
    s = re.sub(r"\s+", " ", s)
    return norm(s)
for act, sec in targeted:
    m = mcp("get_section", {"act": act, "section": sec})
    api_rc, apid = api(f"/api/section/{act}/{urllib.parse.quote(sec)}")
    mbody = (json.loads(m).get("body", "") if m and m.startswith("{") else "")
    abody = (apid or {}).get("body", "") if api_rc == 200 else ""
    same = strip_md(mbody) == strip_md(abody)
    if not same:
        mismatch += 1
    rec("mcp", f"get_section {act}/{sec} == API (md-normalized)", "OK" if same else "FAIL",
        f"mcp={len(mbody)}ch api={len(abody)}ch")
rec("mcp", "API-MCP body consistency", "OK" if mismatch == 0 else "FAIL", f"{mismatch}/{len(targeted)} mismatched")

for act, term, expect in (("itaa-1997", "retail fuel", "995-1"), ("itaa-1936", "assessment", "6"), ("gst-1999", "input tax credit", "195-1")):
    m = mcp("get_definition", {"act": act, "term": term})
    sec = norm(json.loads(m).get("section")) if m and m.startswith("{") else None
    rec("mcp", f"get_definition {act} '{term}'", "OK" if sec == expect else "FAIL", f"section={sec}")

m = mcp("search_all", {"query": "104-107A"})
rec("mcp", "search_all 104-107A", "OK" if m and "104-107A" in m else "FAIL", (m or "")[:80])

m = mcp("get_case", {"citation": "[2009] FCAFC 29"})
rec("mcp", "get_case [2009] FCAFC 29", "OK" if m and ("FCAFC" in m or "[2009]" in m) else "FAIL", (m or "")[:80])
m = mcp("get_case", {"citation": "[2019] FCAFC 29"})
api_has = False
try:
    r = httpx.get(f"{BASE}/api/case/[2019] FCAFC 29", headers=H, timeout=60)
    api_has = r.status_code == 200 and len(r.json().get("body", "")) > 100
except Exception:
    pass
rec("mcp", "get_case [2019] FCAFC 29 (Harding)", "OK" if (m and "Harding" in m and api_has) else "FIND",
    "dual case-store split resolved" if (m and "Harding" in m and api_has) else
    f"MCP={'yes' if m and 'Harding' in m else 'no'} API_text={'yes' if api_has else 'no'}")

# rulings: F4 added the get_ruling-by-citation tool — verify it exists and works
tools = tools_list()
has_tool = "get_ruling" in tools
g = mcp("get_ruling", {"citation": "CR 2017/74"})
g_ok = bool(g) and "CR 2017/74" in g and "Public Rulings" in g
rec("mcp", "MCP get_ruling tool exists", "OK" if (has_tool and g_ok) else "FAIL",
    f"tools_list_has={'yes' if has_tool else 'no'} exercise={'ok' if g_ok else (g or '')[:60]}")
m = mcp("list_rulings", {})
ok = m and "ato_rulings_total" in m
rec("mcp", "list_rulings structure", "OK" if ok else "FAIL", (m or "")[:60])

m = mcp("get_act_tree", {"act": "itaa-1997", "depth": "sections", "part": "3-1"})
rec("mcp", "get_act_tree part 3-1 has 102-6/119-1", "OK" if m and "102-6" in m and "119-1" in m else "FAIL")

fails_mcp = [r for r in results if r[0] == "mcp" and r[2] == "FAIL"]
finds_mcp = [r for r in results if r[0] == "mcp" and r[2] == "FIND"]
print(f"\n── MCP PHASE: {sum(1 for r in results if r[0]=='mcp')} checks | FAIL {len(fails_mcp)} | FIND {len(finds_mcp)}")

# ── CONTENT PHASE: deep corpus correctness/formatting analysis ──
# Full-corpus scans for corruption classes (glued files, fragments, stray cut
# tokens, chapeau drops, formatting artifacts, broken definitions, case
# citations). Every class found is a FIND (data gap) -> synced as a ticket.
# The detector set is scan_corpus_error_classes.DETECTORS — ONE shared list, iterated here
# and by the scanner's own main() (S3, 2026-09-26). Before that this file kept its own
# 15-name hand copy, which had already drifted from main() in both directions.
import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location(
    "scan_corpus_error_classes",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_corpus_error_classes.py"))
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)
for _fn in sc.DETECTORS:
    getattr(sc, _fn)()
_by_class = {}
for _f in sc.findings:
    _by_class.setdefault(_f["class"], []).append(_f)
# Shapes a detector reports separately because they are measured to be correct, not damage.
_BENIGN_CLASSES = {"C11_table_rowwrap", "C11_table_header_split", "C14_fraction_rule"}
_skips = 0
for _cls, _items in sorted(_by_class.items()):
    _first = _items[0]
    _nfiles = len({_i["path"] for _i in _items})
    # A class ending :CONVENTION is a check declining to judge an act that does not follow the
    # convention the check relies on - not a defect. Same for the explicitly benign shapes a split
    # detector reports separately (C11_table_rowwrap: a row that wraps to the next line ends on a
    # connector by nature - measured, not assumed, by _continuation_after). Counting either as a FIND
    # inflated the nightly number and raised tickets for correct text. They report as SKIP, which
    # sync_issues() ignores, so the FIND count means "defects" and nothing else.
    _status = "SKIP" if (_cls.endswith(":CONVENTION") or _cls in _BENIGN_CLASSES) else "FIND"
    if _status == "SKIP":
        _skips += 1
    rec("content", f"corpus {_cls}", _status,
        f"{len(_items)} finding(s) across {_nfiles} file(s) e.g. {_first['path']}: {_first['detail'][:70]}")
print(f"\n── CONTENT PHASE: {len(_by_class)} classes | FAIL 0 | "
      f"FIND {len(_by_class) - _skips} | SKIP {_skips}")

fails_content = [r for r in results if r[0] == "content" and r[2] == "FAIL"]
finds_content = [r for r in results if r[0] == "content" and r[2] == "FIND"]
print(f"\n═══ TOTAL: {len(results)} checks | FAIL {len(fails_api + fails_mcp + fails_content)} | FIND {len(finds_api + finds_mcp + finds_content)} ═══")

# ── sync findings into the issues list (CDN tickets) ─────────────────────────
# Every run (manual or cron) upserts FAIL/FIND results into the issues portal:
# one open ticket per finding CLASS, no duplicates on repeat runs.
def _stable_key(phase, check, detail):
    m = re.match(r"section (\S+)/", check)
    if m and "COMP-MISMATCH" in detail:
        return f"section {m.group(1)} COMP-MISMATCH"
    return check

def sync_issues():
    problems = [r for r in results if r[2] in ("FAIL", "FIND")]
    if not problems:
        print("[issues] clean run — nothing to sync")
        return
    try:
        r = httpx.get(f"{BASE}/api/issues", headers=H, timeout=60)
        rows = r.json().get("issues", []) if r.status_code == 200 else []
    except Exception as e:
        print(f"[issues] list failed ({e}) — skipping sync")
        return
    # param_hash is not returned by the API; dedupe on (tool, params) of open/known rows
    tracked = {
        (x.get("tool"), x.get("params"))
        for x in rows if x.get("status") in ("open", "known") and str(x.get("tool", "")).startswith("audit")
    }
    created = skipped = 0
    for phase, check, status, detail in problems:
        params = _stable_key(phase, check, detail)
        tool = f"audit/{phase}"
        if (tool, params) in tracked:
            skipped += 1
            continue
        resp = httpx.post(
            f"{BASE}/api/issues", headers=H,
            json={
                "category": "bug",
                "tool": tool,
                "params": params,
                "expected": f"{status} not present (audit seed {SEED})",
                "actual": detail,
                "note": f"randomised audit seed {SEED}; {status} — {check}",
            },
            timeout=60,
        )
        if resp.status_code == 200:
            created += 1
            tracked.add((tool, params))
        else:
            print(f"[issues] create failed for '{params}': {resp.status_code} {resp.text[:150]}")
    print(f"[issues] synced: {created} new ticket(s), {skipped} already tracked")

sync_issues()
sys.exit(0 if not (fails_api or fails_mcp) else 2)
