#!/usr/bin/env python3
"""Generate summaries for a court, with slice support for parallel workers.
Usage: python3 batch_summaries.py <COURT> [slice_index total_slices] [--spot-check N]"""
import json, os, re, subprocess, sys, time, urllib.request, random
from pathlib import Path

OUT = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned"
SUMMARY_DIR = f"{OUT}/summaries"
os.makedirs(SUMMARY_DIR, exist_ok=True)

COURT = sys.argv[1] if len(sys.argv) > 1 else "FCA"
SLICE_IDX = int(sys.argv[2]) if len(sys.argv) > 2 else None
SLICE_TOTAL = int(sys.argv[3]) if len(sys.argv) > 3 else None
SPOT_CHECK = 50

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

LABEL = f"{COURT}"
if SLICE_IDX is not None:
    LABEL += f" [{SLICE_IDX}/{SLICE_TOTAL}]"

SUMMARY_PROMPT = """Read this Australian tax judgment and output a detailed structured summary as JSON.

Output valid JSON ONLY (no other text) with this exact structure:
{
  "citation": "[YYYY] COURT N",
  "case_name": "Full case name",
  "court": "Full court name",
  "facts": "2-4 sentences covering the factual background and procedural history",
  "issues": ["Legal question 1", "Legal question 2"],
  "held": "What the court decided in 2-3 sentences",
  "reasoning": "4-8 sentences explaining the court's reasoning",
  "outcome": "What orders were made",
  "cases_cited": ["[YYYY] COURT N"],
  "legislation_cited": ["Act Name YYYY (Cth) s X"]
}

RULES:
- facts: include the dispute, key facts, who sued whom
- issues: precise legal questions
- held: direct answer to each issue
- reasoning: explain WHY the court decided each issue
- outcome: what orders were made
- case_name: the full case name
- court: the full court name
- cases_cited: extract ALL Australian case citations
- legislation_cited: include the FULL act name with jurisdiction
- If document has NO judgment text, return {"error": "empty document"}
- Always include citation, case_name, court fields
DOCUMENT:
"""

def api_call(prompt, max_tokens=4000):
    payload = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1, "max_tokens": max_tokens
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}",
                 "HTTP-Referer": "https://legislation-explorer.local", "X-Title": "Legislation Explorer"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]

# Build SQL with proper quoting for PostgreSQL regex
sql = (
    "SELECT c.citation, c.case_name "
    "FROM cases c "
    f"WHERE c.court='{COURT}' "
    "AND c.citation ~ '^\\[' "
    "AND EXISTS (SELECT 1 FROM case_paragraphs cp WHERE cp.case_id = c.id LIMIT 1) "
    "ORDER BY substring(c.citation FROM '\\[(\\d{4})\\]')::int ASC, "
    "substring(c.citation FROM '[A-Z]+ (\\d+)$')::int ASC;"
)

print(f"[{LABEL}] Fetching {COURT} cases from DB...")
r = subprocess.run(
    ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge", "-tA", "-c", sql],
    capture_output=True, text=True, timeout=30
)

all_cases = []
for line in r.stdout.strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('|', 1)
    all_cases.append({"citation": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})

print(f"[{LABEL}] Found {len(all_cases)} total in DB")

to_do = []
for case in all_cases:
    safe = case["citation"].replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
    fpath = f"{SUMMARY_DIR}/{safe}.json"
    if os.path.exists(fpath):
        with open(fpath) as fh:
            try:
                j = json.load(fh)
                if j.get("_meta") and not j.get("error"):
                    continue
            except:
                pass
    to_do.append(case)

if SLICE_IDX is not None and SLICE_TOTAL is not None:
    slice_size = len(to_do) // SLICE_TOTAL
    remainder = len(to_do) % SLICE_TOTAL
    start = (SLICE_IDX - 1) * slice_size + min(SLICE_IDX - 1, remainder)
    end = start + slice_size + (1 if SLICE_IDX - 1 < remainder else 0)
    to_do = to_do[start:end]
    print(f"[{LABEL}] Slice {SLICE_IDX}/{SLICE_TOTAL}: cases {start}-{end} ({len(to_do)} cases)")

print(f"[{LABEL}] To process: {len(to_do)} cases")

ok = 0
failed = 0
empty = 0
t_start = time.time()

for i, case in enumerate(to_do):
    citation = case["citation"]
    safe = citation.replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
    fpath = f"{SUMMARY_DIR}/{safe}.json"

    safe_db = citation.replace("'", "''")
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"SELECT json_build_object('citation',c.citation,'case_name',c.case_name,'court',c.court,'text',(SELECT string_agg(cp.content, E'\\n\\n' ORDER BY cp.sequence_order) FROM case_paragraphs cp WHERE cp.case_id=c.id)) FROM cases c WHERE c.citation='{safe_db}';"
    ], capture_output=True, text=True, timeout=60)

    if not r.stdout.strip():
        failed += 1
        print(f"[{LABEL}] [{i+1}/{len(to_do)}] {citation}: not in DB")
        continue

    try:
        data = json.loads(r.stdout.strip())
    except:
        failed += 1
        print(f"[{LABEL}] [{i+1}/{len(to_do)}] {citation}: DB parse error")
        continue

    text = data.get("text", "")
    if not text or len(text) < 100:
        empty += 1
        summary = {"citation": citation, "case_name": data.get("case_name",""), "court": data.get("court",""), "error": "empty document"}
        summary["_meta"] = {"source": citation, "text_size_kb": 0, "api_time_s": 0, "total_time_s": 0}
        with open(fpath, "w") as f: json.dump(summary, f, indent=2)
        print(f"[{LABEL}] [{i+1}/{len(to_do)}] {citation}: empty text ({len(text)} chars)")
        continue

    doc = text[:120000]
    text_kb = len(text) / 1024
    t0 = time.time()

    result = None
    for attempt in range(3):
        try:
            result = api_call(SUMMARY_PROMPT + doc)
            if result: break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(30 * (2 ** attempt), 120)
                print(f"[{LABEL}] [{i+1}/{len(to_do)}] Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            elif attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            else:
                failed += 1
                print(f"[{LABEL}] [{i+1}/{len(to_do)}] {citation}: HTTP {e.code}")
                break
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            failed += 1
            print(f"[{LABEL}] [{i+1}/{len(to_do)}] {citation}: API failed: {e}")
            break

    if not result:
        continue

    api_time = time.time() - t0
    cleaned = result.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        summary = json.loads(cleaned)
    except:
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            try:
                summary = json.loads(m.group())
            except:
                summary = {"citation": citation, "error": "no JSON found", "raw": result[:500]}
        else:
            summary = {"citation": citation, "error": "no JSON found", "raw": result[:500]}

    summary["_meta"] = {"source": citation, "text_size_kb": round(text_kb), "api_time_s": round(api_time), "total_time_s": round(time.time() - t0)}

    with open(fpath, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    ok += 1
    is_good = bool(summary.get("facts")) and not summary.get("error")

    if (i + 1) % 10 == 0:
        elapsed = time.time() - t_start
        rate = (i + 1) / elapsed
        eta = (len(to_do) - i - 1) / rate if rate > 0 else 0
        print(f"[{LABEL}] [{i+1}/{len(to_do)}] {ok} OK {failed} FAIL {empty} EMPTY | {rate:.1f}/min | ETA {eta/3600:.1f}h")

    if SPOT_CHECK > 0 and (i + 1) % SPOT_CHECK == 0 and is_good:
        facts_len = len(summary.get("facts", ""))
        reasoning_len = len(summary.get("reasoning", ""))
        cases_n = len(summary.get("cases_cited", []))
        leg_n = len(summary.get("legislation_cited", []))
        issues = []
        if facts_len < 200: issues.append(f"short_facts({facts_len})")
        if reasoning_len < 400: issues.append(f"short_reasoning({reasoning_len})")
        status_str = "OK" if not issues else "|".join(issues)
        print(f"[{LABEL}] SPOT {citation}: facts={facts_len}c reasoning={reasoning_len}c cases={cases_n} leg={leg_n} [{status_str}]")

    time.sleep(1)

elapsed = time.time() - t_start
print(f"\n[{LABEL}] DONE in {elapsed/3600:.1f}h")
print(f"  {ok} success | {failed} failed | {empty} empty")