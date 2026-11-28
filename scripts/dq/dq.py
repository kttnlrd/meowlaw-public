#!/usr/bin/env python3
"""LawKitty data-quality pipeline: find and repair text defects across the corpus, ML-assisted.

    python dq.py run --data DATA_DIR [--pr PRIVATE_RULINGS_JSON_DIR] --out RUN_DIR [--limit N]
    python dq.py <phase> --out RUN_DIR ...      phases: inventory learn features model repair rescore report

Reads the corpus in place and NEVER writes to it. Everything goes under RUN_DIR:
  docs.jsonl        one row per document (corpus, path, kind)
  vocab.json        word frequencies;  lm.pkl  character 4-gram language model
  linefreq.pkl      per-corpus document frequency of each (normalised) line
  features.jsonl    ~25 quality signals per document (+ MinHash for near-duplicates)
  scores.jsonl      weak labels, isolation-forest anomaly score, classifier probability
  files/            repaired copies of changed documents (same relative paths)
  manifest.jsonl    one row per repaired document: fixes applied, gate results, before/after
  review.jsonl      documents that need a person or a re-fetch (not auto-fixable)
  REPORT.md         the morning summary
Every phase is resumable: a phase whose output exists is skipped unless --force.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import hashlib
import html
import json
import math
import os
import pickle
import random
import re
import sys
import time
import unicodedata
from multiprocessing import Pool
from pathlib import Path

WORKERS = max(1, (os.cpu_count() or 2) - 1)
LOG = None

# ----------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def jl_write(path: Path, rows) -> int:
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def jl_read(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


_FM = re.compile(r"\A---\n.*?\n---\n?", re.S)
PR_FIELDS = ("name", "subject", "facts", "reasons_for_decision", "applies_for_periods",
             "scheme_commenced", "qa_pairs", "relevant_legislation", "case_references", "formatted_text")


def read_doc(doc: dict) -> tuple[str, str, object]:
    """(head, body, raw): head is front matter kept verbatim, body the text we judge and repair."""
    p = doc["abs"]
    if doc["kind"] == "prjson":
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        return "", "\n".join(pr_strings(raw)), raw
    text = Path(p).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if doc["kind"] == "md":
        m = _FM.match(text)
        if m:
            return text[:m.end()], text[m.end():], None
    return "", text, None


def pr_strings(raw) -> list[str]:
    out = []
    if not isinstance(raw, dict):
        return out
    for k in PR_FIELDS:
        v = raw.get(k)
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    out += [y for y in x.values() if isinstance(y, str)]
    return out


def map_pr_strings(raw, fn):
    for k in PR_FIELDS:
        v = raw.get(k)
        if isinstance(v, str):
            raw[k] = fn(v)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, str):
                    v[i] = fn(x)
                elif isinstance(x, dict):
                    for kk, y in x.items():
                        if isinstance(y, str):
                            x[kk] = fn(y)
    return raw

# ----------------------------------------------------------------------------- inventory

SKIP_DIRS = {"embeddings", "case_texts", "summaries", "summaries_broken", "summaries_errors_backup",
             "pre1105", "raw", "source", "paragraphs"}


def inventory(data: Path, pr: Path | None, limit: int | None) -> list[dict]:
    docs = []
    for corpus_dir in sorted(p for p in data.iterdir() if p.is_dir()):
        c = corpus_dir.name
        for pattern, kind in (("**/*.md", "md"), ("*.txt", "txt"), ("texts/*.txt", "txt")):
            for f in corpus_dir.glob(pattern):
                rel = f.relative_to(data)
                if any(part in SKIP_DIRS for part in rel.parts[1:-1]) or f.is_symlink():
                    continue
                if f.name.lower() in ("readme.md",):
                    continue
                docs.append({"id": str(rel), "corpus": c, "kind": kind, "abs": str(f), "rel": str(rel)})
    if pr and pr.is_dir():
        for f in sorted(pr.glob("*.json")):
            docs.append({"id": f"private-rulings/{f.name}", "corpus": "private-rulings", "kind": "prjson",
                         "abs": str(f), "rel": f"private-rulings/{f.name}"})
    seen, out = set(), []
    for d in docs:
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    if limit:
        random.Random(7).shuffle(out)
        out = out[:limit]
    return out

# ----------------------------------------------------------------------------- learn: vocab, LM, line freq

WORD = re.compile(r"[A-Za-z]+")
LM_N = 4


def norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))


def _learn_worker(args):
    doc, sample_lm = args
    try:
        _, body, _ = read_doc(doc)
    except Exception:
        return None
    words = collections.Counter(w.lower() for w in WORD.findall(body))
    lines = {hashlib.blake2b(norm_line(l).encode(), digest_size=8).digest()
             for l in body.split("\n") if 3 <= len(l.strip()) <= 160}
    grams = None
    if sample_lm:
        t = "\x02" + re.sub(r"[ \t]+", " ", body)[:20000]
        grams = collections.Counter(t[i:i + LM_N] for i in range(len(t) - LM_N + 1))
    return doc["corpus"], words, lines, grams


def learn(docs: list[dict], out: Path) -> None:
    rng = random.Random(11)
    # LM sample: every corpus contributes, capped so no corpus dominates
    by_c = collections.defaultdict(list)
    for d in docs:
        by_c[d["corpus"]].append(d)
    lm_ids = set()
    for c, ds in by_c.items():
        for d in rng.sample(ds, min(len(ds), 800)):
            lm_ids.add(d["id"])
    vocab = collections.Counter()
    grams = collections.Counter()
    linefreq = collections.defaultdict(collections.Counter)
    ndocs = collections.Counter()
    with Pool(WORKERS) as pool:
        for i, r in enumerate(pool.imap_unordered(_learn_worker, ((d, d["id"] in lm_ids) for d in docs), chunksize=64)):
            if r is None:
                continue
            c, w, lines, g = r
            vocab.update(w)
            ndocs[c] += 1
            linefreq[c].update(lines)
            if g:
                grams.update(g)
            if i % 20000 == 0:
                log(f"learn: {i}/{len(docs)}")
    # prune: keep line hashes seen in >= 3 docs (the only ones that can be boilerplate)
    lf = {c: {h: n for h, n in cnt.items() if n >= 3} for c, cnt in linefreq.items()}
    ctx = collections.Counter()
    for g, n in grams.items():
        ctx[g[:-1]] += n
    V, k = len({ch for g in grams for ch in g}), 0.1
    # store log2 probabilities directly (one dict lookup per character when scoring); drop rare grams
    lp = {g: math.log2((n + k) / (ctx[g[:-1]] + k * V)) for g, n in grams.items() if n >= 2}
    unk = {c: math.log2(k / (n + k * V)) for c, n in ctx.items() if n >= 2}
    lm = {"n": LM_N, "lp": lp, "unk": unk, "floor": math.log2(1 / V)}
    (out / "vocab.json").write_text(json.dumps({k: v for k, v in vocab.items() if v >= 2}), encoding="utf-8")
    with open(out / "lm.pkl", "wb") as f:
        pickle.dump(lm, f)
    with open(out / "linefreq.pkl", "wb") as f:
        pickle.dump({"lf": lf, "ndocs": dict(ndocs)}, f)
    log(f"learn: vocab {len(vocab)}, lm grams {len(grams)}, corpora {len(lf)}")

# ----------------------------------------------------------------------------- chrome: learned page furniture

def lhash(s: str) -> bytes:
    return hashlib.blake2b(norm_line(s).encode(), digest_size=8).digest()


WEB = re.compile(
    r"\b(report a problem|you are here|print(able)? (version|this)|share (this|on)|subscribe|cookies?|"
    r"javascript|browser|website|home ?page|main menu|search (this|the) site|back to|top of (the )?page|"
    r"skip to|download|click here|email this|follow us|"
    r"this page|accessibility|privacy (policy|statement)|all rights reserved|page \d+ of \d+|"
    r"view (full|the) (document|ruling|text)|open in new window|bookmark|rss)\b", re.I)
LEGAL = re.compile(
    r"\b(section|subsection|s\s?\d|act \d{4}|\d{4} act|division|subdivision|part \d|paragraph|regulation|schedule|"
    r"commissioner|taxpayer|income|deduct\w*|assessable|entity|trust(ee)?|company|liability|ruling (applies|is))\b",
    re.I)


def _chrome_worker(doc):
    try:
        _, body, _ = read_doc(doc)
    except Exception:
        return None
    cand = _S["cand"].get(doc["corpus"])
    if not cand:
        return None
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    n = len(lines)
    out = []
    for i, s in enumerate(lines):
        if 3 <= len(s) <= 160:
            h = lhash(s)
            if h in cand:
                out.append((h, s, min(i, n - 1 - i) / max(1, n - 1)))
    return doc["corpus"], out


def chrome(docs: list[dict], out: Path) -> None:
    """Learn which repeated lines are page furniture (web chrome, running headers and footers).

    Candidates: lines repeated in many documents of a corpus. Each gets features (how widespread,
    where in the document it sits, length, word shapes, language-model score, web or legal vocabulary).
    Seed labels come from web-chrome vocabulary (positive) and legal vocabulary or full sentences
    (negative); a logistic regression trained on the seeds then scores every candidate, including the
    many that match neither list. Only lines the model is confident about AND that sit at the start or
    end of documents are treated as chrome."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    _init(str(out))
    lf, nd = _S["lf"]["lf"], _S["lf"]["ndocs"]
    # Only text scraped from websites carries page furniture. Legislation, commentary and our own
    # markdown (footers, dividers, headings) is structure, never chrome.
    scraped = {d["corpus"] for d in docs if d["kind"] == "txt"}
    cand = {c: {h for h, n in hs.items() if n >= max(8, 0.005 * nd.get(c, 1))}
            for c, hs in lf.items() if c in scraped}
    _S["cand"] = {c: s for c, s in cand.items() if s}
    rng = random.Random(5)
    by_c = collections.defaultdict(list)
    for d in docs:
        if d["corpus"] in _S["cand"]:
            by_c[d["corpus"]].append(d)
    sample = [d for ds in by_c.values() for d in (ds if len(ds) <= 3000 else rng.sample(ds, 3000))]
    agg: dict = {}
    with Pool(WORKERS) as pool:
        for r in pool.imap_unordered(_chrome_worker, sample, chunksize=32):
            if not r:
                continue
            c, rows = r
            for h, s, e in rows:
                a = agg.setdefault((c, h), {"text": s, "edge": 0.0, "n": 0})
                a["edge"] += e
                a["n"] += 1
    keys = list(agg)
    if not keys:
        (out / "chrome.json").write_text("{}", encoding="utf-8")
        return
    X, seed = [], []
    for c, h in keys:
        a = agg[(c, h)]
        s = a["text"]
        w = s.split()
        df = lf[c].get(h, 0) / max(1, nd.get(c, 1))
        X.append([math.log(df + 1e-6), a["edge"] / a["n"], math.log1p(len(s)), len(w),
                  sum(x[:1].isupper() for x in w) / max(1, len(w)), int(bool(re.search(r"\d", s))),
                  int(s.rstrip().endswith(".")), lm_line(s), int(bool(WEB.search(s))), int(bool(LEGAL.search(s)))])
        if LEGAL.search(s) or (s.rstrip().endswith(".") and len(w) >= 8) or re.match(r"^\(?[a-z0-9]{1,4}\)\s", s):
            seed.append(0)
        elif WEB.search(s) or NOISE.match(s):
            seed.append(1)
        else:
            seed.append(-1)
    X = np.array(X)
    seed = np.array(seed)
    lab = seed >= 0
    result = {}
    if lab.sum() >= 10 and len(set(seed[lab])) == 2:
        # the two vocabulary flags define the seeds, so the model learns from the other signals only
        feats_idx = [0, 1, 2, 3, 4, 5, 6, 7]
        clf = LogisticRegression(max_iter=2000, class_weight="balanced")
        clf.fit(X[lab][:, feats_idx], seed[lab])
        p = clf.predict_proba(X[:, feats_idx])[:, 1]
    else:
        p = np.where(seed == 1, 1.0, 0.0)
    for (c, h), pp, sd, x in zip(keys, p, seed, X):
        a = agg[(c, h)]
        edge = a["edge"] / a["n"]
        # Removed automatically: web-chrome wording at the start or end of documents. Lines only the
        # model suspects are listed for review ("suggested"), never removed on the model's say-so.
        if not re.search(r"[A-Za-z]{2}", a["text"]):
            sd, pp = 0, 0.0  # markdown dividers, bullets, page rules: structure
        is_chrome = bool(sd == 1 and edge <= 0.25)
        suggested = bool(sd == -1 and pp >= 0.9 and edge <= 0.15)
        result.setdefault(c, {})[h.hex()] = {"text": a["text"], "df": lf[c].get(h, 0), "p": round(float(pp), 3),
                                             "edge": round(edge, 3), "seed": int(sd), "chrome": is_chrome,
                                             "suggested": suggested}
    (out / "chrome.json").write_text(json.dumps(result, ensure_ascii=False, indent=0), encoding="utf-8")
    n_ch = sum(v["chrome"] for c in result.values() for v in c.values())
    log(f"chrome: {len(keys)} repeated lines scored, {n_ch} judged page furniture")

# ----------------------------------------------------------------------------- shared state for workers

_S: dict = {}


def _init(out: str):
    if _S.get("out") == out:
        return
    _S["out"] = out
    o = Path(out)
    _S["vocab"] = json.loads((o / "vocab.json").read_text(encoding="utf-8"))
    with open(o / "lm.pkl", "rb") as f:
        _S["lm"] = pickle.load(f)
    with open(o / "linefreq.pkl", "rb") as f:
        _S["lf"] = pickle.load(f)
    ch = o / "chrome.json"
    _S["chrome"] = ({c: {bytes.fromhex(h) for h, v in hs.items() if v["chrome"]}
                     for c, hs in json.loads(ch.read_text(encoding="utf-8")).items()} if ch.exists() else {})


def lm_line(s: str) -> float:
    """Mean log2 probability per character (add-k smoothed char n-gram). Higher = more typical."""
    lm = _S["lm"]
    n, LP, UNK, floor = lm["n"], lm["lp"], lm["unk"], lm["floor"]
    t = "\x02" + re.sub(r"[ \t]+", " ", s)
    if len(t) < n:
        return 0.0
    tot = 0.0
    for i in range(len(t) - n + 1):
        g = t[i:i + n]
        v = LP.get(g)
        tot += v if v is not None else UNK.get(g[:-1], floor - 3.0)
    return tot / (len(t) - n + 1)


def wf(w: str) -> int:
    return _S["vocab"].get(w.lower(), 0)

# ----------------------------------------------------------------------------- features

MOJI = re.compile(r"Ã[\x80-\xbf]|â€|Â[\xa0-\xbf ]|Ã¢|ï¿½|�")
LIG = re.compile(r"[ﬀ-ﬆ]")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HTMLR = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+);|</?(?:div|span|p|br|table|td|tr|font)\b", re.I)
HYPH_BREAK = re.compile(r"([A-Za-z]{2,})-\n([a-z]{2,})")
SPLIT = re.compile(r"(?<![\w'’.\-/])([A-Za-z]{1,12}) ([a-z]{1,10})\b(?![\w'’\-])")
END_OK = re.compile(r"[.;:!?)\]\"'’”*|>`-]\s*$|^\s*$")
NOISE = re.compile(
    r"^\s*(page \d+( of \d+)?\s*$|compilation no\.?\s*\d+\s*$|authorised version|"
    r"registered:? \d{1,2}[/ ]\w+[/ ]\d{4}\s*$|federal register of legislation|prepared by the office of parliamentary counsel|"
    r"https?://\S+\s*$|printed (on|by)|view (full )?(document|ruling)|back to top|skip to (main )?content|"
    r"print this page|share this page|top of page)", re.I)


_ZIPF: dict = {}


def zipf(w: str) -> float:
    """English word frequency on the Zipf scale (wordfreq): 0 = not a word, 7 = 'the'.
    A lone letter other than a, i or o is treated as a fragment ("s upply", "t axed")."""
    w = w.lower()
    if len(w) == 1 and w not in "aio":
        return 0.0
    v = _ZIPF.get(w)
    if v is None:
        from wordfreq import zipf_frequency
        v = _ZIPF[w] = zipf_frequency(w, "en")
    return v


def split_candidates(body: str) -> list[tuple[str, str]]:
    """Word pieces that should be one word ("decis ion"): the joined word is common in this corpus
    and in English, and at least one piece is not an English word on its own ("in come" is left alone)."""
    out = []
    for m in SPLIT.finditer(body):
        a, b = m.group(1), m.group(2)
        j = a + b
        if wf(j) < 20:
            continue
        # Never: a lone capital (anonymised parties "D owns", case names "R v"), a one-letter second
        # piece ("Act s 8-1" is "section 8-1 of the Act"), or redaction placeholders ("x xxx").
        if (len(a) == 1 and a.isupper()) or len(b) == 1 or re.fullmatch(r"[xX]+", a) or re.fullmatch(r"[xX]+", b):
            continue
        if len(a) == 1:
            # "s ection", "e xpenses": only when what follows is not a word in its own right
            if zipf(b) < 2.0 and zipf(j) >= 3.0:
                out.append((a, b))
            continue
        if zipf(j) >= 2.5 and min(zipf(a), zipf(b)) < 1.5 and max(zipf(a), zipf(b)) < 6.0:
            out.append((a, b))
    return out


def hyph_candidates(body: str) -> list[tuple[str, str]]:
    out = []
    for m in HYPH_BREAK.finditer(body):
        a, b = m.group(1), m.group(2)
        if wf(a + b) >= 10 and wf(f"{a}-{b}") < 3:
            out.append((a, b))
    return out


def boilerplate_lines(corpus: str, body: str) -> list[str]:
    """Page furniture in this document: page counters anywhere ("Page 3 of 26"), and lines the chrome
    model learned for this corpus when they sit in the first or last fifth of the document."""
    lf = _S["lf"]["lf"].get(corpus, {})
    learned = _S.get("chrome", {}).get(corpus, set())
    return [s for _, s in boilerplate_at(corpus, body)]


def boilerplate_at(corpus: str, body: str) -> list[tuple[int, str]]:
    """(line index, text) of every page-furniture line, so a repair removes those occurrences only."""
    lf = _S["lf"]["lf"].get(corpus, {})
    learned = _S.get("chrome", {}).get(corpus, set())
    lines = body.split("\n")
    idx = [i for i, s in enumerate(lines) if s.strip()]
    n = len(idx)
    out = []
    for k, i in enumerate(idx):
        s = lines[i].strip()
        if not (3 <= len(s) <= 160) or re.search(r"\b(s|section|div|part|reg)\.?\s*\d", s, re.I):
            continue
        h = lhash(s)
        near_edge = min(k, n - 1 - k) <= max(2, 0.2 * n)
        if (lf.get(h, 0) >= 5 and NOISE.match(s)) or (h in learned and near_edge):
            out.append((i, s))
    return out


_MH = None


def minhash(body: str, k: int = 64) -> list[int]:
    """64 MinHash values over word 5-shingles (vectorised), for near-duplicate detection."""
    import numpy as np
    global _MH
    toks = WORD.findall(body.lower())
    if not toks:
        return []
    sh = {" ".join(toks[i:i + 5]) for i in range(max(1, len(toks) - 4))}
    if _MH is None:
        rs = np.random.RandomState(1)
        _MH = (rs.randint(1, 2**62, size=k, dtype=np.int64).astype(np.uint64) * np.uint64(2) + np.uint64(1),
               rs.randint(0, 2**62, size=k, dtype=np.int64).astype(np.uint64))
    h = np.fromiter((int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little") for s in sh),
                    dtype=np.uint64, count=len(sh))
    with np.errstate(over="ignore"):
        v = h[:, None] * _MH[0][None, :] + _MH[1][None, :]
    return [int(x) for x in v.min(axis=0)]


def features_of(doc: dict, body: str) -> dict:
    n = len(body)
    lines = [l for l in body.split("\n") if l.strip()]
    words = WORD.findall(body)
    lw = [w.lower() for w in words if len(w) > 2]
    oov = sum(1 for w in lw if wf(w) < 2) / max(1, len(lw))
    sample = lines if len(lines) <= 400 else random.Random(n).sample(lines, 400)
    scores = [lm_line(l) for l in sample if len(l) >= 12]
    bad = [s for s in scores if s < -6.0]
    letters = sum(c.isalpha() for c in body)
    return {
        "id": doc["id"], "corpus": doc["corpus"], "kind": doc["kind"],
        "chars": n, "lines": len(lines), "mean_line": n / max(1, len(lines)),
        "alpha": letters / max(1, n), "digit": sum(c.isdigit() for c in body) / max(1, n),
        "upper": sum(c.isupper() for c in body) / max(1, letters),
        "nonascii": sum(ord(c) > 127 for c in body) / max(1, n),
        "moji": len(MOJI.findall(body)), "lig": len(LIG.findall(body)), "ctrl": len(CTRL.findall(body)),
        "html": len(HTMLR.findall(body)), "ws": len(WS_LINE.findall(body)),
        "splits": len(split_candidates(body)), "hyph": len(hyph_candidates(body)),
        "boiler": len(boilerplate_lines(doc["corpus"], body)),
        "oov": oov,
        "lm_mean": sum(scores) / len(scores) if scores else 0.0,
        "lm_p10": sorted(scores)[len(scores) // 10] if scores else 0.0,
        "lm_bad": len(bad) / max(1, len(scores)),
        "trunc": int(n > 200 and not END_OK.search(body.rstrip()[-3:] if body.strip() else "")),
        "empty": int(len(body.strip()) < 40),
        "mh": minhash(body),
    }


TRUNC_TAIL = re.compile(r"[a-z,]$")
WS_LINE = re.compile(r"(?m)^[ \t ]+$")
_KIND: dict = {}


def corpus_kind(corpus: str) -> str:
    return _KIND.get(corpus, "txt")


def truncated(doc: dict, body: str, raw) -> int:
    """Cut off mid-sentence: the last substantial line ends with a lowercase letter or a comma.
    For private rulings only the narrative fields count (lists of case names legitimately end bare)."""
    texts = ([raw.get(k) for k in ("facts", "reasons_for_decision") if isinstance(raw.get(k), str)]
             if doc["kind"] == "prjson" and isinstance(raw, dict) else [body])
    for t in texts:
        lines = [l.strip() for l in t.split("\n") if l.strip()]
        if not lines:
            continue
        last = lines[-1]
        # list items, table rows and case-name lists legitimately end without punctuation
        if re.match(r"^([-*•|]|\d+\.|\(\w{1,4}\))\s", last) or " v " in last or " v. " in last:
            continue
        if len(last) >= 60 and TRUNC_TAIL.search(last):
            return 1
    return 0


def _feat_worker(doc):
    try:
        _, body, raw = read_doc(doc)
        f = features_of(doc, body)
        f["trunc"] = truncated(doc, body, raw)
        return f
    except Exception as e:
        return {"id": doc["id"], "corpus": doc["corpus"], "kind": doc["kind"], "error": repr(e)[:200]}


def featurize(docs, out: Path, name: str = "features.jsonl"):
    t = time.time()
    _init(str(out))  # loaded once here; fork shares it with every worker
    with Pool(WORKERS) as pool:
        def gen():
            for i, r in enumerate(pool.imap_unordered(_feat_worker, docs, chunksize=32)):
                if i % 10000 == 0:
                    log(f"features: {i}/{len(docs)} ({time.time() - t:.0f}s)")
                yield r
        n = jl_write(out / name, gen())
    log(f"features: {n} docs in {time.time() - t:.0f}s")

# ----------------------------------------------------------------------------- model

RULES = {
    "mojibake": lambda f: f["moji"] > 0,
    "ligature": lambda f: f["lig"] > 0,
    "control": lambda f: f["ctrl"] > 0,
    "html": lambda f: f["html"] >= 3,
    "midword_split": lambda f: f["splits"] >= 2,
    "hyphen_break": lambda f: f["hyph"] >= 1,
    "boilerplate": lambda f: f["boiler"] >= 2,
    "whitespace": lambda f: f.get("ws", 0) >= 20 and f["kind"] != "md",
    "truncated": lambda f: bool(f["trunc"]),
    "empty": lambda f: bool(f["empty"]),
    "garbled": lambda f: f["lm_bad"] > 0.25 and f["lines"] >= 4,
    "high_oov": lambda f: f["oov"] > 0.25 and f["chars"] > 500,
}
NUMF = ["chars", "lines", "mean_line", "alpha", "digit", "upper", "nonascii", "moji", "lig", "ctrl", "html",
        "splits", "hyph", "boiler", "oov", "lm_mean", "lm_p10", "lm_bad", "trunc", "empty"]


def model(out: Path) -> None:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
    from sklearn.model_selection import cross_val_predict
    from sklearn.metrics import roc_auc_score

    feats = [f for f in jl_read(out / "features.jsonl") if "error" not in f]
    X = np.array([[math.log1p(f[k]) if k in ("chars", "lines", "mean_line", "moji", "lig", "ctrl", "html",
                                               "splits", "hyph", "boiler") else f[k] for k in NUMF] for f in feats])
    labels = [[name for name, fn in RULES.items() if fn(f)] for f in feats]
    # anomaly score per document kind (legislation md, txt rulings, private ruling json)
    anom = np.zeros(len(feats))
    kinds = np.array([f["kind"] for f in feats])
    for k in set(kinds):
        idx = np.where(kinds == k)[0]
        if len(idx) < 50:
            continue
        iso = IsolationForest(n_estimators=200, contamination="auto", random_state=0, n_jobs=WORKERS)
        iso.fit(X[idx])
        anom[idx] = -iso.score_samples(X[idx])
    # weak supervision: rule-flagged = 1; clean = 0. Classifier sees only features the rules do NOT
    # read directly, so it must generalise: a high probability with no rule = a defect rules missed.
    y = np.array([1 if l else 0 for l in labels])
    indirect = [NUMF.index(k) for k in ("chars", "lines", "mean_line", "alpha", "digit", "upper", "nonascii",
                                         "oov", "lm_mean", "lm_p10")]
    Xi = X[:, indirect]
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, random_state=0)
    p = cross_val_predict(clf, Xi, y, cv=5, method="predict_proba")[:, 1] if 0 < y.sum() < len(y) else np.zeros(len(y))
    auc = roc_auc_score(y, p) if 0 < y.sum() < len(y) else float("nan")
    rows = []
    for f, l, a, pp in zip(feats, labels, anom, p):
        rows.append({"id": f["id"], "corpus": f["corpus"], "kind": f["kind"], "labels": l,
                     "anomaly": round(float(a), 4), "p_defect": round(float(pp), 4)})
    jl_write(out / "scores.jsonl", rows)
    meta = {"docs": len(rows), "flagged": int(y.sum()), "cv_auc_indirect": round(float(auc), 4),
            "label_counts": collections.Counter(x for l in labels for x in l)}
    (out / "model.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log(f"model: {meta}")

# ----------------------------------------------------------------------------- repair

# Tokens a repair must never change: numbers, provision and paragraph references, citations.
PROTECT = re.compile(r"¶?\d+(?:[-–.]\d+)*[A-Z]{0,3}|\(\w{1,4}\)|\[\d{4}\]|\b[A-Z]{2,6}\s?\d{4}/\d+")


FFFD_CANDIDATES = ["’", "‘", "“", "”", "–", "—", " ", "§", "£", "½", "¼", "¾", "fi", "fl", "ff", "ffi",
                   "é", "è", "à", "ü", "ö", "ç", "'", '"', "-", "$"]
FFFD_MARGIN = 0.04  # mean log2-probability per character the winner must lead the runner-up by


def fffd_choice(t: str, i: int) -> str | None:
    """What a lost character (U+FFFD) at t[i] most likely was, or None to leave it for review.
    Context rules narrow the candidates; the language model chooses only inside a safe group."""
    prev, nxt = t[i - 1] if i else " ", t[i + 1] if i + 1 < len(t) else " "
    word_before = re.search(r"(\w+)\W*$", t[max(0, i - 12):i])
    wb = word_before.group(1).lower() if word_before else ""
    if nxt.isdigit():
        if prev.isdigit():
            return None                                   # 4�% : ½ ¼ ¾ cannot be told apart
        if prev.isalpha() or (wb in ("no", "s", "ss", "par", "para", "r", "reg", "pt", "div", "item", "cl")
                              and prev in " .\xa0"):
            return " "                                    # No�3, ss�17, par�420
        year = _S.get("doc_year")
        if year:
            return "£" if year < 1966 else "$"            # Australia decimalised in 1966
        return None
    if prev.isalpha() and nxt.isalpha():
        lw = re.search(r"([A-Za-z]+)$", t[max(0, i - 20):i]).group(1)
        rw = re.match(r"([A-Za-z]+)", t[i + 1:i + 21]).group(1)
        if len(lw) >= 2 and len(rw) >= 1 and zipf(lw) >= 2.5 and (zipf(rw) >= 2.5 or rw.lower() == "v"):
            return " "                                    # Group�v�State: a lost (non-breaking) space
        group = ["’", "é", "è", "à", "ü", "ö", "ç", "fi", "fl", "ff"]
    elif prev.isalpha() and not nxt.isalnum():
        group = ["’", "”", "é"]
    elif not prev.isalnum() and nxt.isalpha():
        group = ["‘", "“"]
    else:
        return None
    left, right = t[max(0, i - 24):i], t[i + 1:i + 25].replace("�", " ")
    ranked = sorted(((lm_line(left + c + right), c) for c in group), reverse=True)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < FFFD_MARGIN:
        return None
    return ranked[0][1]


def protected(s: str) -> collections.Counter:
    return collections.Counter(PROTECT.findall(s))


def repair_text(corpus: str, body: str, details: list | None = None) -> tuple[str, list[str], list[str]]:
    """Returns (new_body, fixes_applied, removed_lines); word joins are appended to `details`."""
    fixes, removed = [], []
    t = body
    if MOJI.search(t):
        import ftfy
        nt = ftfy.fix_text(t, normalization=None, uncurl_quotes=False, fix_line_breaks=False)
        if nt != t:
            fixes.append("mojibake")
            t = nt
    if "�" in t:
        # Characters lost before the text reached us (U+FFFD). The language model picks, for each
        # one, the candidate that reads most naturally in its own context: O’Sullivan, par 420, ‘R’.
        out_chars, last, changed = [], 0, False
        for m in re.finditer("�+", t):
            if len(m.group(0)) > 1:  # a run of lost characters (non-Latin text): leave for review
                continue
            i = m.start()
            best = fffd_choice(t, i)
            if best is None:
                continue  # not confident: keep the marker so the document stays on the review list
            out_chars.append(t[last:i] + best)
            last = i + 1
            changed = True
        if changed:
            t = "".join(out_chars) + t[last:]
            fixes.append("lost_character")
    if LIG.search(t):
        t = LIG.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), t)
        fixes.append("ligature")
    if CTRL.search(t):
        t = CTRL.sub("", t)
        fixes.append("control")
    if corpus_kind(corpus) != "md" and WS_LINE.search(t):
        # scraped text: lines holding only spaces, and runs of blank lines (never markdown, where
        # trailing spaces are line breaks)
        nt = re.sub(r"\n{3,}", "\n\n", re.sub(r"(?m)^[ \t ]+$", "", t))
        if nt != t:
            fixes.append("whitespace")
            t = nt
    if HTMLR.search(t) and corpus not in ("maps",):
        nt = re.sub(r"</?(?:div|span|font)\b[^>]*>", "", t, flags=re.I)
        nt = re.sub(r"&(nbsp|amp|lt|gt|quot|#\d+);", lambda m: html.unescape(m.group(0)).replace("\xa0", " "), nt)
        if nt != t:
            fixes.append("html")
            t = nt
    hy = hyph_candidates(t)
    if hy:
        for a, b in set(hy):
            t = t.replace(f"{a}-\n{b}", f"{a}{b}\n")
        fixes.append("hyphen_break")
    sp = split_candidates(t)
    if sp:
        for a, b in set(sp):
            # same word boundaries the detector used, so "IPP's team" can never become "IPP'steam"
            t = re.sub(rf"(?<![\w'’.\-/]){re.escape(a)} {re.escape(b)}\b(?![\w'’\-])", a + b, t)
            if details is not None and len(details) < 20:
                details.append(f"{a} {b} -> {a}{b}")
        fixes.append("midword_split")
    at = boilerplate_at(corpus, t)
    if at:
        lines = t.split("\n")
        drop = set()
        for i, s in at:
            # A web phrase inside a line that also carries a date or a number ("Original statement You
            # are here 7 July 2017") loses only the phrase; the version date stays.
            rest = re.sub(r"\s{2,}", " ", WEB.sub(" ", s)).strip()
            if WEB.search(s) and re.search(r"\d", rest) and len(rest) >= 8:
                lines[i] = rest
                removed.append(WEB.search(s).group(0))
            else:
                drop.add(i)
                removed.append(s)
        t = re.sub(r"\n{3,}", "\n\n", "\n".join(l for i, l in enumerate(lines) if i not in drop))
        fixes.append("boilerplate")
    return t, fixes, removed


def gate(before: str, after: str, removed: list[str]) -> tuple[bool, str, dict]:
    """A repair is kept only if every number, provision, paragraph and citation token survives
    (bar those in removed page furniture), the text did not shrink beyond the removed lines, and the
    language model rates the result at least as typical as the original."""
    pb, pa = protected(before), protected(after)
    for r in removed:
        pb.subtract(protected(r))
    pb = +pb
    if pb != pa:
        diff = {k: (pb.get(k, 0), pa.get(k, 0)) for k in set(pb) | set(pa) if pb.get(k, 0) != pa.get(k, 0)}
        return False, "protected tokens changed", {"diff": dict(list(diff.items())[:8])}
    if len(after) < 0.9 * len(before) - sum(len(r) + 1 for r in removed):
        return False, "text shrank", {}
    lb = [lm_line(l) for l in before.split("\n") if len(l) >= 12][:400]
    la = [lm_line(l) for l in after.split("\n") if len(l) >= 12][:400]
    mb = sum(lb) / len(lb) if lb else 0
    ma = sum(la) / len(la) if la else 0
    if ma < mb - 0.02:
        return False, "language model rates it worse", {"lm_before": mb, "lm_after": ma}
    return True, "ok", {"lm_before": round(mb, 3), "lm_after": round(ma, 3)}


def _repair_worker(args):
    doc, out = args
    try:
        y = re.match(r"(\d{4})_", Path(doc["rel"]).name)
        _S["doc_year"] = int(y.group(1)) if y else None  # judgments: <year>_<COURT>_<n>
        head, body, raw = read_doc(doc)
        joins: list = []
        if doc["kind"] == "prjson":
            fx_all, rm_all, ok_all, why = [], [], True, "ok"
            info = {}

            def fix(s):
                nonlocal ok_all, why
                ns, fx, rm = repair_text(doc["corpus"], s, joins)
                if ns == s:
                    return s
                ok, w, inf = gate(s, ns, rm)
                if not ok:
                    ok_all, why = False, w
                    info.update(inf)
                    return s
                fx_all.extend(fx)
                rm_all.extend(rm)
                return ns
            new_raw = map_pr_strings(json.loads(json.dumps(raw)), fix)
            if new_raw == raw:
                return {"id": doc["id"], "status": "unchanged" if ok_all else "rejected", "reason": why, **info}
            dest = Path(out) / "files" / doc["rel"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            # keep the source's own JSON layout so a diff shows only the repaired strings
            src = Path(doc["abs"]).read_text(encoding="utf-8")
            m = re.search(r"\n( +)\"", src)
            dest.write_text(json.dumps(new_raw, ensure_ascii="\\u" in src, indent=len(m.group(1)) if m else None)
                            + ("\n" if src.endswith("\n") else ""), encoding="utf-8")
            return {"id": doc["id"], "status": "repaired", "fixes": sorted(set(fx_all)), "removed": rm_all[:5],
                    "joins": joins, "partial": not ok_all}
        nb, fixes, removed = repair_text(doc["corpus"], body, joins)
        if nb == body:
            return {"id": doc["id"], "status": "unchanged"}
        ok, why, info = gate(body, nb, removed)
        if not ok:
            return {"id": doc["id"], "status": "rejected", "reason": why, "fixes": fixes, "joins": joins, **info}
        dest = Path(out) / "files" / doc["rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(head + nb, encoding="utf-8")
        return {"id": doc["id"], "status": "repaired", "fixes": fixes, "removed": removed[:5], "joins": joins,
                "chars_before": len(body), "chars_after": len(nb), **info}
    except Exception as e:
        return {"id": doc["id"], "status": "error", "reason": repr(e)[:200]}


def repair(docs, out: Path) -> None:
    scores = {s["id"]: s for s in jl_read(out / "scores.jsonl")}
    fixable = {"mojibake", "ligature", "control", "html", "midword_split", "hyphen_break", "boilerplate", "whitespace"}
    todo = [d for d in docs if set(scores.get(d["id"], {}).get("labels", [])) & fixable]
    for d in docs:  # a corpus holding any markdown is treated as markdown (whitespace left alone)
        if _KIND.get(d["corpus"]) != "md":
            _KIND[d["corpus"]] = d["kind"]
    log(f"repair: {len(todo)} candidate documents")
    t = time.time()
    _init(str(out))
    with Pool(WORKERS) as pool:
        def gen():
            for i, r in enumerate(pool.imap_unordered(_repair_worker, ((d, str(out)) for d in todo), chunksize=16)):
                if i % 5000 == 0:
                    log(f"repair: {i}/{len(todo)} ({time.time() - t:.0f}s)")
                yield r
        rows = list(gen())
    jl_write(out / "manifest.jsonl", rows)
    log(f"repair: {collections.Counter(r['status'] for r in rows)}")

# ----------------------------------------------------------------------------- duplicates + review queue

def duplicates(out: Path, threshold: float = 0.9) -> list[tuple[str, str, float]]:
    feats = [(f["id"], f["corpus"], f.get("mh") or []) for f in jl_read(out / "features.jsonl") if f.get("mh")]
    bands, rows = 16, 4
    buckets = collections.defaultdict(list)
    for i, (_, _, mh) in enumerate(feats):
        for b in range(bands):
            buckets[(b, tuple(mh[b * rows:(b + 1) * rows]))].append(i)
    pairs = set()
    for ids in buckets.values():
        if 1 < len(ids) <= 50:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    pairs.add((ids[x], ids[y]))
    out_pairs = []
    for a, b in pairs:
        ma, mb = feats[a][2], feats[b][2]
        sim = sum(1 for x, y in zip(ma, mb) if x == y) / len(ma)
        if sim >= threshold:
            out_pairs.append((feats[a][0], feats[b][0], round(sim, 3)))
    return sorted(out_pairs, key=lambda x: -x[2])


def review(out: Path) -> None:
    scores = list(jl_read(out / "scores.jsonl"))
    man = {m["id"]: m for m in jl_read(out / "manifest.jsonl")} if (out / "manifest.jsonl").exists() else {}
    queue = []
    for s in scores:
        why = [l for l in s["labels"] if l in ("truncated", "empty", "garbled", "high_oov")]
        m = man.get(s["id"])
        if m and m["status"] in ("rejected", "error"):
            why.append(f"repair {m['status']}: {m.get('reason')}")
        if not s["labels"] and s["p_defect"] > 0.8:
            why.append("model: looks defective though no rule fired")
        if why:
            queue.append({"id": s["id"], "corpus": s["corpus"], "why": why,
                          "priority": round(s["anomaly"] + s["p_defect"] + 0.5 * len(why), 3)})
    dups = duplicates(out)
    # Identical private rulings are real (the ATO issues the same template ruling to many applicants):
    # counted in the report, not queued. Every other near-duplicate pair is queued.
    pr_dups = [p for p in dups if p[0].startswith("private-rulings/") and p[1].startswith("private-rulings/")]
    (out / "duplicates.json").write_text(json.dumps({"private_ruling_pairs": len(pr_dups),
                                                      "other_pairs": [p for p in dups if p not in pr_dups]},
                                                     indent=0), encoding="utf-8")
    for a, b, sim in dups:
        if (a, b, sim) in pr_dups:
            continue
        queue.append({"id": a, "corpus": a.split("/")[0], "why": [f"near-duplicate of {b} ({sim})"], "priority": sim})
    queue.sort(key=lambda r: -r["priority"])
    jl_write(out / "review.jsonl", queue)
    log(f"review: {len(queue)} items, {len(dups)} near-duplicate pairs")

# ----------------------------------------------------------------------------- rescore + report

def rescore(out: Path) -> None:
    man = [m for m in jl_read(out / "manifest.jsonl") if m["status"] == "repaired"]
    docs = {d["id"]: d for d in jl_read(out / "docs.jsonl")}
    fixed = []
    for m in man:
        d = dict(docs[m["id"]])
        d["abs"] = str(out / "files" / d["rel"])
        fixed.append(d)
    featurize(fixed, out, "features_after.jsonl")


def report(out: Path, started: float) -> None:
    feats = {f["id"]: f for f in jl_read(out / "features.jsonl") if "error" not in f}
    after = {f["id"]: f for f in jl_read(out / "features_after.jsonl")} if (out / "features_after.jsonl").exists() else {}
    scores = list(jl_read(out / "scores.jsonl"))
    man = list(jl_read(out / "manifest.jsonl")) if (out / "manifest.jsonl").exists() else []
    rev = list(jl_read(out / "review.jsonl")) if (out / "review.jsonl").exists() else []
    meta = json.loads((out / "model.json").read_text(encoding="utf-8"))
    by_corpus = collections.defaultdict(collections.Counter)
    for s in scores:
        by_corpus[s["corpus"]]["docs"] += 1
        for l in s["labels"]:
            by_corpus[s["corpus"]][l] += 1
    rep = collections.defaultdict(collections.Counter)
    for m in man:
        c = m["id"].split("/")[0]
        rep[c][m["status"]] += 1
        for fx in m.get("fixes", []):
            rep[c]["fix:" + fx] += 1
    # before/after on repaired docs
    keys = ["moji", "lig", "ctrl", "html", "splits", "hyph", "boiler", "lm_bad"]
    ba = {k: [0.0, 0.0] for k in keys}
    lm = [0.0, 0.0, 0]
    for i, a in after.items():
        b = feats.get(i)
        if not b or "error" in a:
            continue
        for k in keys:
            ba[k][0] += b[k]
            ba[k][1] += a[k]
        lm[0] += b["lm_mean"]
        lm[1] += a["lm_mean"]
        lm[2] += 1
    L = ["# Data clean-up report", "",
         f"Run: `{out}` · {time.strftime('%Y-%m-%d %H:%M')} · {((time.time() - started) / 3600):.1f} h", "",
         "Nothing in the live data was changed. Repaired copies are in `files/`; every change is in `manifest.jsonl`.", "",
         "## Summary", "",
         f"- Documents analysed: **{meta['docs']:,}**; flagged by at least one rule: **{meta['flagged']:,}**",
         f"- Repaired automatically (all checks passed): **{sum(1 for m in man if m['status'] == 'repaired'):,}**",
         f"- Repairs refused by the safety gate: **{sum(1 for m in man if m['status'] == 'rejected'):,}**",
         f"- Review queue (needs a person or a re-fetch): **{len(rev):,}**",
         f"- Classifier (rules held out) cross-validated AUC: **{meta['cv_auc_indirect']}**: how well the "
         "model spots defective documents from general text statistics alone.", "",
         "## Defects found, by kind", "", "| Defect | Documents |", "|---|---|"]
    for k, v in sorted(meta["label_counts"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v:,} |")
    L += ["", "## Before and after (repaired documents only)", "", "| Signal | Before | After |", "|---|---|---|"]
    for k in keys:
        L.append(f"| {k} | {ba[k][0]:,.0f} | {ba[k][1]:,.0f} |")
    if lm[2]:
        L.append(f"| language-model mean (higher = cleaner) | {lm[0] / lm[2]:.3f} | {lm[1] / lm[2]:.3f} |")
    L += ["", "## By corpus", "", "| Corpus | Docs | Flagged kinds | Repaired | Refused |", "|---|---|---|---|---|"]
    for c in sorted(by_corpus, key=lambda c: -by_corpus[c]["docs"]):
        kinds = ", ".join(f"{k} {v}" for k, v in by_corpus[c].most_common() if k != "docs")[:160]
        L.append(f"| {c} | {by_corpus[c]['docs']:,} | {kinds or '-'} | {rep[c]['repaired']:,} | {rep[c]['rejected']:,} |")
    ch = json.loads((out / "chrome.json").read_text(encoding="utf-8")) if (out / "chrome.json").exists() else {}
    if ch:
        L += ["", "## Page furniture the model learned (removed only near the start or end of a document)", "",
              "| Corpus | Line | In docs | Model p | Seed |", "|---|---|---|---|---|"]
        for c in sorted(ch):
            rows = sorted((v for v in ch[c].values() if v["chrome"]), key=lambda v: -v["df"])[:8]
            for v in rows:
                seed = {1: "web words", 0: "legal words", -1: "model only"}[v["seed"]]
                L.append(f"| {c} | {v['text'][:70].replace('|', '/')} | {v['df']:,} | {v['p']} | {seed} |")
        sug = sorted(((c, v) for c in ch for v in ch[c].values() if v.get("suggested")), key=lambda cv: -cv[1]["df"])
        if sug:
            L += ["", "### Suggested by the model only (NOT removed; confirm to add them)", "",
                  "| Corpus | Line | In docs | Model p |", "|---|---|---|---|"]
            for c, v in sug[:30]:
                L.append(f"| {c} | {v['text'][:70].replace('|', '/')} | {v['df']:,} | {v['p']} |")
    du = json.loads((out / "duplicates.json").read_text(encoding="utf-8")) if (out / "duplicates.json").exists() else {}
    if du:
        L += ["", "## Near-duplicates", "",
              f"- Private rulings sharing the same text: **{du['private_ruling_pairs']:,}** pairs (ATO template "
              "rulings issued to different applicants; expected, not queued).",
              f"- Other near-duplicate pairs (queued for review): **{len(du['other_pairs']):,}**"]
    L += ["", "## Top of the review queue", ""]
    for r in rev[:40]:
        L.append(f"- `{r['id']}`: {'; '.join(r['why'])}")
    L += ["", "## Applying", "",
          "Review `manifest.jsonl` and a sample of `files/`, then copy `files/` over a COPY of the data directory "
          "(the private rulings under `files/private-rulings/` belong in the private rulings JSON folder), "
          "run `scripts/check_ingest_real.py` on it and rebuild the search index. Nothing is applied automatically."]
    (out / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log("report written")

# ----------------------------------------------------------------------------- driver

def main() -> int:
    global LOG
    import multiprocessing
    multiprocessing.set_start_method("fork", force=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["run", "inventory", "learn", "features", "model", "repair", "review", "rescore", "report"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--pr", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    LOG = out / "run.log"
    started = time.time()
    data = Path(a.data).resolve()
    if str(out.resolve()).startswith(str(data)):
        raise SystemExit("--out must not be inside the data directory")

    def phase(name, done_file, fn):
        if a.phase not in ("run", name):
            return
        if (out / done_file).exists() and not a.force and a.phase == "run":
            log(f"{name}: done already, skipping")
            return
        log(f"== {name}")
        fn()

    docs_cache = []

    def get_docs():
        if not docs_cache:
            docs_cache.extend(jl_read(out / "docs.jsonl"))
        return docs_cache

    phase("inventory", "docs.jsonl", lambda: log(f"inventory: {jl_write(out / 'docs.jsonl', inventory(data, Path(a.pr) if a.pr else None, a.limit))} docs"))
    phase("learn", "linefreq.pkl", lambda: learn(get_docs(), out))
    phase("chrome", "chrome.json", lambda: chrome(get_docs(), out))
    phase("features", "features.jsonl", lambda: featurize(get_docs(), out))
    phase("model", "scores.jsonl", lambda: model(out))
    phase("repair", "manifest.jsonl", lambda: repair(get_docs(), out))
    phase("review", "review.jsonl", lambda: review(out))
    phase("rescore", "features_after.jsonl", lambda: rescore(out))
    phase("report", "REPORT.md.never", lambda: report(out, started))
    log("finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
