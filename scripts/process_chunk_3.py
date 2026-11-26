#!/usr/bin/env python3
"""Process chunk 3: generate AI summaries for 204 Australian tax cases."""
import json, os, re, time, urllib.request, sys
from pathlib import Path

# Add the agent checkout under HERMES_DIR to path to get API key
sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("HERMES_DIR", "~/.hermes")), "hermes-agent"))
from run_agent import AIAgent

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")
SUMMARIES_DIR = Path(f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries")
CASE_TEXTS_DIR = DATA_DIR / 'case_texts'
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_FILE = Path('/tmp/summary_chunk_3.json')

# Get API key from Hermes Agent credential system (without printing it)
agent = AIAgent(
    provider="openrouter",
    model="deepseek/deepseek-v4-flash",
    base_url="https://openrouter.ai/api/v1",
    api_key=None,
    quiet_mode=True,
    max_iterations=0,
)
API_KEY = agent.api_key

def clean_html_text(html):
    """Extract readable judgment text from raw AustLII HTML."""
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
    """Generate AI summary from judgment text via OpenRouter."""
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
with open(CHUNK_FILE) as f:
    cases = json.load(f)

print(f"Processing {len(cases)} cases from chunk 3...")
print()

total = len(cases)
success = 0
failed = 0
empty = 0
start = time.time()

for i, case in enumerate(cases):
    citation = case['citation']
    title = case['title']
    court = case.get('court', 'hca')
    year = case.get('year', '')
    fname = case['fname']

    # Skip already existing summaries
    safe = citation.replace(' ', '_').replace('/', '_').replace('[', '').replace(']', '')
    out_path = SUMMARIES_DIR / f"{safe}.json"
    if out_path.exists():
        print(f"  [{i+1}/{total}] ⏭️  {citation}: already exists, skipping")
        continue

    # Read and clean HTML
    try:
        with open(CASE_TEXTS_DIR / fname) as f:
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
        print(f"  [{i+1}/{total}] ⚠️  {citation}: too short ({len(clean_text)}c)")
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

    # Extract outcome for one-line summary
    outcome = summary.get("outcome", summary.get("held", "N/A"))[:80]

    elapsed_total = time.time() - start
    rate = (i + 1) / elapsed_total if elapsed_total > 0 else 0
    remaining = total - (i + 1)
    eta = remaining / rate if rate > 0 else 0

    print(f"  [{i+1}/{total}] {status} {citation} ({year}) [{court}] in {elapsed:.0f}s | "
          f"outcome: {outcome}")

print(f"\n{'='*60}")
print(f"CHUNK 3 DONE in {(time.time()-start)/60:.1f}m")
print(f"✅ {success} success | ❌ {failed} failed | ⚠️ {empty} empty | Total: {total}")