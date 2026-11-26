#!/usr/bin/env python3
"""
Build data/definitions_all.json — the catalog the backend serves to the UI.

Source of truth: the per-act definitions.json files (data/{act}/definitions.json),
which carry the canonical *per-term* anchors (e.g. "s995-1-4-build-to-rent-manner")
used everywhere else in the pipeline (pipeline/link_definitions.py) and backend
(backend/processors/markdown.py). Earlier versions of this script re-derived
anchors from the dictionary markdown and emitted a single generic subsection
anchor ("s995-1-1") for every term, so every defined-term link in the UI jumped
to the top of the dictionary section instead of the specific definition.

Output structure (unchanged, so the backend needs no changes):

    {
      "<act>": {
        "section": "<dictionary section id>",
        "terms": {
          "<display term>": {"anchor": "<per-term anchor>", "section": "<section>"},
          ...
        }
      }
    }

The backend lowercases term keys at load time and uses info.get("term") for
display, so we preserve the best available casing for each term.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
OUT_PATH = DATA_DIR / "definitions_all.json"

# Acts to include and their dictionary section id. Each is only emitted if its
# per-act definitions.json exists.
ACT_DICT_SECTION = {
    "itaa-1997": "995-1",
    "itaa-1936": "6",
    "gst-1999": "195-1",
    "taa-1953": None,  # section discovered from the per-act file if present
}


def is_junk_term(term: str) -> bool:
    """Skip obvious non-terms.

    At minimum, terms beginning with "(" are headings/fragments accidentally
    captured during extraction (e.g. the literal
    "(1) In this Act, except so far as the contrary intention appears").
    """
    t = term.strip()
    if not t:
        return True
    if t.startswith("("):
        return True
    # List-style continuation fragments ("film and", "... of") are not terms
    # (CDN-0172 class) — drop them from the served catalog as well.
    if re.search(r"\b(?:and|of|the|or)$", t, re.IGNORECASE):
        return True
    return False


def load_term_casing(act: str) -> dict[str, str]:
    """Best-effort map of lowercased term -> nicely-cased display term.

    Recovered from the previous definitions_all.json (which was extracted from
    the markdown and preserved casing), used as a fallback for per-act files
    that store only lowercased keys (e.g. itaa-1936).
    """
    if not OUT_PATH.exists():
        return {}
    try:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    terms = old.get(act, {}).get("terms", {})
    return {k.lower(): k for k in terms}


def build_act_terms(act: str, dict_section: str | None) -> dict | None:
    """Build the {section, terms} entry for one act from its definitions.json."""
    per_act_path = DATA_DIR / act / "definitions.json"
    if not per_act_path.exists():
        return None

    raw = json.loads(per_act_path.read_text(encoding="utf-8"))
    casing = load_term_casing(act)
    # Provenance of the per-act file, carried through to the served catalogue
    # (CDN-0209).  Extra keys are ignored by every consumer of this file
    # (data_loader, graph_etl, validate_data all read "section"/"terms").
    provenance = raw.get("_provenance") or {}

    terms: dict[str, dict] = {}
    sections_seen: set[str] = set()

    for key, info in raw.items():
        # Provenance and other underscore-prefixed metadata keys are not terms
        # (CDN-0209: extract_definitions.py writes a "_provenance" block).
        if key.startswith("_"):
            continue
        # Display term: prefer the explicit "term" field, then recovered casing,
        # then the (lowercased) key itself.
        display = info.get("term") or casing.get(key.lower()) or key
        if is_junk_term(display) or is_junk_term(key):
            continue

        anchor = info.get("anchor")
        section = info.get("section") or dict_section
        if not anchor or not section:
            continue
        sections_seen.add(section)
        terms[display] = {"anchor": anchor, "section": section}

    if not terms:
        return None

    # The catalog's top-level "section" is the dictionary section. Use the
    # configured one, else the most common section observed.
    section = dict_section
    if section is None:
        section = max(sections_seen, key=lambda s: sum(
            1 for v in terms.values() if v["section"] == s
        ))

    return {"section": section, "provenance": provenance, "terms": terms}


def validate_anchors(act: str, entry: dict, sample: int = 50) -> tuple[int, int]:
    """Sanity-check anchors against <a id="..."> tags in the section markdown.

    Returns (hits, checked). Intended only for the script's own logging; the
    per-term anchors come from the authoritative per-act files regardless.
    """
    import random

    sections_dir = DATA_DIR / act / "sections"
    anchor_cache: dict[str, set[str]] = {}

    def anchors_for(section: str) -> set[str]:
        if section in anchor_cache:
            return anchor_cache[section]
        found: set[str] = set()
        for f in sections_dir.rglob(f"{section}.md"):
            found = set(re.findall(r'<a id="([^"]+)">', f.read_text(encoding="utf-8")))
            break
        anchor_cache[section] = found
        return found

    items = list(entry["terms"].values())
    if len(items) > sample:
        items = random.sample(items, sample)
    hits = 0
    for v in items:
        if v["anchor"] in anchors_for(v["section"]):
            hits += 1
    return hits, len(items)


def source_commit() -> str:
    """The git commit the catalogue was merged at (provenance)."""
    try:
        return subprocess.run(
            ["git", "-C", str(BASE), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def main() -> None:
    catalog: dict[str, dict] = {}
    sha = source_commit()

    for act, dict_section in ACT_DICT_SECTION.items():
        entry = build_act_terms(act, dict_section)
        if entry is None:
            print(f"  SKIP {act}: no per-act definitions.json (or no usable terms)")
            continue
        # The served entry records its own producer as well as the per-act
        # producer it was merged from (CDN-0209).
        entry["provenance"] = {
            **(entry.get("provenance") or {}),
            "merged_by": "pipeline/extract_all_definitions.py",
            "merged_source_commit": sha,
        }
        catalog[act] = entry
        hits, checked = validate_anchors(act, entry)
        pct = (100 * hits / checked) if checked else 0.0
        print(
            f"  {act}: {len(entry['terms'])} terms (section {entry['section']}), "
            f"anchor<a-id> sample {hits}/{checked} = {pct:.1f}%"
        )

    # Atomic write: temp file in the same directory, then os.replace.
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    total = sum(len(v["terms"]) for v in catalog.values())
    print(f"\nWrote {total} terms across {len(catalog)} acts to {OUT_PATH}")


if __name__ == "__main__":
    main()
