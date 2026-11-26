#!/usr/bin/env python3
"""Batch-generate summaries for all remaining tax courts (AATA, FCA, FCAFC).
Processes court by court. Logs progress and spot-checks every 50 cases."""
import json, subprocess, os, urllib.request, time, re, sys, random
from pathlib import Path

OUT = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned"
STATUS_FILE = f"{OUT}/batch_all_status.json"
os.makedirs(f"{OUT}/summaries", exist_ok=True)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

SPOT_CHECK_LOG = f"{OUT}/spot_check_log.md"

# Courts to process (in order)
COURTS = ["AATA", "FCA", "FCAFC"]

def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"processed": 0, "success": 0, "failed": 0, "empties": 0, "errors": [],
            "started_at": None, "current_court": None, "last_spot_check_at": 0}

def save_status(s):
    with open(STATUS_FILE, "w") as f:
        json.dump(s, f, indent=2)

def log_spot_check(citation, summary, is_ok, issues=""):
    entry = {
        "citation": citation,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": is_ok,
        "issues": issues,
        "facts_len": len(summary.get("facts", "")),
        "reasoning_len": len(summary.get("reasoning", "")),
        "cases_n": len(summary.get("cases_cited", [])),
        "leg_n": len(summary.get("legislation_cited", [])),
    }
    with open(SPOT_CHECK_LOG, "a") as f:
        f.write(f"\n### {citation} ({entry['timestamp']})\n")
        f.write(f"- OK: {is_ok} | Facts: {entry['facts_len']}c | Reasoning: {entry['reasoning_len']}c | Cases: {entry['cases_n']} | Leg: {entry['leg_n']}\n")
        if not is_ok:
            f.write(f"- Issues: {issues}\n")
    return entry

def fetch_cases_for_court(court):
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"""
SELECT c.citation, c.case_name
FROM cases c
WHERE c.court='{court}'
  AND c.citation ~ '^\\['
  AND EXISTS (SELECT 1 FROM case_paragraphs cp WHERE cp.case_id = c.id LIMIT 1)
ORDER BY
  substring(c.citation FROM '\\[(\\d{{4}})\\]+')::int ASC,
  substring(c.citation FROM '[A-Z]+ (\\d+)$')::int ASC
"""
    ], capture_output=True, text=True, timeout=30)
    cases = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 1)
        cases.append({"citation": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    return cases

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

SUMMARY_PROMPT = """Read this Australian tax judgment and output a detailed structured summary as JSON.

Output valid JSON ONLY (no other text) with this exact structure:
{
  "citation": "[YYYY] COURT N",
  "facts": "2-4 sentences covering the factual background and procedural history",
  "issues": ["Legal question 1", "Legal question 2"],
  "held": "What the court decided in 2-3 sentences",
  "reasoning": "4-8 sentences explaining the court's reasoning",
  "outcome": "What orders were made",
  "cases_cited": ["[YYYY] COURT N"],
  "legislation_cited": ["Act name section"]
}

RULES:
- facts: include the dispute, key facts, who sued whom
- issues: precise legal questions
- held: direct answer to each issue
- reasoning: explain WHY the court decided each issue
- outcome: what orders were made
- cases_cited: extract ALL Australian case citations
- legislation_cited: extract ALL legislation references
- If document has NO actual judgment text, return {"error": "empty document"}
- Always include the citation field

DOCUMENT:
"""

def fetch_case_text(citation):
    safe = citation.replace("'", "''")
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"SELECT json_build_object('citation',c.citation,'case_name',c.case_name,'court',c.court,'text', (SELECT string_agg(cp.content, E'\\\\n\\\\n' ORDER BY cp.sequence_order) FROM case_paragraphs cp WHERE cp.case_id=c.id)) FROM cases c WHERE c.citation='{safe}';"
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
        status["empties"] += 1
        status["errors"].append(f"{citation}: empty text ({len(full_text)} chars)")
        # Still save minimal JSON with citation
        summary = {"citation": citation, "error": "empty document"}
        with open(out_json, "w") as f:
            json.dump(summary, f, indent=2)
        return "EMPTY"

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
        status["failed"] += 1
        status["errors"].append(f"{citation}: API exhausted retries")
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
                summary = {"citation": citation, "error": "parse failed", "raw": result[:500]}
        else:
            summary = {"citation": citation, "error": "no JSON found", "raw": result[:500]}

    summary["_meta"] = {
        "source": citation,
        "text_size_kb": round(text_kb),
        "api_time_s": round(api_time),
        "total_time_s": round(time.time() - t0)
    }

    # Fallback regex for cases/legislation
    regex_cases = extract_cases_regex(full_text)
    regex_leg = extract_legislation_regex(full_text)
    if not summary.get("cases_cited"):
        summary["cases_cited"] = regex_cases
    if not summary.get("legislation_cited"):
        summary["legislation_cited"] = regex_leg

    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    status["success"] += 1
    return "OK"

if __name__ == "__main__":
    status = load_status()
    if status["started_at"] is None:
        status["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_status(status)

    if not os.path.exists(SPOT_CHECK_LOG):
        with open(SPOT_CHECK_LOG, "w") as f:
            f.write("# Spot Check Log — All Courts\n\n")

    batch_start = time.time()

    # Calculate grand total across all courts
    grand_total = 0
    for court in COURTS:
        gc = fetch_cases_for_court(court)
        grand_total += len(gc)

    print(f"📊 Total remaining: {grand_total} cases across {len(COURTS)} courts", flush=True)
    grand_total = 0
    grand_processed = status["processed"]

    for court in COURTS:
        if status.get("current_court") and status["current_court"] != court and status["current_court"] in COURTS:
            if COURTS.index(status["current_court"]) < COURTS.index(court):
                # Skip courts already completed
                continue

        status["current_court"] = court
        save_status(status)

        cases = fetch_cases_for_court(court)
        court_total = len(cases)

        # Count already done
        already = sum(1 for c in cases if os.path.exists(
            f"{OUT}/summaries/{c['citation'].replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')}.json"
        ))

        print(f"\n{'='*60}", flush=True)
        print(f"📋 {court}: {court_total} total, {already} already done", flush=True)
        print(f"{'='*60}", flush=True)

        court_processed = 0
        court_success = 0

        for i, case in enumerate(cases):
            result = process(case, status)
            if result == "SKIP":
                court_processed += 1
                continue

            status["processed"] += 1
            court_processed += 1
            if result == "OK":
                court_success += 1

            # Spot check every 50 cases
            if court_processed > 0 and court_processed % 50 == 0:
                # Pick 1-2 random cases from the last 50
                batch_start_idx = max(0, i - 49)
                batch = cases[batch_start_idx:i+1]
                samples = random.sample(batch, min(2, len(batch)))
                for sample in samples:
                    safe = sample["citation"].replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
                    spath = f"{OUT}/summaries/{safe}.json"
                    if os.path.exists(spath):
                        with open(spath) as sf:
                            sd = json.load(sf)
                        facts_ok = bool(sd.get("facts"))
                        held_ok = bool(sd.get("held"))
                        is_ok = facts_ok and held_ok and not sd.get("error")
                        log_spot_check(sample["citation"], sd, is_ok,
                                       "" if is_ok else f"facts={facts_ok}, held={held_ok}")

            # Save status periodically
            if status["processed"] % 10 == 0:
                save_status(status)

            # Progress
            if court_processed % 10 == 0 or court_processed == 1:
                elapsed = time.time() - batch_start
                rate = status["processed"] / elapsed if elapsed > 0 else 0
                remaining = (grand_total or 1) - status["processed"]
                eta = remaining / rate if rate > 0 else 0
                total_cases = court_total
                pct = (court_processed / total_cases * 100) if total_cases > 0 else 0
                print(f"  📈 [{court}] {court_processed}/{total_cases} ({pct:.0f}%) | ✅{status['success']} ⚠️{status['empties']} ❌{status['failed']} | ETA {eta/3600:.1f}h", flush=True)

        print(f"  ✅ {court} done: {court_success} success, {status['empties']} empty, {status['failed']} failed", flush=True)

    save_status(status)
    total_time = time.time() - batch_start
    print(f"\n{'='*60}", flush=True)
    print(f"🏁 ALL COURTS DONE in {total_time/3600:.1f}h", flush=True)
    print(f"   ✅ {status['success']} success | ⚠️ {status['empties']} empty | ❌ {status['failed']} failed", flush=True)
    print(f"{'='*60}", flush=True)