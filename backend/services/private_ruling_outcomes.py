"""Outcome enrichment for private ruling search results.

Loads data/private_rulings_outcomes.json (built by
scripts/build_private_ruling_outcomes.py) and exposes:

  get(authnum)        -> outcome record or None
  enrich(result)      -> attach outcome/qa/date/subject to a result dict
  filter_outcome(rs, outcome) -> keep results whose outcome label matches

The outcome label is a search aid (how the ATO answered the taxpayer's own
question), NOT a legal characterisation.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parents[2]
OUTCOMES_PATH = _BASE / "data" / "private_rulings_outcomes.json"

_cache: dict[str, dict] | None = None

VALID_OUTCOMES = {"yes", "no", "mixed"}


def load_outcomes() -> dict[str, dict]:
    global _cache
    if _cache is None:
        data: dict[str, dict] = {}
        try:
            data = json.loads(OUTCOMES_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("[outcomes] cannot load %s: %s", OUTCOMES_PATH, exc)
        _cache = data
        logger.info("[outcomes] loaded %d rulings", len(_cache))
    return _cache


def get(authnum: str) -> dict | None:
    return load_outcomes().get(authnum)


def enrich(result: dict) -> dict:
    """Attach outcome metadata to a private_ruling search result in place."""
    auth = result.get("section") or ""
    rec = get(auth)
    if not rec:
        return result
    result.setdefault("title", result.get("title") or rec["name"])
    result["outcome"] = rec["outcome"] or "unknown"
    result["qa"] = rec["qa"]
    if not result.get("date") and rec["date_of_advice"]:
        result["date"] = rec["date_of_advice"]
    if not result.get("subject") and rec["subject"]:
        result["subject"] = rec["subject"]
    return result


def outcome_matches(rec_outcome: str, want: str) -> bool:
    if not want:
        return True
    if want not in VALID_OUTCOMES:
        return False
    return rec_outcome == want


def filter_results(results: list[dict], outcome: str | None) -> list[dict]:
    """Post-filter private ruling results by outcome label (case-insensitive)."""
    if not outcome:
        return results
    want = outcome.strip().lower()
    if want not in VALID_OUTCOMES:
        return results
    kept = []
    for r in results:
        if r.get("source_type") != "private_ruling":
            kept.append(r)
            continue
        rec = get(r.get("section") or "")
        if rec and outcome_matches(rec.get("outcome") or "", want):
            kept.append(r)
    return kept
