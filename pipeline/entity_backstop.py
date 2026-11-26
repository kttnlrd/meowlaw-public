"""Entity resolution backstop (graph spec §7).

The regex pipeline (`_parse_leg_ref` / `_case_key` in graph_etl.py) leaves
tens of thousands of refs unresolved. This module resolves them in three
stages, cheapest first:

  collect   → data/entity_candidates.json  {leg: {ref: count}, case: {ref: count}}
  local     → deterministic resolution: state acts / regulations / act-name-only
              are out_of_scope; rich section forms (paragraph, "of the ITAA 1997",
              FBTAA) map to existing section nodes; case refs resolve against a
              party-name alias index built from the case corpus. Ambiguous
              (multi-citation) case refs are flagged, never guessed.
  map       → DeepSeek batch pass over the LEG residue only (case refs are fully
              deterministic — the corpus is the ground truth, LLM recall of old
              citations would only produce keys that don't exist). Resumable via
              checkpoint markers.
  validate  → G4 gate: every mapped key resolves to a graph node; ambiguous
              flagged; writes a 100-entry manual review list.

Status values: mapped | ambiguous | unknown | out_of_scope.
Only `mapped` entries carry a key and ever become edges.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.graph_etl import _ACT_ALIASES, _case_key, _parse_leg_ref  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
CANDIDATES = DATA / "entity_candidates.json"
ALIAS_MAP = DATA / "entity_alias_map.json"
REVIEW = DATA / "entity_review.json"
GRAPH_DB = DATA / "graph.db"

RULINGS_DIR = Path(os.environ.get("HERMES_RULINGS_DIR", "~/.hermes/private_rulings")).expanduser()
LLM_DIR = RULINGS_DIR / "data" / "json_llm"

BATCH_SIZE = 400
MAX_REF_LEN = 220  # longer strings are prose dumps, not citations

_STATE_RE = re.compile(r"\((?:SA|NSW|VIC|QLD|WA|TAS|NT|ACT|UK|NZ|US)\)", re.IGNORECASE)
_REG_RE = re.compile(r"\bregulations?\b", re.IGNORECASE)
# richer section keywords than the ETL's (paragraph/subparagraph/item too)
_SECTION_KW_RE = re.compile(
    r"(?:sub)?(?:paragraph|section|subsection|item)\s+([0-9]+(?:-[0-9]+)?[A-Za-z]*)"
    r"(?:\([^)]*\))?", re.IGNORECASE)
# post-positioned: "section 35-35 of the ITAA 1997"
_POST_RE = re.compile(
    r"(?:sub)?(?:paragraph|section|subsection|item)\s+([0-9]+(?:-[0-9]+)?[A-Za-z]*)"
    r"\s+of\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE)
_NON_SECTION_RE = re.compile(r"^\s*(?:division|subdivision|part|schedule|chapter)\b", re.IGNORECASE)
# non-section keyword post-positioned: "Division 900 of the ITAA 1997"
_NON_SECTION_POST_RE = re.compile(
    r"^\s*(?:division|subdivision|part|schedule|chapter)\s+\S+\s+of\s+", re.IGNORECASE)
_NEUTRAL_RE = re.compile(r"\[\d{4}\]\s*[A-Z]+\s+\d+")

# extra aliases the ETL lacks
_EXTRA_ALIASES = [
    ("fringe benefits tax assessment act", "fbt-1986"),
    ("fbt assessment act", "fbt-1986"),
    ("fbt act 1986", "fbt-1986"),
    ("fbt act", "fbt-1986"),
    ("fbtaa 1986", "fbt-1986"),
    ("fbtaa", "fbt-1986"),
    ("ita 1936", "itaa-1936"),
    ("ita 1997", "itaa-1997"),
    ("gsta 1999", "gst-1999"),
    ("gsta", "gst-1999"),
    ("taa 1953", "taa-1953"),
    ("taa", "taa-1953"),
    ("the income tax assessment act 1997", "itaa-1997"),
    ("the income tax assessment act 1936", "itaa-1936"),
    ("the taxation administration act 1953", "taa-1953"),
    ("the corporations act 2001", "corporations-act-2001"),
    ("the goods and services tax act 1999", "gst-1999"),
]
_ALIASES = sorted(_ACT_ALIASES + _EXTRA_ALIASES, key=lambda a: len(a[0]), reverse=True)


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------

def _scan_file(path: str) -> tuple[list[str], list[str]]:
    unparsed: list[str] = []
    dropped: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return unparsed, dropped
    for ref in (d.get("legislation_refs_llm") or []) + (d.get("relevant_legislation") or []):
        if _parse_leg_ref(ref) is None and ref.strip():
            unparsed.append(ref.strip())
    for cit in (d.get("case_refs_llm") or []) + (d.get("case_references") or []):
        if _case_key(cit) is None and cit.strip():
            dropped.append(cit.strip())
    return unparsed, dropped


def collect() -> None:
    files = sorted(LLM_DIR.glob("*.json"))
    print(f"[entity] scanning {len(files)} files with 8 workers...")
    leg: Counter[str] = Counter()
    case: Counter[str] = Counter()
    with ProcessPoolExecutor(max_workers=8) as ex:
        for i, (unparsed, dropped) in enumerate(
            ex.map(_scan_file, [str(p) for p in files], chunksize=64)):
            for ref in unparsed:
                if len(ref) <= MAX_REF_LEN:
                    leg[ref] += 1
            for ref in dropped:
                if len(ref) <= MAX_REF_LEN:
                    case[ref] += 1
            if (i + 1) % 10_000 == 0:
                print(f"[entity] {i + 1}/{len(files)} | leg distinct {len(leg)} case distinct {len(case)}")
    CANDIDATES.write_text(json.dumps({"leg": dict(leg.most_common()), "case": dict(case.most_common())},
                                     indent=1, ensure_ascii=False))
    print(f"[entity] collected {len(leg)} distinct leg + {len(case)} distinct case refs → {CANDIDATES}")


# --------------------------------------------------------------------------
# local (deterministic)
# --------------------------------------------------------------------------

def _graph_key_sets() -> tuple[set[str], set[str]]:
    conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, timeout=10)
    try:
        section_keys = {r[0] for r in conn.execute(
            "SELECT key FROM nodes WHERE node_type='section'")}
        case_keys = {r[0] for r in conn.execute(
            "SELECT key FROM nodes WHERE node_type='case'")}
    finally:
        conn.close()
    return section_keys, case_keys


def _norm_party(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\b(federal\s+)?(commissioner|commissioners)\s+of\s+(taxation|inland\s+revenue)\b",
               "fcot", s)
    s = re.sub(r"\bfc of t\b|\bfcot\b|\bcomr\b|\bct\b", "fcot", s)
    s = re.sub(r"\b&amp;\b|\band\b|&", "and", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _case_alias_index() -> dict[str, list[str]]:
    """Normalised party-pair → [neutral citations] from the case corpus."""
    idx: dict[str, list[str]] = {}
    for name in ("hca_tax_cases", "fca_tax_cases", "fcafc_tax_cases", "aata_tax_cases"):
        p = DATA / f"{name}.json"
        if not p.exists():
            continue
        for case in json.loads(p.read_text()):
            title = case.get("title", "")
            cit = case.get("citation", "")
            if not title or not cit or " v " not in title:
                continue
            parties = sorted(_norm_party(x) for x in re.split(r"\s+v\.?\s+", title))
            key = " v ".join(parties)
            idx.setdefault(key, [])
            if cit not in idx[key]:
                idx[key].append(cit)
    return idx


CROSSWALK_FILE = DATA / "case_crosswalk.json"


def _load_crosswalk() -> dict[str, str]:
    """Neutral citation → reporter citation for cases the graph keys by reporter."""
    if CROSSWALK_FILE.exists():
        return json.loads(CROSSWALK_FILE.read_text())
    return {}


def _resolve_case_key(cit: str, case_keys: set[str], crosswalk: dict[str, str]) -> dict:
    """Map a neutral citation to a graph case key, honouring reporter-format keys."""
    k = f"case:{cit}"
    if k in case_keys:
        return {"status": "mapped", "key": k}
    rep = crosswalk.get(cit)
    if rep and f"case:{rep}" in case_keys:
        return {"status": "mapped", "key": f"case:{rep}"}
    return {"status": "unknown"}


def _map_case_ref(ref: str, case_keys: set[str], alias_idx: dict[str, list[str]],
                  crosswalk: dict[str, str] | None = None) -> dict:
    crosswalk = crosswalk if crosswalk is not None else _load_crosswalk()
    m = _NEUTRAL_RE.search(ref)
    if m:
        return _resolve_case_key(m.group(0), case_keys, crosswalk)
    if " v " not in ref:
        return {"status": "unknown"}
    parties = sorted(_norm_party(x) for x in re.split(r"\s+v\.?\s+", ref))
    hits = alias_idx.get(" v ".join(parties), [])
    if len(hits) == 1:
        return _resolve_case_key(hits[0], case_keys, crosswalk)
    if len(hits) > 1:
        # all hits must resolve, else ambiguous
        resolved = [_resolve_case_key(c, case_keys, crosswalk) for c in hits]
        keys = {r["key"] for r in resolved if r["status"] == "mapped"}
        if len(keys) == 1:
            return {"status": "mapped", "key": keys.pop()}
        return {"status": "ambiguous", "candidates": hits}
    # subset match: ref names fewer parties than the title (e.g. "Lunney v FC of T"
    # vs "Lunney v FC of T and Another") — only when unambiguous
    subset_hits = []
    for title_key, cits in alias_idx.items():
        tp = set(title_key.split(" v "))
        if set(parties) <= tp and len(parties) < len(tp):
            subset_hits.extend(cits)
    subset_hits = sorted(set(subset_hits))
    if len(subset_hits) == 1:
        return _resolve_case_key(subset_hits[0], case_keys, crosswalk)
    if len(subset_hits) > 1:
        return {"status": "ambiguous", "candidates": subset_hits}
    return {"status": "unknown"}


def _act_slug(ref: str) -> tuple[str | None, str]:
    """Find act alias in ref (prefix, "the " tolerated). Returns (slug, remainder)."""
    low = ref.lower()
    if low.startswith("the "):
        low = low[4:]
        for alias, slug in _ALIASES:
            if low.startswith(alias):
                return slug, ref[4 + len(alias):]
    for alias, slug in _ALIASES:
        if low.startswith(alias):
            return slug, ref[len(alias):]
    return None, ref


def _map_leg_ref(ref: str, section_keys: set[str]) -> dict:
    if _STATE_RE.search(ref):
        return {"status": "out_of_scope", "reason": "state/territory act"}
    if _REG_RE.search(ref):
        return {"status": "out_of_scope", "reason": "regulation"}

    # post-positioned act: "section 35-35 of the ITAA 1997"
    pm = _POST_RE.search(ref)
    if pm:
        sec = pm.group(1)
        slug, _ = _act_slug(pm.group(2))
        if slug:
            k = f"section:{slug}:{sec}"
            return {"status": "mapped" if k in section_keys else "out_of_scope",
                    "key": k if k in section_keys else None,
                    "reason": None if k in section_keys else "section node absent"}

    slug, rest = _act_slug(ref)
    if slug is None:
        # post-positioned non-section: "Division 900 of the ITAA 1997"
        if _NON_SECTION_POST_RE.match(ref):
            return {"status": "out_of_scope", "reason": "division/part/schedule"}
        # no act at all → the LLM may still know the act; keep for LLM pool
        return {"status": "unknown"}
    rest = rest.strip()
    if not rest:
        return {"status": "out_of_scope", "reason": "act name only"}
    if _NON_SECTION_RE.match(rest):
        return {"status": "out_of_scope", "reason": "division/part/schedule"}
    m = _SECTION_KW_RE.search(rest)
    if m:
        k = f"section:{slug}:{m.group(1)}"
        return {"status": "mapped" if k in section_keys else "out_of_scope",
                "key": k if k in section_keys else None,
                "reason": None if k in section_keys else "section node absent"}
    return {"status": "unknown"}


def local_stage() -> None:
    cand = json.loads(CANDIDATES.read_text())
    section_keys, case_keys = _graph_key_sets()
    alias_idx = _case_alias_index()
    crosswalk = _load_crosswalk()
    print(f"[entity] graph: {len(section_keys)} section keys, {len(case_keys)} case keys, "
          f"{len(alias_idx)} case party aliases, {len(crosswalk)} crosswalk entries")
    mapping: dict = {}
    if ALIAS_MAP.exists():
        mapping = json.loads(ALIAS_MAP.read_text())

    for kind in ("leg", "case"):
        refs = [r for r in cand[kind] if r not in mapping]
        for ref in refs:
            cnt = cand[kind][ref]
            if kind == "leg":
                res = _map_leg_ref(ref, section_keys)
            else:
                res = _map_case_ref(ref, case_keys, alias_idx, crosswalk)
            res["kind"] = kind
            res["count"] = cnt
            mapping[ref] = res
    ALIAS_MAP.write_text(json.dumps(mapping, indent=1, ensure_ascii=False))
    from collections import Counter as _C
    stats = _C(v["status"] for v in mapping.values())
    print(f"[entity] local done: {dict(stats)}")
    llm_pool = [r for r, v in mapping.items() if v["status"] == "unknown" and v["kind"] == "leg"]
    print(f"[entity] LLM pool: {len(llm_pool)} leg refs")


# --------------------------------------------------------------------------
# map (DeepSeek batch, resumable)
# --------------------------------------------------------------------------

def _chat_completion(prompt: str, max_tokens: int = 8000) -> str:
    """One DeepSeek chat call via stdlib urllib (no SDK dependency).

    The openai SDK is not installed in every interpreter that runs this
    script (background processes may resolve a different python), so we use
    urllib directly. Retries on 429/5xx with exponential backoff.

    max_tokens defaults high: a 400-ref batch emits ~12 output tokens per
    entry — a 2000-token cap truncates the JSON mid-object and the whole
    batch is lost (hit this in the first run).
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
    raise last_err  # type: ignore[misc]


def _llm_map_batch(refs: list[str], section_keys: set[str], counts: dict[str, int]) -> dict[str, dict]:
    lines = "\n".join(f"{i}. {r}" for i, r in enumerate(refs))
    prompt = f"""You are normalising Australian tax-law reference strings from private ruling texts. Each string names legislation — often with the act missing, abbreviated, or reordered ("FBTAA section 49", "s 35-55", "section 35-35 of the ITAA 1997", "Income Tax Assessment Act 1997 paragraph 35-55(1)(a)").

Map each string to a canonical section key of the form: section:<act-slug>:<section-number> where act-slug is one of: itaa-1997, itaa-1936, taa-1953, gst-1999, fbt-1986, corporations-act-2001, sis-1993, sga-1992, aml-ctf-2006, aml-ctf-rules-2007.
- Drop subsection/paragraph detail: "paragraph 35-55(1)(a)" -> section:itaa-1997:35-55.
- If the act is clearly one of the above but the section number is missing, or the act is NOT one of the above (state acts, regulations, treaties, Income Tax (Transitional Provisions) Act), or the string is prose/junk -> output "out_of_scope".
- If the string could mean provisions in MORE THAN ONE act (e.g. "s 6-5" could be ITAA 1997 or ITAA 1936) or you are not confident -> output "ambiguous".
- Never invent section numbers.

Reply with ONLY a JSON object mapping indices you resolved to a key, e.g. {{"3": "section:itaa-1997:35-55", "7": "ambiguous"}}. Indices you omit are treated as out_of_scope. No prose.

Strings:
{lines}"""
    try:
        text = _chat_completion(prompt)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            print(f"[entity] batch returned no JSON (len {len(text)}) — tail: "
                  f"{text[-200:]!r}", file=sys.stderr)
            return {}
        data = json.loads(m.group(0))
    except Exception as exc:  # noqa: BLE001
        print(f"[entity] batch failed after retries ({exc}) — refs stay unknown, retried next run",
              file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    for i, r in enumerate(refs):
        val = str(data.get(str(i), "out_of_scope")).strip()
        entry: dict = {"kind": "leg", "count": counts.get(r, 1)}
        if val == "ambiguous":
            entry["status"] = "ambiguous"
        elif val.startswith("section:"):
            # only accept keys that exist in the graph — no hallucinated edges
            if val in section_keys:
                entry["status"] = "mapped"
                entry["key"] = val
            else:
                entry["status"] = "out_of_scope"
                entry["reason"] = "LLM key absent from graph"
        else:
            entry["status"] = "out_of_scope"
        out[r] = entry
    return out


def map_stage(limit: int | None = None) -> None:
    cand = json.loads(CANDIDATES.read_text())
    section_keys, _ = _graph_key_sets()
    mapping: dict = {}
    if ALIAS_MAP.exists():
        mapping = json.loads(ALIAS_MAP.read_text())
    pool = [r for r, v in mapping.items() if v["status"] == "unknown" and v["kind"] == "leg"]
    # refs may also be entirely absent (ran map before local): include those too
    pool += [r for r in cand["leg"] if r not in mapping]
    pool = sorted(set(pool), key=lambda r: -cand["leg"].get(r, 1))
    if limit is not None:
        pool = pool[:limit]
    print(f"[entity] LLM pool: {len(pool)} leg refs")
    counts = cand["leg"]
    for i in range(0, len(pool), BATCH_SIZE):
        chunk = pool[i:i + BATCH_SIZE]
        mapping.update(_llm_map_batch(chunk, section_keys, counts))
        if (i // BATCH_SIZE) % 5 == 0 or limit is not None:
            ALIAS_MAP.write_text(json.dumps(mapping, indent=1, ensure_ascii=False))
            (ALIAS_MAP.with_suffix(".marker")).write_text(f"mapped {len(mapping)}\n")
            n_map = sum(1 for v in mapping.values() if v["status"] == "mapped")
            print(f"[entity] {i + len(chunk)}/{len(pool)} — mapped {n_map}")
    ALIAS_MAP.write_text(json.dumps(mapping, indent=1, ensure_ascii=False))
    (ALIAS_MAP.with_suffix(".marker")).write_text(f"mapped {len(mapping)}\n")
    from collections import Counter as _C
    print(f"[entity] done: {dict(_C(v['status'] for v in mapping.values()))} → {ALIAS_MAP}")


# --------------------------------------------------------------------------
# validate (G4 gate)
# --------------------------------------------------------------------------

def validate() -> int:
    mapping: dict = {}
    if ALIAS_MAP.exists():
        mapping = json.loads(ALIAS_MAP.read_text())
    if not mapping:
        print("[entity] no mapping yet — run `local` then `map`", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True)
    try:
        keys = {r["key"] for r in mapping.values() if r["status"] == "mapped"}
        ph = ",".join("?" * len(keys))
        found = {row[0] for row in conn.execute(
            f"SELECT key FROM nodes WHERE key IN ({ph})", list(keys))}
    finally:
        conn.close()

    missing = keys - found
    n_mapped = len(keys)
    n_ambig = sum(1 for r in mapping.values() if r["status"] == "ambiguous")
    n_oos = sum(1 for r in mapping.values() if r["status"] == "out_of_scope")
    n_unknown = sum(1 for r in mapping.values() if r["status"] == "unknown")

    print(f"[entity] G4: mapped {n_mapped} | ambiguous {n_ambig} | "
          f"out_of_scope {n_oos} | unknown {n_unknown}")
    print(f"[entity] G4: mapped keys resolving to graph nodes: {len(found)}/{n_mapped}")

    ok = True
    if missing:
        ok = False
        print(f"[entity] G4 FAIL — {len(missing)} mapped keys not in graph:")
        for k in sorted(missing)[:20]:
            print(f"    {k}")

    mapped_entries = sorted(
        ((r, v) for r, v in mapping.items() if v["status"] == "mapped"),
        key=lambda rv: -rv[1].get("count", 1),
    )
    REVIEW.write_text(json.dumps(
        [{"ref": r, "key": v["key"], "kind": v["kind"]} for r, v in mapped_entries[:100]],
        indent=1, ensure_ascii=False))
    print(f"[entity] review list ({min(100, len(mapped_entries))} entries) → {REVIEW}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["collect", "local", "map", "validate"])
    ap.add_argument("--limit", type=int, default=None,
                    help="map: smoke-test only the top-N pool refs (no full run)")
    args = ap.parse_args()
    if args.stage == "collect":
        collect()
    elif args.stage == "local":
        local_stage()
    elif args.stage == "map":
        map_stage(limit=args.limit)
    else:
        sys.exit(validate())
