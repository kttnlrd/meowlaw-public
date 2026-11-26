#!/usr/bin/env python3
"""Batch-generate summaries for all cases with raw HTML but no summary JSON.
Reads from data/case_texts/, generates AI summaries, saves to scripts/cleaned/summaries/
"""
import json, os, re, time, urllib.request
from pathlib import Path
from collections import Counter

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")
SUMMARIES_DIR = Path(f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries")
CASE_TEXTS_DIR = DATA_DIR / 'case_texts'
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

# API key
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    with open(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
        for line in f:
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                API_KEY = line.strip().split("=", 1)[1]
                break

def clean_html_text(html):
    """Extract readable judgment text from raw AustLII HTML."""
    import re
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL|re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    # Find judgment start
    for marker in ['REASONS FOR JUDGMENT', 'INTRODUCTION', 'FEDERAL COURT', 'JUDGMENT']:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx:]
            break
    # Remove trailing AustLII chrome
    idx = text.rfind('AustLII')
    if idx > 0:
        text = text[:idx]
    return text.strip()

def generate_summary(citation, title, text):
    """Generate AI summary from judgment text."""
    text = text[:25000]  # truncate
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

# Build the list of cases to process
COURT_FILES = {
    "hca": DATA_DIR / "hca_tax_cases.json",
    "fca": DATA_DIR / "fca_tax_cases.json",
    "fcafc": DATA_DIR / "fcafc_tax_cases.json",
    "aata": DATA_DIR / "aata_tax_cases.json",
}

all_cases = []
for court_key, path in COURT_FILES.items():
    if path.exists():
        with open(path) as f:
            cases = json.load(f)
        for c in cases:
            all_cases.append((court_key, c.get('citation', ''), c.get('title', '')))

existing_summaries = set(os.listdir(str(SUMMARIES_DIR)))
existing_case_texts = set(os.listdir(str(CASE_TEXTS_DIR)))

to_process = []
for court, citation, title in all_cases:
    safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    if f"{safe}.json" in existing_summaries:
        continue
    m = re.match(r'\[(\d{4})\]\s+(\S+)\s+(\d+)', citation)
    if m:
        year, court_abbr, num = m.groups()
        ct_fname = f"{year}_{court_abbr}_{num}.html"
    else:
        ct_fname = f"{safe}.html"
    if ct_fname in existing_case_texts:
        to_process.append((court, citation, title, year, ct_fname))

# Sort by year descending (recent first), then by court priority
court_priority = {"hca": 0, "fcafc": 1, "fca": 2}
to_process.sort(key=lambda x: (-int(x[3]), court_priority.get(x[0], 9), x[1]))

print(f"Processing {len(to_process)} cases...")
print()

total = len(to_process)
success = 0
failed = 0
empty = 0
start = time.time()

for i, (court, citation, title, year, ct_fname) in enumerate(to_process):
    # Read and clean HTML
    try:
        with open(CASE_TEXTS_DIR / ct_fname) as f:
            raw_html = f.read()
        clean_text = clean_html_text(raw_html)
    except Exception as e:
        print(f"  [{i+1}/{total}] ❌ {citation}: read error: {e}")
        failed += 1
        continue

    if len(clean_text) < 200:
        # Save empty marker
        summary = {"citation": citation, "title": title, "error": "empty document", "text_length": len(clean_text)}
        safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
        with open(SUMMARIES_DIR / f"{safe}.json", 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"  [{i+1}/{total}] ⚠️ {citation}: too short ({len(clean_text)}c)")
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
    else:
        success += 1
        status = "✅"

    elapsed_total = time.time() - start
    rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
    remaining = total - (i + 1)
    eta = remaining / rate if rate > 0 else 0

    print(f"  [{i+1}/{total}] {status} {citation} ({year}) [{court}] in {elapsed:.0f}s | "
          f"✅{success} ❌{failed} ⚠️{empty} | ETA {eta/60:.0f}m")

print(f"\n{'='*60}")
print(f"DONE in {(time.time()-start)/60:.1f}m")
print(f"✅ {success} success | ❌ {failed} failed | ⚠️ {empty} empty")