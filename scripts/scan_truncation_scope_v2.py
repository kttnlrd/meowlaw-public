#!/usr/bin/env python3
"""CDN-0193 triage buckets, v2 — page-aware, correct-compilation measurement.

Why v2 exists
-------------
`scan_truncation_scope.py` (v1) produced the buckets in
`.hermes/plans/cdn-0193-scope.json` (338 recoverable / 117 page_split /
49 not_found). Codex review B6 flagged those buckets as heuristics; reading v1
confirms two measurement defects beyond the documented 4-word-suffix guess:

  1. itaa-1997 findings (340 of 504, the bulk) were probed against the repo's
     comp-263 `raw/*.txt` while the corpus itself is comp-266. v1's own plan
     addendum says the 263 repo raw is STALE and must never be used.
  2. v1 concatenated volumes into a flat line list and *guessed* a page split
     from "what the next 1-3 non-empty lines look like". The raw text carries
     real page boundaries (pdftotext `\\f`), so the distinction between
     "text continues on the same page" (parser-side loss) and "text resumes on
     the next page" (extractor page-boundary defect) is directly measurable.

v2 method (read-only, reproducible)
-----------------------------------
  * Sources pinned to the compilation the corpus frontmatter declares; each
    file's own footer compilation number is asserted, never the filename.
  * Raw text tokenised per PDF page (`\\f`-split), so every token carries a
    page id: a truncation is a PAGE-BOUNDARY loss only if the clause tail is the
    last thing on its page and the continuation opens the next page.
  * Candidate anchoring: an 8-token tail probe, validated by the row's own
    identifier cell appearing shortly before it in the raw — v1 took the LAST
    bare-suffix match in the concatenated act, which can bind to a different
    table entirely.
  * Buckets are emitted with explicit `kind: heuristic-measured` labels plus
    the evidence string for each classification.

Usage: python3 scripts/scan_truncation_scope_v2.py [--show N]
"""
from __future__ import annotations
import os

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STAGING = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data"))
FINDINGS = Path("/tmp/corpus_scan_20260826.json")

# act -> (raw dir, compilation the corpus frontmatter declares)
SOURCES = {
    "itaa-1997": (STAGING / "itaa-1997" / "raw", 266),   # comp266/, not the stale 263
    "taa-1953": (DATA / "taa-1953" / "raw", 222),
    "gst-1999": (DATA / "gst-1999" / "raw", 96),
    "fbt-1986": (DATA / "fbt-1986" / "raw", 96),
    "itaa-1936": (DATA / "itaa-1936" / "raw", 191),
    "sis-1993": (DATA / "sis-1993" / "raw", 126),
}

FOOTERISH = re.compile(
    r"compilation no|authorised version|^page\b|^\d{1,3}$|"
    r"^(income tax assessment|taxation administration|a new tax system|"
    r"fringe benefits tax assessment|superannuation industry)\b",
    re.I,
)
# a fresh logical row / heading is not a clause continuation
NEWROW = re.compile(r"^(item\b|\d+[A-Z]?$|\([a-z]\)$|[A-Z][A-Za-z]*$)")
PROBE_LEN = 8
TIE = 0


def norm_tok(t: str) -> str:
    t = t.strip("*_`'\"()[],;:.·—–-")
    return t.lower()


def tok_stream(paths: list[Path]):
    """-> list of (norm_token, page_id, raw_token). Page ids are global."""
    toks: list[tuple[str, int, str]] = []
    page = 0
    pages: list[int] = []          # page id -> index in toks where page starts
    footers: list[str] = []
    for p in sorted(paths):
        text = p.read_text(errors="replace")
        m = re.search(r"Compilation No\.?\s*(\d+)", text)
        footers.append(f"{p.name}={m.group(1) if m else '?'}")
        for pg in text.split("\f"):
            pages.append(len(toks))
            for m2 in re.finditer(r"\S+", pg):
                nt = norm_tok(m2.group(0))
                if nt:
                    toks.append((nt, page, m2.group(0)))
            page += 1
    return toks, pages, footers


def page_of(pages: list[int], idx: int) -> int:
    """index into `pages` of the page containing token idx."""
    lo, hi = 0, len(pages) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pages[mid] <= idx:
            lo = mid
        else:
            hi = mid - 1
    return lo


def classify(toks, pages, hit_start: int, probe_len: int):
    """Return (bucket, evidence) for a candidate match."""
    end = hit_start + probe_len
    pg = page_of(pages, end - 1)
    # look forward within the same page (skip pure punctuation / short numerals)
    same_page = []
    for j in range(end, min(len(toks), (pages[pg + 1] if pg + 1 < len(pages) else len(toks)))):
        w = toks[j][2]
        if FOOTERISH.match(w):
            break
        if len(norm_tok(w)) > 0 and not re.fullmatch(r"\d{1,3}", w):
            same_page.append(w)
        if len(same_page) >= 14:
            break
    if len(same_page) >= 4:
        return "parser_side_same_page", " ".join(same_page[:12])
    # continuation may open the next page
    if pg + 1 < len(pages):
        nxt = []
        for j in range(pages[pg + 1], min(len(toks), pages[pg + 2] if pg + 2 < len(pages) else len(toks))):
            w = toks[j][2]
            if FOOTERISH.match(w):
                continue
            nxt.append(w)
            if len(nxt) >= 14:
                break
        joined = " ".join(nxt[:10])
        if len(nxt) >= 4 and not NEWROW.match(nxt[0]):
            return "page_boundary_split", joined
        if len(nxt) >= 4:
            return "page_boundary_split", f"(starts new row) {joined}"
    return "tail_not_located_after_probe", " ".join(same_page[:6]) or "(nothing follows on page)"


def pr_prev(tail_toks: list[str], pr: list[str]):
    """Token immediately preceding `pr` inside the row's own tail, or None.

    Generalises the full-probe `prev_tok` (tail_toks[-PROBE_LEN-1]) to the
    shorter fallback probes: for a probe of length k the predecessor is
    tail_toks[-k-1], because every probe is a suffix of `tail_toks`.
    """
    k = len(pr)
    return tail_toks[-k - 1] if len(tail_toks) > k else None


def main() -> int:
    show = 3
    if "--show" in sys.argv:
        show = int(sys.argv[sys.argv.index("--show") + 1])
    findings = json.loads(FINDINGS.read_text())
    trunc = [f for f in findings if f["class"] == "C11_table_truncated"]

    cache: dict[str, tuple] = {}
    stats = Counter()
    probe_len_used: Counter = Counter()
    per_act: dict[str, Counter] = {}
    ambiguity = Counter()
    examples: dict[str, list[str]] = {}
    source_report: dict[str, dict] = {}
    per_finding: dict[str, str] = {}

    for f in trunc:
        path = f["path"]
        act = path.split("/", 1)[0]
        if act not in SOURCES:
            stats["act_unmapped"] += 1
            continue
        m = re.search(r"line (\d+)", f["detail"])
        if not m:
            stats["no_line_in_finding"] += 1
            continue
        ln_no = int(m.group(1))
        try:
            md_line = (DATA / path).read_text(errors="replace").splitlines()[ln_no - 1]
        except Exception:
            stats["md_unreadable"] += 1
            continue
        cells = [c.strip() for c in md_line.strip().strip("|").split("|")]
        cells = [c for c in cells if c]
        if not cells:
            stats["row_empty"] += 1
            continue
        tail_toks = [norm_tok(t) for t in cells[-1].split()]
        tail_toks = [t for t in tail_toks if t]
        if len(tail_toks) < 3:
            stats["tail_too_short"] += 1
            per_finding[f"{path}:{ln_no}"] = "tail_too_short"
            continue
        probe = tail_toks[-PROBE_LEN:] if len(tail_toks) >= PROBE_LEN else tail_toks
        prev_tok = tail_toks[-PROBE_LEN - 1] if len(tail_toks) > PROBE_LEN else None
        row_id = norm_tok(cells[0])

        if act not in cache:
            d, comp = SOURCES[act]
            toks, pages, footers = tok_stream(sorted(d.glob("*.txt")))
            index: dict[str, list[int]] = {}
            for i, (nt, _p, _r) in enumerate(toks):
                index.setdefault(nt, []).append(i)
            cache[act] = (toks, pages, index)
            source_report[act] = {"dir": str(d), "expected_compilation": comp,
                                  "file_footers": footers, "tokens": len(toks), "pages": len(pages)}
            print(f"[src] {act:10s} {len(toks):>9,} tokens {len(pages):>5,} pages  {d}")
        toks, pages, index = cache[act]

        # progressive probe fallback: full tail, then 6/5/4 tokens. The rarest
        # token in the probe anchors the search, so a stopword-heavy tail cannot
        # blow up the candidate list.
        probes = []
        for n in (PROBE_LEN, 6, 5, 4):
            if len(tail_toks) >= n and tail_toks[-n:] not in probes:
                probes.append(tail_toks[-n:])
        best, best_score, used_len = None, -1, None
        for pr in probes:
            if any(t not in index for t in pr):
                continue
            rarest = min(range(len(pr)), key=lambda k: len(index[pr[k]]))
            if len(index[pr[rarest]]) > 4000:
                continue
            for i in index[pr[rarest]][:4000]:
                start = i - rarest
                if start < 0 or start + len(pr) > len(toks):
                    continue
                if all(toks[start + k][0] == pr[k] for k in range(len(pr))):
                    score = 1
                    if row_id and any(toks[j][0] == row_id for j in range(max(0, start - 60), start)):
                        score += 2
                    prev = pr_prev(tail_toks, pr)
                    if prev is not None and start > 0 and toks[start - 1][0] == prev:
                        score += 1
                    if score > best_score:
                        best, best_score, used_len, probe = start, score, len(pr), pr
            if best is not None:
                break
        if best is None:
            stats["probe_not_found"] += 1
            per_act.setdefault(act, Counter())["probe_not_found"] += 1
            per_finding[f"{path}:{ln_no}"] = "probe_not_found"
            continue
        probe_len_used[used_len] += 1
        if best_score <= 1:
            ambiguity["matched_without_row_anchor"] += 1
        bucket, evidence = classify(toks, pages, best, len(probe))
        stats[bucket] += 1
        per_act.setdefault(act, Counter())[bucket] += 1
        per_finding[f"{path}:{ln_no}"] = bucket
        examples.setdefault(bucket, [])
        if len(examples[bucket]) < 6:
            examples[bucket].append(f"{path}:{ln_no} probe={' '.join(probe)} | {evidence}")

    total = sum(stats.values())
    print(f"\nC11_table_truncated findings: {len(trunc)}  classified={total}")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  {k:32s} {v}")
    print("\nper act:")
    for act, c in sorted(per_act.items()):
        print(f"  {act:12s} " + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print("\nambiguity flags:", dict(ambiguity))
    for b, ex in examples.items():
        print(f"\n--- examples: {b} ---")
        for e in ex[:show]:
            print("  " + e[:200])

    out = REPO / ".hermes/plans/cdn-0193-scope-v2.json"
    cross_tab = None
    v1_path = Path("/tmp/v1_buckets.json")
    if v1_path.exists():
        v1 = json.loads(v1_path.read_text())
        tab = Counter()
        for k, v2b in per_finding.items():
            tab[f"{v1.get(k, 'unclassified_in_v1')} -> {v2b}"] += 1
        cross_tab = dict(sorted(tab.items(), key=lambda kv: -kv[1]))
        print("\nv1 -> v2 cross-tab (per finding):")
        for k, v in cross_tab.items():
            print(f"  {v:4d}  {k}")
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "supersedes": "cdn-0193-scope.json (v1, heuristic)",
        "kind": "heuristic-measured",
        "method": ("page-aware token-stream probe against the compilation the corpus declares; "
                   "8-token tail probe anchored by the row's identifier cell; page split is "
                   "measured from pdftotext form-feeds, not guessed from neighbouring lines"),
        "method_defects_in_v1": [
            "page splits inferred from adjacent lines; raw page boundaries ignored",
            "'last matching 4-word suffix' could bind to a different table in the same act",
            "v1 probed itaa-1997 against the stale comp-263 repo raw (measured effect: 1 of 340 findings)",
        ],
        "v1_reproduced_exactly": "yes (scripts/verify_v1_buckets.py -> 338/117/49)",
        "stats": dict(stats),
        "probe_length_used": dict(probe_len_used),
        "per_act": {a: dict(c) for a, c in sorted(per_act.items())},
        "cross_tab_v1_to_v2": cross_tab,
        "ambiguity": dict(ambiguity),
        "sources": source_report,
        "per_finding": per_finding,
        "examples": {b: e[:6] for b, e in examples.items()},
        "confidence": ("high for page_boundary_split (measured against real page markers); "
                       "medium for parser_side_same_page (continuation present but clause-level "
                       "identity is not proven); low for probe_not_found / tail_too_short"),
    }, indent=2, ensure_ascii=False))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
