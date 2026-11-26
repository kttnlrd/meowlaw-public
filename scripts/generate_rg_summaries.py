#!/usr/bin/env python3
"""Generate AI summaries for ASIC Regulatory Guides (RGs).

Reads each RG text file (data/regulatory-guides/texts/RG_*.txt) and the manifest,
sends text to DeepSeek V4 Flash via OpenRouter, and saves structured summaries
to data/regulatory-guides/summaries/RG_{number}.json.

Slice support for parallel workers:
  python3 generate_rg_summaries.py --slice-idx 0 --slice-total 3
"""

import json
import os
import re
import time
import urllib.request
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
TEXTS_DIR = BASE_DIR / "data" / "regulatory-guides" / "texts"
MANIFEST_PATH = BASE_DIR / "data" / "regulatory-guides" / "rg_manifest.json"
OUTPUT_DIR = BASE_DIR / "data" / "regulatory-guides" / "summaries"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "OPENROUTER_API_KEY" in line and "***" not in line:
                parts = line.strip().split("=", 1)
                if len(parts) == 2 and parts[1]:
                    API_KEY = parts[1]
                    break

# Load manifest
with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
    MANIFEST = json.load(f)

# Build lookup: rg_number -> metadata
MANIFEST_BY_NUMBER = {entry["rg_number"]: entry for entry in MANIFEST}


def get_rg_files() -> list[Path]:
    """Get all existing RG text files sorted by number."""
    files = []
    for p in sorted(TEXTS_DIR.glob("RG_*.txt")):
        files.append(p)
    return files


def parse_rg_number(filename: str) -> int:
    """Extract RG number from filename like RG_001.txt -> 1."""
    m = re.search(r"RG_(\d+)", filename)
    return int(m.group(1)) if m else 0


def get_rg_metadata(rg_number: int) -> dict:
    """Get metadata from manifest for a given RG number."""
    return MANIFEST_BY_NUMBER.get(rg_number, {})


def extract_status(rg_number: int) -> str:
    """Get status from manifest."""
    meta = get_rg_metadata(rg_number)
    status = meta.get("status", "current")
    # Normalize: current -> "Final", withdrawn -> "Withdrawn"
    if status == "current":
        return "Final"
    elif status == "withdrawn":
        return "Withdrawn"
    return status.capitalize()


def extract_date(rg_number: int) -> str:
    """Extract date from manifest or text."""
    meta = get_rg_metadata(rg_number)
    date = meta.get("date", "")
    if date:
        # Dates look like "16-" or "27-" or "March 2024" — clean up
        date_clean = date.rstrip("-").strip()
        if date_clean:
            return date_clean
    return ""


def prepare_text_for_ai(text: str, max_chars: int = 8000) -> str:
    """Prepare RG text for AI summarization by keeping the most important parts.

    Keeps the beginning (title, about, key points, first few sections) and
    the end (key terms, related information with legislation/case refs).
    Total is capped at max_chars.
    """
    if len(text) <= max_chars:
        return text

    # Keep first ~60% and last ~40% of the budget
    head_chars = int(max_chars * 0.6)
    tail_chars = max_chars - head_chars

    head = text[:head_chars]
    tail = text[-tail_chars:]

    return head + "\n[... content truncated ...]\n" + tail


SUMMARY_PROMPT = """Read this ASIC Regulatory Guide and output a structured summary as JSON.

Output valid JSON ONLY (no other text) with this exact structure:
{
  "citation": "RG 1",
  "title": "Full guide title",
  "type": "ASIC Regulatory Guide",
  "status": "Final | Withdrawn",
  "subject": "2-3 sentences on what the guide addresses",
  "background": "2-3 sentences on the legislative context, purpose, and who the guide is for",
  "ruling": "3-6 sentences covering ASIC's stated regulatory approach, interpretation of the law, and key guidance given",
  "date_of_effect": "YYYY-MM-DD or description of when this guide applies",
  "legislation_referenced": ["Corporations Act 2001 (Cth) s 912A", ...],
  "cases_referenced": ["Plaintiff v Defendant [YYYY] COURT N", ...],
  "related_rulings": ["RG 2", "INFO 225"]
}

CRITICAL RULES:
- cases_referenced: EVERY entry MUST include BOTH the full case name AND a proper medium-neutral citation
- NEVER output "(no full citation provided)" or similar placeholder text
- If you find a case name but cannot find a proper citation, OMIT it
- Format: "Plaintiff v Defendant [YYYY] COURT N"
- legislation_referenced: include full act name with jurisdiction and specific section numbers where mentioned
- related_rulings: any related ASIC regulatory guides, information sheets, reports, or instruments referenced
- ruling: focus on ASIC's stated regulatory approach, not legal advice. Describe what ASIC says it will do or how it interprets the law
- subject: concise description of what the guide covers
- background: legislative context and who the guide is intended for

DOCUMENT:
"""


def ai_summarize(text: str, max_text: int = 8000) -> dict:
    """Send RG text to DeepSeek V4 Flash and get structured summary."""
    prepared = prepare_text_for_ai(text, max_text)

    payload = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": SUMMARY_PROMPT + prepared}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://legislation-explorer.local",
            "X-Title": "Legislation Explorer",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read())

    content = result["choices"][0]["message"]["content"]
    # Try to extract JSON from the response
    json_match = re.search(r"\{.*\}", content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {"error": "JSON parse failed", "raw": content[:300]}


def process_rgs(files: list[Path], skip_existing: bool = True):
    """Process RG files with AI summarization."""
    total = len(files)
    done = 0
    errors = 0
    t0 = time.time()

    for i, f in enumerate(files):
        rg_number = parse_rg_number(f.name)
        citation = f"RG {rg_number}"
        out_path = OUTPUT_DIR / f"RG_{rg_number}.json"

        if skip_existing and out_path.exists():
            done += 1
            continue

        try:
            meta = get_rg_metadata(rg_number)
            text = f.read_text(encoding="utf-8", errors="replace")
            result = ai_summarize(text)

            if "error" in result:
                print(f"  ERROR {citation}: {result.get('error')}")
                errors += 1
                # Write error summary to avoid reprocessing
                result["citation"] = citation
                result["title"] = meta.get("title", citation)
                result["type"] = "ASIC Regulatory Guide"
                result["status"] = extract_status(rg_number)
                out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                # Ensure required fields
                result["citation"] = result.get("citation", citation)
                result["title"] = result.get("title", meta.get("title", citation))
                result["type"] = result.get("type", "ASIC Regulatory Guide")
                result["status"] = result.get("status", extract_status(rg_number))
                # Ensure arrays exist
                for arr_field in ["legislation_referenced", "cases_referenced", "related_rulings"]:
                    if arr_field not in result or not isinstance(result[arr_field], list):
                        result[arr_field] = []
                out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                done += 1

        except Exception as e:
            print(f"  EXCEPTION {citation}: {e}")
            errors += 1

        if (i + 1) % 10 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{citation}] {i+1}/{total} ({done} OK, {errors} errors) "
                  f"@ {rate:.1f}/s, ETA {remaining/60:.0f}m")

    elapsed = time.time() - t0
    print(f"Done: {done} OK, {errors} errors / {total} in {elapsed/60:.1f}m")


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate RG summaries")
    parser.add_argument("--slice-idx", type=int, default=None, help="Slice index (0-based)")
    parser.add_argument("--slice-total", type=int, default=None, help="Total slices")
    parser.add_argument("--no-skip", action="store_true", help="Re-process even if exists")
    parser.add_argument("--rg", type=int, default=None, help="Process a single RG number only")
    args = parser.parse_args()

    all_files = get_rg_files()
    print(f"Found {len(all_files)} RG text files")

    # Filter to single RG if specified
    if args.rg is not None:
        all_files = [f for f in all_files if parse_rg_number(f.name) == args.rg]
        print(f"Processing single RG {args.rg}")

    # Apply slice if specified
    if args.slice_idx is not None and args.slice_total is not None:
        total = len(all_files)
        chunk_size = total // args.slice_total
        remainder = total % args.slice_total
        start = args.slice_idx * chunk_size + min(args.slice_idx, remainder)
        end = start + chunk_size + (1 if args.slice_idx < remainder else 0)
        all_files = all_files[start:end]
        print(f"Slice {args.slice_idx + 1}/{args.slice_total}: processing {len(all_files)} RGs")

    print(f"Processing {len(all_files)} RGs...")
    process_rgs(all_files, skip_existing=not args.no_skip)
