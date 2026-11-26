"""Entity alias resolution — raw ref strings -> canonical graph keys.

The entity-resolution backstop builds `data/entity_alias_map.json`
(53,707 raw refs -> {kind, status, key, count}) but nothing in the live
runtime consumed it. This service is the runtime bridge: it maps raw refs
(e.g. "FBTAA section 49", "Glenn v Federal Commissioner of Land Tax") to
canonical graph keys (e.g. "section:fbt-1986:49", "case:(1915) 20 CLR 490"),
verified against graph.db so no phantom keys reach search/graph responses.

Dependency-light on purpose: stdlib + sqlite3 only — the service env runs
python3.12 without tiktoken/openai and must not import them.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parents[2]
DATA_DIR = _BASE / "data"
ALIAS_MAP_PATH = DATA_DIR / "entity_alias_map.json"
CROSSWALK_PATH = DATA_DIR / "case_crosswalk.json"
GRAPH_DB_PATH = DATA_DIR / "graph.db"

# Module-level lazy caches. Frozen to empty on missing file -> lookup returns None.
_alias_map: dict[str, dict] | None = None
_normalized_index: dict[str, dict] | None = None
_crosswalk: dict[str, str] | None = None
_graph_keys: set[str] | None = None


def _norm(s: str) -> str:
    """Collapse whitespace and fold case for tolerant ref matching."""
    return " ".join(str(s).split()).lower()


def _load_alias_map() -> dict[str, dict]:
    global _alias_map, _normalized_index
    if _alias_map is None:
        raw: dict[str, dict] = {}
        try:
            raw = json.loads(ALIAS_MAP_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("[alias] cannot load %s: %s", ALIAS_MAP_PATH, exc)
        _alias_map = raw
        _normalized_index = {_norm(ref): entry for ref, entry in raw.items()}
    return _alias_map


def _load_crosswalk() -> dict[str, str]:
    global _crosswalk
    if _crosswalk is None:
        data: dict[str, str] = {}
        try:
            data = json.loads(CROSSWALK_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("[alias] cannot load %s: %s", CROSSWALK_PATH, exc)
        _crosswalk = data
    return _crosswalk


def _graph_key_set() -> set[str]:
    global _graph_keys
    if _graph_keys is None:
        _graph_keys = set()
        try:
            conn = sqlite3.connect(f"file:{GRAPH_DB_PATH}?mode=ro", uri=True)
            try:
                _graph_keys = {r[0] for r in conn.execute("SELECT key FROM nodes")}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("[alias] cannot open %s: %s", GRAPH_DB_PATH, exc)
        logger.info("[alias] cached %d graph node keys", len(_graph_keys))
    return _graph_keys


def _verify_key(key: str) -> str | None:
    """Return the key if it exists in graph.db, else None (no phantoms).

    Defensive crosswalk translation: a neutral case citation absent from
    graph.db (e.g. "case:[1915] HCA 57") resolves via case_crosswalk.json to
    the reporter key that does exist ("case:(1915) 20 CLR 490"). The alias map
    already carries resolved keys, so this is a belt-and-braces fallback.
    """
    keys = _graph_key_set()
    if key in keys:
        return key
    if key.startswith("case:"):
        reporter = _load_crosswalk().get(key[len("case:"):])
        if reporter:
            reporter_key = f"case:{reporter}"
            if reporter_key in keys:
                return reporter_key
    return None


def lookup(raw_ref: str) -> str | None:
    """Map a raw ref string to a canonical graph key, or None.

    Exact verbatim match first; then a case-insensitive, whitespace-normalised
    fallback (refs in the map are verbatim from ruling JSON, but query text may
    differ in case/spacing). Entries that are ambiguous/unknown/out_of_scope
    resolve to None. The returned key is always verified to exist in graph.db.
    """
    if not raw_ref:
        return None
    mapping = _load_alias_map()
    if not mapping:
        return None
    entry = mapping.get(raw_ref)
    if entry is None and _normalized_index is not None:
        entry = _normalized_index.get(_norm(raw_ref))
    if not entry or entry.get("status") != "mapped":
        return None
    key = entry.get("key")
    if not key:
        return None
    return _verify_key(key)
