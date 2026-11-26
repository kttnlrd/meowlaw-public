#!/usr/bin/env python3
"""Re-generate summaries for the 44 'no JSON found' error files and 3 empty documents."""
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

OUT = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned"
SUMMARY_DIR = f"{OUT}/summaries"

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

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

# Find error files
error_files = []
for fname in sorted(os.listdir(SUMMARY_DIR)):
    if not fname.endswith(".json"):
        continue
    with open(os.path.join(SUMMARY_DIR, fname)) as f:
        try:
            data = json.load(f)
        except:
            continue
    if data.get("error"):
        error_files.append((fname, data))

print(f"Found {len(error_files)} error files to regenerate")

ok = 0
failed = 0
skipped = 0

for fname, old_data in error_files:
    citation = old_data.get("citation", "")
    case_name = old_data.get("case_name", "")
    
    if not citation:
        skipped += 1
        print(f"[{skipped+ok+failed}] {fname}: no citation, skipping")
        continue
    
    # Fetch full text from DB
    safe = citation.replace("'", "''")
    r = subprocess.run([
        "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
        "-d", "cadena_knowledge", "-tA",
        "-c", f"SELECT json_build_object('citation',c.citation,'case_name',c.case_name,'court',c.court,'text', (SELECT string_agg(cp.content, E'\\n\\n' ORDER BY cp.sequence_order) FROM case_paragraphs cp WHERE cp.case_id=c.id)) FROM cases c WHERE c.citation='{safe}';"
    ], capture_output=True, text=True, timeout=60)
    
    if not r.stdout.strip():
        print(f"  {citation}: not in DB, keeping as-is")
        skipped += 1
        continue
    
    try:
        case_data = json.loads(r.stdout.strip())
    except:
        print(f"  {citation}: DB parse error, keeping as-is")
        skipped += 1
        continue
    
    text = case_data.get("text", "")
    if not text or len(text) < 100:
        print(f"  {citation}: empty text ({len(text)} chars), keeping as-is")
        skipped += 1
        continue
    
    doc = text[:120000]
    
    print(f"  [{ok+failed+skipped}/{len(error_files)}] {citation}...", end=" ", flush=True)
    
    result = None
    for attempt in range(3):
        try:
            result = api_call(SUMMARY_PROMPT + doc)
            if result: break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(30 * (2 ** attempt), 120)
                print(f"rate limited, waiting {wait}s...", end=" ", flush=True)
                time.sleep(wait)
                continue
            elif attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            else:
                print(f"HTTP {e.code}")
                failed += 1
                break
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"API failed: {e}")
            failed += 1
            break
    
    if not result:
        continue
    
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
                print("parse failed")
                failed += 1
                continue
        else:
            print("no JSON")
            failed += 1
            continue
    
    summary["_meta"] = {"source": citation, "text_size_kb": round(len(text)/1024), "api_time_s": 0, "total_time_s": 0}
    
    safe_fname = citation.replace(" ", "_").replace("/", "_").replace("[", "").replace("]", "")
    with open(f"{SUMMARY_DIR}/{safe_fname}.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    ok += 1
    print(f"✅ {case_name[:60]} ({len(text)//1024}kb)")

print(f"\nDone. ✅ {ok} fixed | ❌ {failed} failed | ⏭️ {skipped} skipped")