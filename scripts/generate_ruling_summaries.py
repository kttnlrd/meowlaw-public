#!/usr/bin/env python3
"""Generate ruling summaries.

Two modes:
  --type aid    Regex-based extraction for ATO IDs (fast, no AI)
  --type full   AI summarization via DeepSeek V4 Flash (full rulings)

Slice support for parallel workers:
  python3 generate_ruling_summaries.py --type full --slice-idx 0 --slice-total 5
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
RULINGS_DIR = Path(__file__).resolve().parent.parent / "data" / "rulings"
OUTPUT_DIR = RULINGS_DIR / "summaries"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not API_KEY:
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "DEEPSEEK_API_KEY" in line and "***" not in line:
                parts = line.strip().split("=", 1)
                if len(parts) == 2 and parts[1]:
                    API_KEY = parts[1]
                    break

# ── Case citation regex patterns ───────────────────────────────────────────
CASE_PATTERNS = [
    re.compile(r"\[\d{4}\]\s+(?:HCA|FCAFC|FCA|AATA|NSWSC|NSWCA|VSC|VSCA|QSC|QCA|SASC|SASCFC|WASC|WASCA|FedCFamC2G|FamCA|FCCA|ACTSC|NTSC|TASSC)\s+\d+"),
    re.compile(r"\(\d{4}\)\s+\d+\s+(?:CLR|FCR|ALR|ALD|ATR|NSWLR|VR|WAR|SASR|Qd\s*R|TASR|NTJ)\s+\d+"),
]

# ── Type config ─────────────────────────────────────────────────────────────
# Type -> (label, count)
ALL_TYPES = {
    "TR": 449, "TD": 1032, "CR": 2517, "PR": 1063, "PS_LA": 277,
    "GSTR": 128, "PCG": 73, "LCG": 55, "TA": 152, "IT": 233,
    "MT": 16, "SGR": 4,
}

# ── References-section parser (deterministic enrichment, LK-03) ─────────────
# The LLM summary undercounts `cases_referenced` and leaves `related_rulings`
# empty because it does not read the ruling's closing "References" block. This
# parser reads the authoritative lists straight from the source text and is
# merged into the AI result, so a future run cannot regress on those fields.
_RULING_CIT_RE = re.compile(
    r"\b(?:TR|TD|CR|PR|GSTR|LCG|LCR|PCG|MT|SGR|TA|ATOID|AID|FTR|FTD|PS\s?LA)"
    r"\s+\d{2,4}/D?\d+\b"
    r"|\bIT\s+\d{1,4}\b"
)
_MNC_RE = re.compile(
    r"\[\d{4}\]\s+(?:HCA|FCAFC|FCA|AATA|ARTA|NSWSC|NSWCA|VSC|VSCA|QSC|QCA|"
    r"SASC|SASCFC|WASC|WASCA|FedCFamC2G|FamCA|FCCA|ACTSC|NTSC|TASSC)\s+\d+"
)

# Sub-labels that terminate a References sub-section.
_REF_STOPS = (
    "Legislative references", "Legislative References",
    "Related Rulings/Determinations", "Related Rulings", "Previous draft",
    "Cases relied on", "Other references", "Other References",
    "ATO references", "ATO References",
)


def parse_references_section(text: str) -> dict:
    """Extract related rulings + cases from an ATO ruling's back-matter.

    Returns {"cases_referenced": [...], "related_rulings": [...]}. Empty lists
    when the document has no such sections. Handles both the newer layout
    (a standalone 'References' heading with 'Cases relied on' / 'Related
    Rulings' sub-headings) and the older layout ('Related Rulings/Determinations'
    heading with no 'References' block).
    """
    refs: dict[str, list[str]] = {"cases_referenced": [], "related_rulings": []}

    def _slice_after_last(label: str, stops: tuple[str, ...]) -> str:
        # Anchor on the LAST occurrence of the heading — back-matter headings
        # sit at the end of the document.
        idx = text.rfind(label)
        if idx == -1:
            return ""
        rest = text[idx + len(label):]
        rest = re.sub(r"^\s*:?", "", rest)
        end = len(rest)
        for sl in stops:
            if sl == label:
                continue
            sm = re.search(re.escape(sl), rest)
            if sm and sm.start() < end:
                end = sm.start()
        return rest[:end]

    # Cases — one entry per bullet. Handle all three heading variants (newer
    # "Cases relied on", older "Case References"/"Cases Referred to").
    seen_case: set[str] = set()
    for case_label in ("Cases relied on", "Case References", "Cases Referred to"):
        case_block = _slice_after_last(case_label, _REF_STOPS)
        for raw in re.split(r"\s+-\s+", case_block):
            entry = re.sub(r"\s+", " ", raw).strip(" -;:\n")
            if len(entry) < 6:
                continue
            # Must look like a case citation (a court report, a year, or a party name).
            if not re.search(
                r"(\b(?:v\.?|Case|ATC|ATD|FCR|CLR|ALR|FLR|ALJR|ATR)\b|\[\d{4}\]|\(\d{4}\))",
                entry, re.IGNORECASE,
            ):
                continue
            key = entry.lower()
            if key in seen_case:
                continue
            seen_case.add(key)
            # Repair PDF line-wrap artefacts: "Tully Co- operative" → "Co-operative".
            entry = re.sub(r"(\w)- (\w)", r"\1-\2", entry)
            refs["cases_referenced"].append(entry)

    # Related rulings / determinations / previous drafts (semicolon-separated).
    for label in ("Related Rulings/Determinations", "Related Rulings",
                  "Previous draft"):
        seg = _slice_after_last(label, _REF_STOPS)
        for m in _RULING_CIT_RE.finditer(seg):
            cit = re.sub(r"\s+", " ", m.group(0)).strip()
            if cit not in refs["related_rulings"]:
                refs["related_rulings"].append(cit)
    return refs

SUMMARY_PROMPT = """Read this ATO ruling and output a structured summary as JSON.

Output valid JSON ONLY (no other text) with this exact structure:
{
  "citation": "TR 2024/1",
  "title": "Full ruling title",
  "type": "Tax Ruling | Taxation Determination | Practical Compliance Guideline | etc",
  "status": "Final | Withdrawn | Draft",
  "subject": "2-3 sentences on what the ruling addresses",
  "background": "2-3 sentences on the legislative context or factual scenario",
  "ruling": "3-6 sentences covering the key binding positions",
  "date_of_effect": "YYYY-MM-DD or short description, or null",
  "legislation_referenced": ["Income Tax Assessment Act 1997 (Cth) s 6-1", ...],
  "cases_referenced": ["Plaintiff v Defendant [YYYY] COURT N", ...],
  "related_rulings": ["TR 2023/1"]
}

CRITICAL RULES:
- date_of_effect: populate ONLY from a date the document actually states (its
  "Date of effect" section, a "Date of Issue"/"Date of Effect" line, or an
  explicit "applies from"/"applies to" sentence). Copy the date verbatim or
  restate it as YYYY-MM-DD. If the document states no explicit date, output
  null. NEVER invent a date, and NEVER use 1 January of a year (YYYY-01-01)
  as a placeholder for a year-only or unknown date.
- cases_referenced: EVERY entry MUST include BOTH the full case name AND a proper medium-neutral citation
- NEVER output "(no full citation provided)" or similar placeholder text
- If you find a case name but cannot find a proper citation, OMIT it
- Format: "Plaintiff v Defendant [YYYY] COURT N"
- ALWAYS read the document's closing "References" section before answering.
  It is the authoritative and complete list; do not rely on the body alone:
  * "Cases relied on:" is a bullet list of binding case law — put EVERY entry
    into cases_referenced (do not stop after the first few).
  * "Related Rulings/Determinations:" and "Previous draft:" list other ATO
    products in a semicolon-separated list (e.g. "LCR 2020/2; TR 97/23;
    TR 2006/10") — split on "; " and put EACH citation into related_rulings.
- legislation_referenced: include full act name with jurisdiction and specific section numbers; if more than 25 distinct provisions, list only the 25 most important (the ones central to the ruling's outcome)
- related_rulings: any rulings referenced including withdrawn-by, replaces, or other related documents — include every citation from the References section's "Related Rulings/Determinations" and "Previous draft" lines
- ruling: focus on the binding positions the Commissioner is taking

DOCUMENT:
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def get_ruling_files(rtype: str) -> list[Path]:
    """Get all ruling files for a given type, sorted."""
    return sorted(RULINGS_DIR.glob(f"{rtype}_*.txt"))


def parse_citation(filename: str, rtype: str) -> str:
    """Parse citation from filename like TR_2024_1.txt -> TR 2024/1."""
    stem = filename.replace(".txt", "")
    if rtype == "PS_LA":
        parts = stem.split("_")
        return f"PS LA {parts[2]}/{parts[3]}"
    parts = stem.split("_", 2)
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1]}/{parts[2]}"
    return stem


def extract_status(text: str) -> str:
    """Check if ruling is withdrawn or draft."""
    if "Withdrawn" in text[:1000]:
        return "Withdrawn"
    if "Draft" in text[:1000]:
        return "Draft"
    return "Final"


def extract_aid(text: str, citation: str) -> dict:
    """Extract ATO ID summary using regex (no AI)."""
    cat = ""
    title = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("ATO ID"):
            # Look ahead for category and title, skipping ===== and blank lines
            for j in range(i + 1, min(i + 6, len(lines))):
                stripped = lines[j].strip()
                if not stripped or re.match(r'^[=*\s-]+$', stripped):
                    continue
                if not cat:
                    cat = stripped
                elif not title:
                    title = stripped.strip()
                    # Remove leading whitespace/indentation from title
                    title = re.sub(r'^\s+', '', title)
                    break
            break

    # Strip the CAUTION/FOI boilerplate from the body
    # The boilerplate starts at "FOI status:" and ends at the "Issue" section
    body = text
    notice = ""
    foi_match = re.search(r'(FOI status:.*?)(?=Issue|\Z)', text, re.DOTALL)
    if foi_match:
        notice = foi_match.group(1).strip()
        # Remove the CAUTION block from the body
        caution_match = re.search(r'CAUTION:.*?(?:professional adviser\.|Interest\.)', text, re.DOTALL)
        if caution_match:
            body = text[:caution_match.start()] + text[caution_match.end():]
            # Also remove the FOI line from the body
            body = re.sub(r'\s+FOI status:.*?\n', '\n', body)

    # Strip header from body (everything before "Issue")
    issue_start = body.find("Issue")
    if issue_start >= 0:
        body = body[issue_start:]

    # Question: only the first paragraph after "Issue"
    question = ""
    m = re.search(r"Issue\n(.+?)(?:\n\n|\nDecision|\nFacts|\Z)", body, re.DOTALL)
    if m:
        question = m.group(1).strip()

    # Case refs via regex
    cases = set()
    for cp in CASE_PATTERNS:
        for m in cp.finditer(text):
            cases.add(m.group(0).strip())

    # Legislation refs via regex — bounded act-title capture
    # Strategy: match known short-name act formats + optional (Cth),
    # then capture section refs that immediately follow. Deduplicate by (act, section).
    leg = set()
    _KNOWN_ACTS_RE = re.compile(
        r"(?:Income Tax Assessment Act \d{4}|Fringe Benefits Tax(?: Assessment)? Act \d{4}|"
        r"A New Tax System \(Goods and Services Tax\) Act \d{4}|Taxation Administration Act \d{4}|"
        r"Tax Agent Services Act \d{4}|Corporations Act \d{4}|"
        r"Superannuation Industry \(Supervision\) Act \d{4}|"
        r"Superannuation Guarantee \(Administration\) Act \d{4}|"
        r"Family Law Act \d{4}|Social Security Act \d{4}|"
        r"ITAA\s+\d{4}|TAA\s+\d{4})"
        r"(?:\s+\(Cth\))?"
        r"(?:\s+s(?:s|ection)?\.?\s+(\d+[-\dA-Za-z]*(?:\([^)]+\))?))?",
        re.IGNORECASE,
    )
    seen_pairs: set[tuple[str, str]] = set()
    for m in _KNOWN_ACTS_RE.finditer(text):
        act = m.group(0).strip()
        section = m.group(1) or ""
        # Sentence-boundary guard: if the match is followed by a comma+word, it may
        # be a sentence fragment — reject unless act name is a clear short-title.
        # Only keep if we captured a section or the act stand-alone looks valid.
        if not section:
            # Stand-alone act name: keep only if ends with (Cth) or year
            if not (act.endswith(")") or re.search(r'\d{4}\s*$', act)):
                continue
        # Build canonical form
        if section:
            # Normalise section numbers to lowercase for dedup
            norm_section = section.lower().strip()
            key = (act.lower().strip(), norm_section)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            leg.add(f"{act} s {section}")
        else:
            leg.add(act)

    # Also find \"section X of the Act Name\" patterns — bounded to avoid capturing sentence fragments
    standalone = re.findall(
        r"(?:section|s\.?)\s+(\d+[-\dA-Za-z]*(?:\([^)]+\))?)\s+of\s+the\s+"
        r"(Income Tax Assessment Act \d{4}|Fringe Benefits Tax(?: Assessment)? Act \d{4}|"
        r"A New Tax System \(Goods and Services Tax\) Act \d{4}|Taxation Administration Act \d{4}|"
        r"Tax Agent Services Act \d{4}|Corporations Act \d{4}|"
        r"Superannuation Industry \(Supervision\) Act \d{4}|"
        r"Superannuation Guarantee \(Administration\) Act \d{4}|"
        r"Family Law Act \d{4}|Social Security Act \d{4}|"
        r"ITAA\s+\d{4}|TAA\s+\d{4})"
        r"(?:\s+\(Cth\))?",
        text, re.IGNORECASE,
    )
    for sec_num, act_name in standalone:
        act_full = f"{act_name.strip()} (Cth)"
        norm_section = sec_num.lower().strip()
        key = (act_full.lower(), norm_section)
        if key not in seen_pairs:
            seen_pairs.add(key)
            leg.add(f"{act_full} s {sec_num}")
        elif f"{act_full} s {sec_num}" not in leg:
            leg.add(f"{act_full} s {sec_num}")

    # Format title with citation prefix
    display_title = f"{citation} — {title}" if title else citation

    # Status
    status = "Final"
    if re.search(r"withdrawn", text[:2000], re.IGNORECASE):
        status = "Withdrawn"

    return {
        "citation": citation,
        "title": display_title,
        "type": "ATO Interpretative Decision",
        "status": status,
        "subject": cat,
        "question": question,
        "notice": notice,
        "body": body,
        "legislation_referenced": sorted(leg)[:20],
        "cases_referenced": sorted(cases)[:20],
        "full_text": text,
    }


def _extract_json(content: str) -> dict | None:
    """Parse JSON from model output: greedy braces first, then first-{/last-} slice."""
    if not content or not content.strip():
        return None
    for candidate in (re.search(r"\{.*\}", content, re.DOTALL), None):
        if candidate:
            try:
                return json.loads(candidate.group())
            except json.JSONDecodeError:
                pass
    s, e = content.find("{"), content.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(content[s:e + 1])
        except json.JSONDecodeError:
            pass
    return None


def ai_summarize(text: str, max_text: int = 8000) -> dict:
    """Send ruling text to DeepSeek V4 Flash and get structured summary.

    Retries once on parse failure or transient API error. max_tokens is 4000 —
    earlier 2000 cap truncated long summaries mid-JSON (325 error stubs, Aug 2026).
    """
    if len(text) > max_text:
        text = text[:max_text] + "\n... [truncated]"

    payload = {
        "model": "deepseek-chat",  # NOT v4-flash: flash burns max_tokens on reasoning_content, returns empty content
        "messages": [{"role": "user", "content": SUMMARY_PROMPT + text}],
        "temperature": 0.1,
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    last_finish = ""
    for attempt in (1, 2):
        if attempt == 2 and last_finish == "length":
            payload["max_tokens"] = 8000  # truncation: give the model more room
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            finish = result["choices"][0].get("finish_reason", "")
            content = result["choices"][0]["message"]["content"]
            if not content:
                raise ValueError("null content")
            summary = _extract_json(content)
            if summary is not None:
                return summary
            if attempt == 1:
                last_finish = finish
                time.sleep(2)
                continue
            return {"error": "JSON parse failed", "raw": content[:300]}
        except Exception as e:
            if attempt == 1:
                last_finish = ""
                time.sleep(5)
                continue
            return {"error": f"API failed: {e}", "raw": ""}


def process_aid(files: list[Path], label: str):
    """Process ATO ID files with regex extraction."""
    total = len(files)
    done = 0
    errors = 0
    for f in files:
        citation = parse_citation(f.name, "AID")
        out_path = OUTPUT_DIR / f"{citation.replace(' ', '_').replace('/', '_')}.json"
        if out_path.exists():
            done += 1
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            summary = extract_aid(text, citation)
            out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
            done += 1
        except Exception as e:
            print(f"  [{label}] ERROR {citation}: {e}")
            errors += 1
        if total > 100 and (done + errors) % 500 == 0:
            print(f"  [{label}] {done + errors}/{total} ({errors} errors)")
    print(f"  [{label}] Done: {done} OK, {errors} errors / {total}")


def process_full(files: list[Path], label: str, skip_existing: bool = True):
    """Process full ruling files with AI summarization."""
    total = len(files)
    done = 0
    errors = 0
    for i, f in enumerate(files):
        citation = parse_citation(f.name, f.stem.split("_", 1)[0])
        out_path = OUTPUT_DIR / f"{citation.replace(' ', '_').replace('/', '_')}.json"
        if skip_existing and out_path.exists():
            done += 1
            continue

        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            result = ai_summarize(text)

            if "error" in result:
                print(f"  [{label}] ERROR {citation}: {result.get('error')}")
                errors += 1
                # Write error summary to avoid reprocessing
                result["citation"] = citation
                result["status"] = extract_status(text)
                out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                # LK-03: deterministically top-up cases_referenced / related_rulings
                # from the ruling's References section — the LLM routinely
                # undercounts cases and leaves related_rulings empty.
                refs = parse_references_section(text)
                for key in ("cases_referenced", "related_rulings"):
                    merged = list(result.get(key) or [])
                    have = {re.sub(r"\s+", " ", str(x)).strip().lower() for x in merged}
                    for item in refs[key]:
                        if item.lower() not in have:
                            merged.append(item)
                            have.add(item.lower())
                    result[key] = merged
                result["citation"] = result.get("citation", citation)
                out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                done += 1
        except Exception as e:
            print(f"  [{label}] EXCEPTION {citation}: {e}")
            errors += 1

        if (i + 1) % 50 == 0:
            elapsed = time.time() - _t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{label}] {i+1}/{total} ({errors} errors) "
                  f"@ {rate:.1f}/s, ETA {remaining/60:.0f}m")

    print(f"  [{label}] Done: {done} OK, {errors} errors / {total}")


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate ruling summaries")
    parser.add_argument("--type", choices=["aid", "full"], required=True)
    parser.add_argument("--types", help="Comma-separated types to process (e.g. TR,TD)")
    parser.add_argument("--slice-idx", type=int, default=None, help="Slice index (0-based)")
    parser.add_argument("--slice-total", type=int, default=None, help="Total slices")
    parser.add_argument("--no-skip", action="store_true", help="Re-process even if exists")
    args = parser.parse_args()

    _t0 = time.time()

    if args.type == "aid":
        files = get_ruling_files("AID")
        print(f"[AID] Processing {len(files)} ATO IDs...")
        process_aid(files, "AID")

    else:
        types = [t.strip() for t in args.types.split(",")] if args.types else list(ALL_TYPES.keys())
        all_files = []
        for t in types:
            all_files.extend(get_ruling_files(t))

        # Apply slice if specified
        if args.slice_idx is not None and args.slice_total is not None:
            total = len(all_files)
            chunk_size = total // args.slice_total
            remainder = total % args.slice_total
            start = args.slice_idx * chunk_size + min(args.slice_idx, remainder)
            end = start + chunk_size + (1 if args.slice_idx < remainder else 0)
            all_files = all_files[start:end]
            label = f"FULL [{args.slice_idx + 1}/{args.slice_total}] {','.join(types)}"
        else:
            label = f"FULL {','.join(types)}"

        print(f"[{label}] Processing {len(all_files)} rulings...")
        process_full(all_files, label, skip_existing=not args.no_skip)