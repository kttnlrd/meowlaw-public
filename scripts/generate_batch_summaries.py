#!/usr/bin/env python3
"""Batch-generate DeepSeek summaries for tax cases in PostgreSQL.
Usage: generate_batch_summaries.py COURT  (e.g. FCA, FCAFC, HCA)
Writes progress to batch_{court}_status.json. Skips cases where .json already exists."""
import json, subprocess, os, urllib.request, time, re, sys
from pathlib import Path

COURT = sys.argv[1].upper() if len(sys.argv) > 1 else "HCA"

OUT = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned"
STATUS_FILE = f"{OUT}/batch_{COURT}_status.json"
os.makedirs(f"{OUT}/summaries", exist_ok=True)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

# ── Load/resume progress ──────────────────────────────────
def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"court": COURT, "processed": 0, "success": 0, "failed": 0, "errors": [], "started_at": None}

def save_status(s):
    with open(STATUS_FILE, "w") as f:
        json.dump(s, f, indent=2)

# ── Fetch cases from DB ───────────────────────────────────
def fetch_all_cases():
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"""
SELECT c.citation, c.case_name
FROM cases c
WHERE c.court='{COURT}'
  AND c.citation ~ '^\\['
  AND EXISTS (SELECT 1 FROM case_paragraphs cp WHERE cp.case_id = c.id LIMIT 1)
ORDER BY
  substring(c.citation FROM '\\[(\\d{{4}})\\]+')::int ASC,
  substring(c.citation FROM '{COURT} (\\d+)$')::int ASC
"""
    ], capture_output=True, text=True, timeout=30)
    cases = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 1)
        cases.append({"citation": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    return cases

# ── API call ───────────────────────────────────────────────
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

SUMMARY_PROMPT = """Read this Australian tax judgment and output a detailed structured summary as JSON. Be thorough — the summary should be enough to understand the entire case without reading the judgment.

Output valid JSON ONLY (no other text) with this exact structure:
{
  "citation": "[YYYY] COURT N",
  "case_name": "Full case name",
  "court": "Court name",
  "judges": ["Judge 1", "Judge 2"],
  "date": "Decision date",
  "facts": "2-4 sentences covering the factual background and procedural history",
  "issues": ["Legal question 1", "Legal question 2"],
  "held": "What the court decided in 2-3 sentences",
  "reasoning": "4-8 sentences explaining the court's reasoning per issue, including key legal principles applied",
  "outcome": "What orders were made (appeal allowed/dismissed, etc.)",
  "cases_cited": ["[YYYY] COURT N", "[YYYY] COURT N"],
  "legislation_cited": ["Act name section", "Act name section"]
}

RULES:
- facts: include the dispute, key facts, who sued whom, procedural history
- issues: precise legal questions the court had to answer
- held: the direct answer to each issue
- reasoning: explain WHY the court decided each issue the way it did, including any tests or principles applied
- outcome: what orders were made
- cases_cited: extract ALL Australian case citations you find in the judgment (format: [YYYY] COURT N or [YYYY] HCA N)
- legislation_cited: extract ALL legislation references (include section numbers and Act names)
- If the document has NO actual judgment text (just metadata), return {"error": "empty document"}

DOCUMENT:
"""

def fetch_case_text(citation):
    safe = citation.replace("'", "''")
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"SELECT json_build_object('citation',c.citation,'case_name',c.case_name,'court',c.court,'judges',c.judges,'decision_date',c.decision_date,'outcome',c.outcome,'related_provisions',c.related_provisions,'related_rulings',c.related_rulings, 'text', (SELECT string_agg(cp.content, E'\\\\n\\\\n' ORDER BY cp.sequence_order) FROM case_paragraphs cp WHERE cp.case_id=c.id)) FROM cases c WHERE c.citation='{safe}';"
    ], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout.strip())

def extract_cases_regex(text):
    return list(set(re.findall(r'\[\d{4}\]\s*[A-Z]+\s*\d+', text)))

def extract_legislation_regex(text):
    refs = set()
    for m in re.finditer(r'(?:^|\s)(?:s|section|subsection|paragraph|subparagraph|Division|Part|Schedule)\s+(\d+[A-Za-z]?(?:\([^)]*\))?)', text):
        refs.add(m.group(0).strip())
    for m in re.finditer(r'([A-Z][A-Za-z ]*(?:Act|Regulation|Rules|Ordinance|By-law)\s*(?:\(Cth\)|\(NSW\)|\(Vic\)|\(Qld\)|\(SA\)|\(WA\)|\(Tas\)|\(ACT\)|\(NT\))?\s*\d{4})', text):
        refs.add(m.group(0).strip())
    return sorted(refs)

def process(case, status):
    citation = case["citation"]
    safe_name = citation.replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
    out_json = f"{OUT}/summaries/{safe_name}.json"

    # Skip if already done
    if os.path.exists(out_json):
        with open(out_json) as f:
            try:
                j = json.load(f)
                if j.get("_meta"):
                    return "SKIP"
            except:
                pass

    t0 = time.time()

    try:
        data = fetch_case_text(citation)
    except Exception as e:
        status["failed"] += 1
        status["errors"].append(f"{citation}: DB fetch failed: {e}")
        return "FAIL_DB"

    full_text = data.get("text", "")
    if not full_text or len(full_text) < 100:
        status["failed"] += 1
        status["errors"].append(f"{citation}: empty text ({len(full_text)} chars)")
        return "FAIL_EMPTY"

    text_kb = len(full_text) / 1024
    doc = full_text[:120000]

    for attempt in range(3):
        try:
            t1 = time.time()
            result = api_call(SUMMARY_PROMPT + doc, max_tokens=4000)
            if result is None:
                raise ValueError("API returned null content")
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            status["failed"] += 1
            status["errors"].append(f"{citation}: API failed after 3 attempts: {e}")
            return "FAIL_API"
    else:
        # Should not be reached, but safety net
        status["failed"] += 1
        status["errors"].append(f"{citation}: API failed — exhausted retries")
        return "FAIL_API"

    api_time = time.time() - t1

    cleaned = result.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        summary = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m:
            try:
                summary = json.loads(m.group())
            except:
                summary = {"error": "failed to parse", "raw": result[:500]}
        else:
            summary = {"error": "no JSON found", "raw": result[:500]}

    summary["_meta"] = {
        "source": citation,
        "text_size_kb": round(text_kb),
        "api_time_s": round(api_time),
        "total_time_s": round(time.time() - t0)
    }

    regex_cases = extract_cases_regex(full_text)
    regex_leg = extract_legislation_regex(full_text)
    if not summary.get("cases_cited"):
        summary["cases_cited"] = regex_cases
    if not summary.get("legislation_cited"):
        summary["legislation_cited"] = regex_leg

    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    status["success"] += 1
    cases_n = len(summary.get("cases_cited", []))
    leg_n = len(summary.get("legislation_cited", []))
    print(f"  ✅ {citation}: {text_kb:.0f}KB → {api_time:.0f}s, {cases_n}c, {leg_n}l", flush=True)
    return "OK"

# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    status = load_status()
    if status["started_at"] is None:
        status["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_status(status)

    cases = fetch_all_cases()
    total = len(cases)
    already_done = sum(1 for c in cases if os.path.exists(
        f"{OUT}/summaries/{c['citation'].replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')}.json"
    ) and (lambda f: True) and True)

    print(f"📊 {total} total {COURT} cases in DB, {already_done} already processed", flush=True)
    print(f"   {'RESUMING' if status['processed'] > 0 else 'STARTING FRESH'}", flush=True)

    last_percent = 0
    batch_start = time.time()

    for i, case in enumerate(cases):
        result = process(case, status)
        if result == "SKIP":
            continue

        status["processed"] += 1

        # Save status every 10 cases
        if status["processed"] % 10 == 0:
            save_status(status)

        # Print aggregate every 10 cases
        if status["processed"] % 10 == 0 or status["processed"] == 1:
            pct = (status["processed"] / total) * 100
            elapsed = time.time() - batch_start
            rate = status["processed"] / elapsed if elapsed > 0 else 0
            eta = (total - status["processed"]) / rate if rate > 0 else 0
            print(f"\n📈 {status['processed']}/{total} ({pct:.0f}%) | ✅{status['success']} ❌{status['failed']} | {eta/60:.0f}m remaining", flush=True)

        # Progress milestone tracking for the monitoring cron
        new_pct = int((status["processed"] / total) * 100)
        if new_pct > last_percent + 9:
            last_percent = new_pct
            save_status(status)

    save_status(status)
    print(f"\n🏁 DONE: {status['success']} success, {status['failed']} failed in {time.time() - batch_start:.0f}s", flush=True)
