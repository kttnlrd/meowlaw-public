#!/usr/bin/env python3
"""Fix cosmetic data issues across the corpus, per type.

Types (matching verify_data_integrity.py ARTIFACT_CHECKS):
  double_space_marker  collapse 2+ literal spaces after **(n)** markers to 1
                       (markdown subsection markers; corpus standard is one space)
  smart_quote          normalise curly quotes to straight ASCII
                       (U+2018/2019 -> ', U+201C/201D -> ")

Scope:
  sections .md           both types
  rulings .txt           smart_quote
  rulings summaries .json smart_quote
  private rulings .json  smart_quote

Usage:
  python3 fix_cosmetic_issues.py --dry-run
  python3 fix_cosmetic_issues.py --type smart_quote --dry-run
  python3 fix_cosmetic_issues.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRIVATE = Path.home() / ".hermes" / "private_rulings" / "data" / "json"

DOUBLE_SPACE_RE = re.compile(r"(\*\*\([a-z0-9]+\)\*\*) {2,}")
SMART_QUOTE_RE = re.compile(r"[\u2018\u2019\u201c\u201d]")
SMART_MAP = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}


def fix_double_space(text: str) -> str:
    return DOUBLE_SPACE_RE.sub(r"\1 ", text)


def fix_smart_quote(text: str) -> str:
    return SMART_QUOTE_RE.sub(lambda m: SMART_MAP[m.group(0)], text)


def fix_smart_quote_json(text: str) -> str:
    """Normalise curly quotes inside JSON string VALUES only.

    Raw-text replacement is unsafe for JSON: curly quotes may appear inside
    string values where a straight quote would break structure (curly quotes
    are not JSON delimiters, so they were valid as content). Parse, walk the
    values, and re-dump so any resulting straight quotes are properly escaped.
    """
    data = json.loads(text)
    changed = False

    def walk(obj):
        nonlocal changed
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    nv = SMART_QUOTE_RE.sub(lambda m: SMART_MAP[m.group(0)], v)
                    if nv != v:
                        obj[k] = nv
                        changed = True
                else:
                    walk(v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str):
                    nv = SMART_QUOTE_RE.sub(lambda m: SMART_MAP[m.group(0)], v)
                    if nv != v:
                        obj[i] = nv
                        changed = True
                else:
                    walk(v)

    walk(data)
    if not changed:
        return text
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def repair_json_quotes(text: str) -> str | None:
    """Repair JSON broken by unescaped straight quotes inside string values.

    Naive curly->straight normalisation turns safe content quotes (curly is
    not a JSON delimiter) into structural breaks. This parser walks the text
    and at each ambiguous in-string '"' tries CLOSE vs CONTENT, backtracking
    on parse failure, restoring the content quotes to curly. Returns repaired
    text, or None if no consistent interpretation exists.

    Ambiguity resolution is schema-aware: prose-bearing keys (facts, qa_pairs,
    question, answer, formatted_text, ...) keep strings open (content-first),
    while metadata keys (legislation/case refs, periods) are strict. This
    mirrors the private-ruling JSON schema where prose values legitimately
    carry quoted phrases ("X", "Y" and "Z".) inside one string.
    """
    PROSE_KEYS = {
        "name", "subject", "facts", "reasons_for_decision", "qa_pairs",
        "question", "answer", "formatted_text",
    }
    n = len(text)
    budget = 50_000_000

    def ws(i):
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    KEY_BOUNDARY_RE = re.compile(r'^\s*,\s*"[^"]{1,60}"\s*:')
    KEY_BOUNDARY_RE2 = re.compile(r'^\s*"[^"]{1,60}"\s*:')

    def _looks_like_key_boundary(qpos: int) -> bool:
        """True if the quote at qpos is a value-end delimiter followed by a key
        (e.g. `",\n  "date_of_advice":`). Content curly quotes never sit right
        before a `"key":` pattern."""
        tail = text[qpos + 1:qpos + 120]
        return bool(KEY_BOUNDARY_RE.match(tail) or KEY_BOUNDARY_RE2.match(tail))

    def parse_string(i, repairs, is_key=False, strict=False, container="top"):
        """i at opening quote. Returns (end, repairs) or None."""
        nonlocal budget
        i += 1
        while i < n:
            budget -= 1
            if budget < 0:
                return None
            c = text[i]
            if c in "\r\n" or (ord(c) < 32 and c != "\t"):
                return None  # raw control chars are invalid inside JSON strings
            if c == "\\":
                i += 2
                continue
            if c == '"':
                if is_key or strict:
                    # keys are fixed schema; metadata values are short and
                    # never carry curly-quote prose
                    r = parse_after_string(i + 1, repairs, is_key, container)
                    if r is not None:
                        return r
                    return None
                # prose: try CONTENT first (these were curly quotes); the
                # original structure keeps strings open as long as possible
                r = parse_string(i + 1, repairs + [i], is_key, False, container)
                if r is not None:
                    return r
                # try CLOSE
                r = parse_after_string(i + 1, repairs, is_key, container)
                if r is not None:
                    return r
                return None
            i += 1
        return None

    def parse_value(i, repairs, prose=False, container="top"):
        i = ws(i)
        if i >= n:
            return None
        c = text[i]
        if c == '"':
            return parse_string(i, repairs, False, strict=not prose, container=container)
        if c == "{":
            return parse_object(i, repairs)
        if c == "[":
            return parse_array(i, repairs, prose)
        # scalar
        while i < n and text[i] not in ",]}\"' \t\r\n":
            i += 1
        return (i, repairs)

    def parse_object(i, repairs):
        i = ws(i + 1)
        if i < n and text[i] == "}":
            return (i + 1, repairs)
        while True:
            i = ws(i)
            if i >= n or text[i] != '"':
                return None
            key_start = i + 1
            r = parse_string(i, repairs, is_key=True)
            if r is None:
                return None
            i, repairs = r  # i is AT the colon
            key_text = text[key_start:i].strip().rstrip('"')
            i = ws(i)
            if i >= n or text[i] != ":":
                return None
            prose = key_text in PROSE_KEYS
            r = parse_value(i + 1, repairs, prose=prose, container="obj")
            if r is None:
                return None
            i, repairs = r
            i = ws(i)
            if i >= n:
                return None
            if text[i] == ",":
                i += 1
                continue
            if text[i] == "}":
                return (i + 1, repairs)
            return None

    def parse_array(i, repairs, prose=False):
        i = ws(i + 1)
        if i < n and text[i] == "]":
            return (i + 1, repairs)
        while True:
            r = parse_value(i, repairs, prose=prose, container="arr")
            if r is None:
                return None
            i, repairs = r
            i = ws(i)
            if i >= n:
                return None
            if text[i] == ",":
                i += 1
                continue
            if text[i] == "]":
                return (i + 1, repairs)
            return None

    def parse_after_string(i, repairs, is_key=False, container="top"):
        """After a string closes: key -> ':', value -> , } ] or EOF."""
        i = ws(i)
        if i >= n:
            return (i, repairs) if container == "top" else None
        c = text[i]
        if is_key:
            if c == ":":
                return (i, repairs)  # leave position AT the colon; caller checks it
            return None
        if c == ",":
            return (i, repairs)  # leave position AT the comma; caller advances
        if c == "}" and container in ("obj", "top"):
            return (i, repairs)
        if c == "]" and container in ("arr", "top"):
            return (i, repairs)
        return None

    res = parse_value(0, [])
    if res is None:
        return None
    end, repairs = res
    if ws(end) != n:
        return None

    out = list(text)
    for pos in repairs:
        prev = text[pos - 1] if pos > 0 else " "
        curly = "\u201d" if (prev.isalnum() or prev in ".,;:!?)") else "\u201c"
        out[pos] = curly
    return "".join(out)


FIXERS = {
    "double_space_marker": fix_double_space,
    "smart_quote": fix_smart_quote,
}


def collect_files() -> list[Path]:
    files = []
    # sections .md
    for p in DATA.rglob("*.md"):
        if "/sections/" in str(p):
            files.append(p)
    # rulings .txt + summaries .json
    rulings = DATA / "rulings"
    if rulings.is_dir():
        files.extend(sorted(rulings.glob("*.txt")))
        sm = rulings / "summaries"
        if sm.is_dir():
            files.extend(sorted(sm.glob("*.json")))
    # private rulings .json
    if PRIVATE.is_dir():
        files.extend(sorted(PRIVATE.glob("*.json")))
    return files


def types_for(path: Path) -> list[str]:
    if "/sections/" in str(path):
        return ["double_space_marker", "smart_quote"]
    return ["smart_quote"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Fix cosmetic data issues per type")
    ap.add_argument("--type", choices=sorted(FIXERS), default=None,
                    help="limit to one type (default: all applicable per file)")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="report only, no writes (default)")
    ap.add_argument("--apply", action="store_true",
                    help="write fixes to disk")
    args = ap.parse_args()

    files = collect_files()
    counts = Counter()
    by_dir = Counter()
    changed_files = 0
    examples = {}

    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        new = text
        for t in types_for(p):
            if args.type and t != args.type:
                continue
            if t == "smart_quote" and p.suffix == ".json":
                try:
                    fixed = fix_smart_quote_json(new)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue  # leave unparseable files alone
            else:
                fixed = FIXERS[t](new)
            if fixed != new:
                n = len(DOUBLE_SPACE_RE.findall(new)) if t == "double_space_marker" \
                    else len(SMART_QUOTE_RE.findall(new))
                counts[t] += n
                if "/sections/" in str(p):
                    group = str(p.relative_to(DATA)).split("/")[0]
                else:
                    group = p.parent.name
                by_dir[(t, group)] += n
                if t not in examples:
                    rel = str(p.relative_to(ROOT)) if str(p).startswith(str(ROOT)) else str(p)
                    examples[t] = (rel, n)
                new = fixed
        if new != text:
            changed_files += 1
            if args.apply:
                p.write_text(new, encoding="utf-8")

    print("=" * 60)
    print(f"files scanned: {len(files)}  files changed: {changed_files}  mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    for (t, d), n in sorted(by_dir.items()):
        print(f"  {t:22s} {d:30s} {n}")
    print("=" * 60)
    for t, (ex, n) in examples.items():
        print(f"example [{t}]: {ex} ({n} matches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
