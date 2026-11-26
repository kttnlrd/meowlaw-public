#!/usr/bin/env python3
"""Process summary_chunk_1.json: generate AI summaries for 204 Australian tax cases."""
import json, os, re, sys, time, urllib.request
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / 'data'
SUMMARIES_DIR = BASE / 'scripts/cleaned/summaries'
CASE_TEXTS_DIR = DATA_DIR / 'case_texts'
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

# Load API key
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break
if not API_KEY:
    print("FATAL: No API key found", flush=True)
    sys.exit(1)

def clean_html_text(html):
    """Extract readable judgment text from raw AustLII HTML."""
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL|re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    for marker in ['REASONS FOR JUDGMENT', 'INTRODUCTION', 'FEDERAL COURT', 'JUDGMENT']:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx:]
            break
    idx = text.rfind('AustLII')
    if idx > 0:
        text = text[:idx]
    return text.strip()

def generate_summary(citation, title, text):
    """Generate AI summary from judgment text using OpenRouter."""
    text = text[:25000]
    if len(text) < 200:
        return {"citation": citation, "title": title, "error": "empty document", "text_length": len(text)}

    prompt = f'''You are a legal summariser specialising in Australian tax and customs law. Generate a structured JSON summary.

Citation: {citation}
Title: {title}

Judgment text:
{text}

Output ONLY valid JSON with these fields:
- "citation": "{citation}"
- "title": "{title}"
- "facts": string (2-4 sentences: factual background, procedural history)
- "issues": array of strings (legal questions addressed)
- "held": string (what the court decided — state the vote split explicitly, e.g. "By a 5:2 majority..." or "unanimously", and name which judges dissented)
- "reasoning": string (4-8 sentences of key reasoning, covering majority and any dissent)
- "outcome": string (result/orders)
- "judges": array of strings (each judge or joint bench member, e.g. "Gageler CJ", "Jagot J (dissenting)")
- "dissenting": array of strings (names of judges who dissented; empty array if unanimous)
- "bench_size": integer (total number of judges who sat)
- "cases_cited": array of full case citation strings
- "legislation_cited": array of legislation reference strings

Be precise and accurate to the text. Never call a decision unanimous when any judge dissented.'''

    payload = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1, "max_tokens": 4000
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://legislation-explorer.local",
                    "X-Title": "Legislation Explorer"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            return {"citation": citation, "title": title, "error": f"API failed: {e}"}
    else:
        return {"citation": citation, "title": title, "error": "API exhausted"}

    # Parse JSON from response
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
    try:
        summary = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            try:
                summary = json.loads(m.group())
            except:
                summary = {"citation": citation, "title": title, "error": "parse failed", "raw": content[:300]}
        else:
            summary = {"citation": citation, "title": title, "error": "no JSON", "raw": content[:300]}

    summary["_meta"] = {"source": "case_texts", "text_length": len(text)}
    return summary

# Load chunk
with open('/tmp/summary_chunk_1.json') as f:
    cases = json.load(f)

print(f"Processing {len(cases)} cases from chunk...", flush=True)
print(flush=True)

total = len(cases)
success = 0
failed = 0
empty = 0
start = time.time()

for i, case in enumerate(cases):
    court = case.get('court', '')
    citation = case['citation']
    title = case['title']
    ct_fname = case['fname']
    
    # Read and clean HTML
    try:
        with open(CASE_TEXTS_DIR / ct_fname) as f:
            raw_html = f.read()
        clean_text = clean_html_text(raw_html)
    except Exception as e:
        print(f"  [{i+1}/{total}] ❌ {citation}: read error: {e}", flush=True)
        failed += 1
        continue

    if len(clean_text) < 200:
        summary = {"citation": citation, "title": title, "error": "empty document", "text_length": len(clean_text)}
        safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
        with open(SUMMARIES_DIR / f"{safe}.json", 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"  [{i+1}/{total}] ⚠️ {citation}: too short ({len(clean_text)}c)", flush=True)
        empty += 1
        continue

    # Generate summary
    t0 = time.time()
    summary = generate_summary(citation, title, clean_text)
    elapsed = time.time() - t0

    safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    with open(SUMMARIES_DIR / f"{safe}.json", 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    has_error = bool(summary.get("error"))
    if has_error:
        failed += 1
        status = "❌"
        elapsed_total = time.time() - start
        rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
        remaining = total - (i + 1)
        eta = remaining / rate if rate > 0 else 0
        print(f"  [{i+1}/{total}] {status} {citation} ({case.get('year','')}) [{court}] in {elapsed:.0f}s | "
              f"✅{success} ❌{failed} ⚠️{empty} | ETA {eta/60:.0f}m", flush=True)
    else:
        success += 1
        status = "✅"
        outcome = summary.get('outcome', '').strip()
        print(f"  [{i+1}/{total}] {status} {citation} -> {outcome[:120]}", flush=True)

    elapsed_total = time.time() - start
    rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
    remaining = total - (i + 1)
    eta = remaining / rate if rate > 0 else 0

    if (i + 1) % 10 == 0:
        print(f"  ⏱️  [{i+1}/{total}] checkpoint: ✅{success} ❌{failed} ⚠️{empty} | elapsed {(elapsed_total)/60:.1f}m | ETA {eta/60:.0f}m", flush=True)

print(f"\n{'='*60}", flush=True)
print(f"DONE in {(time.time()-start)/60:.1f}m", flush=True)
print(f"✅ {success} success | ❌ {failed} failed | ⚠️ {empty} empty", flush=True)

# Print summary of all cases
print(f"\n{'='*60}", flush=True)
print(f"SUMMARY PER CASE:", flush=True)
with open('/tmp/summary_chunk_1.json') as f:
    cases2 = json.load(f)
for c in cases2:
    cit = c['citation']
    safe = cit.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    fp = SUMMARIES_DIR / f"{safe}.json"
    if fp.exists():
        try:
            with open(fp) as f:
                s = json.load(f)
            err = s.get('error', '')
            if err:
                print(f"  ❌ {cit}: ERROR - {err}", flush=True)
            else:
                outcome = s.get('outcome', '').strip()[:100]
                print(f"  ✅ {cit}: {outcome}", flush=True)
        except:
            print(f"  ❌ {cit}: corrupted JSON", flush=True)
    else:
        print(f"  ❌ {cit}: no summary file", flush=True)