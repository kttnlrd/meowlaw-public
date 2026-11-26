#!/usr/bin/env python3
"""CDN-0188: reflow treaty article bodies flattened by the v3.1 reconstruction.

The reconstruction commit (0cf39fb08) collapsed fragment-per-line article
bodies into one giant line per file. The pre-commit fragments preserved the
paragraph/subparagraph boundaries ('1.', 'a)', etc. on their own lines), so
we reflow FROM THE FRAGMENTS: join fragment lines into clean paragraphs,
recovering quote and apostrophe joins without spurious spaces.

Usage:
    python3 scripts/reflow_treaty_articles.py --article new-zealand/article-04-resident
    python3 scripts/reflow_treaty_articles.py --all-nz
    python3 scripts/reflow_treaty_articles.py --all            # affected set
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTICLES = REPO / "data" / "treaties"

PARA_MARK = re.compile(r"^(\d+\.|[a-e]\)|[ivx]+\))$")


def reflow_fragments(fragments: str) -> str:
    """Turn fragment-per-line text into clean paragraph-per-line markdown.

    Tokens that are exactly a quote ('"') or apostrophe ("'") bind to their
    neighbour WITHOUT spaces ('"word"', "individual's"); every other fragment
    joins the paragraph with a single space.
    """
    paras = []
    cur: list[str] = []
    for raw in fragments.splitlines():
        s = raw.strip()
        if not s:
            continue
        if PARA_MARK.match(s):
            if cur:
                paras.append(cur)
            cur = [s]
        else:
            cur.append(s)
    if cur:
        paras.append(cur)

    def join_tokens(toks: list[str]) -> str:
        # naive join with single spaces
        s = " ".join(t for t in toks if t).strip()
        # Split on quotes; quotes alternate open/close. Remove spaces inside
        # the quotes ('term " resident' -> 'term "resident'), keep a single
        # space on the OUTSIDE of each quote.
        parts = re.split(r'(")', s)
        out = ""
        q_idx = 0  # 0=outside, 1=inside (opened)
        for p in parts:
            if p == '"':
                # trim space on the inside side before flipping state
                if q_idx == 0:  # opening quote: trim following space
                    out = out.rstrip()
                    out += ' "'
                    q_idx = 1
                else:           # closing quote: trim trailing inside space
                    out = out.rstrip()
                    out += '"'
                    q_idx = 0
            elif q_idx == 1:
                out += p.lstrip()
            else:
                out += (" " + p.strip()) if out else p.strip()
        # apostrophe fragments join tight both sides
        out = re.sub(r"\s+'", "'", out)
        out = re.sub(r"'\s+", "'", out)
        return out

    cleaned = []
    for toks in paras:
        para = join_tokens(toks)
        # drop chrome lines that belonged to the old body header
        low = para.strip().lower()
        if re.match(r"^[-–]+\s+[a-z ]+$", low):
            continue
        if low.startswith("-") and len(low) < 45 and not re.search(r"\d", low):
            continue
        if low == "definitions" or low.startswith("definitions resident"):
            continue
        if low in ("oecd", "commentary", "resident", "article"):
            continue
        if low.endswith("royalties") and low.startswith(("taxation of income", "- taxation")) :
            continue
        para = re.sub(r"^\s*[-–]+\s*$", "", para).strip()
        if para:
            cleaned.append(para)
    return "\n\n".join(cleaned) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--article", help="relative path under data/treaties, e.g. new-zealand/article-04-resident")
    ap.add_argument("--all-nz", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.article:
        targets = [ARTICLES / (args.article + ".md")]
    elif args.all_nz:
        targets = sorted((ARTICLES / "new-zealand" / "articles").glob("*.md"))
    elif args.all:
        targets = sorted(ARTICLES.glob("*/articles/*.md"))
    else:
        print("specify --article PATH | --all-nz | --all", file=sys.stderr)
        return 2

    changed = 0
    for p in targets:
        if not p.exists():
            print(f"missing: {p}", file=sys.stderr)
            continue
        cur = p.read_text(errors="replace")
        fm_match = re.match(r"^---\n.*?\n---\n", cur, re.DOTALL)
        if not fm_match:
            continue
        # CDN-0188 scope: only files whose body is one giant flattened line
        # (the v3.1 reconstruction regression). Already-formatted files keep
        # their existing structure.
        body = cur[fm_match.end():].strip()
        nlines = [l for l in body.splitlines() if l.strip()]
        if len(nlines) > 1 or len(body) < 200:
            continue
        rel_git = str(p.relative_to(REPO))
        r = subprocess.run(["git", "show", f"0cf39fb08^:{rel_git}"],
                           capture_output=True, text=True, cwd=REPO)
        if r.returncode != 0:
            continue
        frag = r.stdout
        fm2 = re.match(r"^---\n.*?\n---\n", frag, re.DOTALL)
        frag_body = frag[fm2.end():] if fm2 else frag
        new_body = reflow_fragments(frag_body)
        if not new_body.strip():
            continue
        new_text = cur[:fm_match.end()] + new_body
        if new_text == cur:
            continue
        if args.dry_run:
            print(f"WOULD reflow: {rel_git}")
            changed += 1
            continue
        p.write_text(new_text)
        changed += 1
    print(f"done: {changed} files {'(dry-run)' if args.dry_run else 'reflowed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
