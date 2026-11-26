#!/usr/bin/env python3
"""Process chunk 2: generate AI summaries for 204 Australian tax cases."""
import json, os, re, sys, time, urllib.request
from pathlib import Path

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
    """Generate AI summary from judgment text."""
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
with open("/tmp/summary_chunk_2.json") as f:
    cases = json.load(f)

# Check existing summaries so we can skip already-done ones
existing_summaries = set(os.listdir(str(SUMMARIES_DIR)))

total = len(cases)
success = 0
failed = 0
empty = 0
start = time.time()

print(f"Processing {total} cases from chunk 2...\n")

for i, case in enumerate(cases):
    citation = case.get("citation", "?")
    title = case.get("title", "?")
    fname = case.get("fname", "")
    court = case.get("court", "?")

    safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    out_path = SUMMARIES_DIR / f"{safe}.json"

    if out_path.name in existing_summaries:
        # Check if it has an error or is legit
        try:
            with open(out_path) as cf:
                existing = json.load(cf)
            if existing.get("error"):
                # Re-process if error
                pass
            else:
                print(f"  [{i+1}/{total}] ✅ {citation} (already done)")
                success += 1
                continue
        except:
            pass

    # Read HTML
    html_path = CASE_TEXTS_DIR / fname
    if not html_path.exists():
        print(f"  [{i+1}/{total}] ❌ {citation}: HTML file not found: {fname}")
        failed += 1
        continue

    try:
        with open(html_path) as f:
            raw_html = f.read()
        clean_text = clean_html_text(raw_html)
    except Exception as e:
        print(f"  [{i+1}/{total}] ❌ {citation}: read error: {e}")
        failed += 1
        continue

    if len(clean_text) < 200:
        summary = {"citation": citation, "title": title, "error": "empty document", "text_length": len(clean_text)}
        with open(out_path, 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"  [{i+1}/{total}] ⚠️ {citation}: too short ({len(clean_text)}c)")
        empty += 1
        continue

    # Generate summary
    t0 = time.time()
    summary = generate_summary(citation, title, clean_text)
    elapsed = time.time() - t0

    with open(out_path, 'w') as f:
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

    outcome_str = summary.get("outcome", summary.get("held", "?"))[:80]
    print(f"  [{i+1}/{total}] {status} {citation} ({title[:50]}) in {elapsed:.0f}s | "
          f"✅{success} ❌{failed} ⚠️{empty} | ETA {eta/60:.0f}m | outcome: {outcome_str}")

print(f"\n{'='*60}")
print(f"DONE in {(time.time()-start)/60:.1f}m")
print(f"✅ {success} success | ❌ {failed} failed | ⚠️ {empty} empty")