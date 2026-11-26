#!/usr/bin/env python3
"""Process a chunk of cases: read raw HTML, generate AI summary, save JSON.
Usage: python3 process_chunk.py <chunk_file.json> [output_dir]
"""
import json, os, re, sys, time, urllib.request
from pathlib import Path

CHUNK_FILE = sys.argv[1]
OUTPUT_DIR = sys.argv[2] if len(sys.argv) > 2 else f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"
CASE_TEXTS_DIR = f"{Path(__file__).resolve().parent.parent}/data/case_texts"

# Load API key
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

def clean_html_text(html):
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
- "held": string (what the court decided)
- "reasoning": string (4-8 sentences of key reasoning)
- "outcome": string (result/orders)
- "cases_cited": array of full case citation strings
- "legislation_cited": array of legislation reference strings

Be precise and accurate to the text.'''

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

# Main
with open(CHUNK_FILE) as f:
    cases = json.load(f)

total = len(cases)
success = 0
failed = 0
empty = 0
start = time.time()

os.makedirs(OUTPUT_DIR, exist_ok=True)

for i, case in enumerate(cases):
    citation = case['citation']
    title = case['title']
    safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    outpath = os.path.join(OUTPUT_DIR, f"{safe}.json")

    # Skip if already exists
    if os.path.exists(outpath):
        with open(outpath) as f:
            existing = json.load(f)
        if not existing.get("error"):
            success += 1
            continue

    # Read HTML
    fname = case['fname']
    html_path = os.path.join(CASE_TEXTS_DIR, fname)
    if not os.path.exists(html_path):
        failed += 1
        print(f"  [{i+1}/{total}] ⚠️ {citation}: HTML not found at {html_path}")
        continue

    with open(html_path) as f:
        raw_html = f.read()

    clean_text = clean_html_text(raw_html)

    if len(clean_text) < 200:
        summary = {"citation": citation, "title": title, "error": "empty document", "text_length": len(clean_text)}
        with open(outpath, 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        empty += 1
        print(f"  [{i+1}/{total}] ⚠️ {citation}: too short ({len(clean_text)}c)")
        continue

    t0 = time.time()
    summary = generate_summary(citation, title, clean_text)
    elapsed = time.time() - t0

    with open(outpath, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    if summary.get("error"):
        failed += 1
        print(f"  [{i+1}/{total}] ❌ {citation} in {elapsed:.0f}s: {summary['error'][:60]}")
    else:
        success += 1
        print(f"  [{i+1}/{total}] ✅ {citation} in {elapsed:.0f}s")

    # Print stats every 10
    if (i + 1) % 10 == 0:
        elapsed_total = time.time() - start
        rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
        remaining = total - (i + 1)
        eta = remaining / rate if rate > 0 else 0
        print(f"  📊 [{i+1}/{total}] ✅{success} ❌{failed} ⚠️{empty} | @ {rate:.1f}/s | ETA {eta/60:.0f}m")

elapsed_total = time.time() - start
print(f"\n{'='*60}")
print(f"CHUNK DONE in {elapsed_total/60:.1f}m")
print(f"✅ {success} success | ❌ {failed} failed | ⚠️ {empty} empty")
