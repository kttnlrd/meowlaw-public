"""Collections: any folder data/<id>/ with a collection.yaml next to its tree.json.

A collection is served by the same machinery as every other source (/api/acts, /api/tree,
/api/section, search, MCP outline/fetch), so ingesting new material never needs new code.
collection.yaml holds what that machinery cannot infer:

    title: Firm KB
    group: Firm knowledge          # sidebar group
    access: restricted             # public | signed-in | restricted

Access: public = anyone; signed-in = any logged-in user (or MCP token owner); restricted =
admins plus users granted the collection id (the existing grants table and admin panel).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import yaml

from backend.config import DATA_DIR

ACCESS = ("public", "signed-in", "restricted")
_lock = threading.Lock()
_cache: tuple[tuple, dict] | None = None


_files_cache: tuple[float, str, list[Path]] = (0.0, "", [])


def _files() -> list[Path]:
    """data/*/collection.yaml. One stat per folder (never a glob: Path.glob lists every file in
    every folder, 70,000 in case_texts), and the list is reused for a few seconds."""
    global _files_cache
    now = time.monotonic()
    if now - _files_cache[0] < 5 and _files_cache[1] == str(DATA_DIR):
        return _files_cache[2]
    try:
        found = sorted(f for d in DATA_DIR.iterdir() if (f := d / "collection.yaml").is_file())
    except OSError:
        found = []
    _files_cache = (now, str(DATA_DIR), found)
    return found


def all_collections() -> dict[str, dict]:
    """{id: {id, title, group, access}}; re-read when any collection.yaml changes."""
    global _cache
    files = _files()
    stamp = tuple((str(f), f.stat().st_mtime_ns) for f in files if f.exists())
    with _lock:
        if _cache and _cache[0] == stamp:
            return _cache[1]
    out: dict[str, dict] = {}
    for f in files:
        try:
            meta = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            meta = {}  # unreadable: keep it registered, so it falls back to restricted
        if not isinstance(meta, dict):
            meta = {}
        cid = f.parent.name.lower()
        access = str(meta.get("access", "restricted")).lower()
        out[cid] = {
            "id": cid,
            "title": str(meta.get("title") or cid),
            "group": str(meta.get("group") or "Collections"),
            "access": access if access in ACCESS else "restricted",  # unknown -> safest
        }
    with _lock:
        _cache = (stamp, out)
    return out


def get(cid: str) -> dict | None:
    return all_collections().get((cid or "").lower())


def set_access(cid: str, access: str) -> dict:
    """Admin: change a collection's access level (rewrites its collection.yaml)."""
    if access not in ACCESS:
        raise ValueError(f"access must be one of {ACCESS}")
    cid = (cid or "").lower()
    if cid not in all_collections():  # only known collection ids ever reach the filesystem
        raise KeyError(cid)
    f = DATA_DIR / cid / "collection.yaml"
    try:
        meta = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["access"] = access
    tmp = f.with_suffix(f".yaml.{threading.get_ident()}.tmp")
    tmp.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(f)
    return all_collections()[cid]


_alias_cache: tuple[tuple, dict] | None = None


def _norm_cite(s: str) -> str:
    return " ".join(s.replace("¶", " ").lower().split())


def alias_lookup(citation: str) -> list[tuple[str, str]]:
    """[(collection id, doc id)] for a citation listed in any data/<id>/aliases.json
    (written by scripts/collection_meta.py). Access is checked by the caller, as for any id."""
    global _alias_cache
    files = [DATA_DIR / cid / "aliases.json" for cid in all_collections()]
    files = [f for f in files if f.is_file()]
    stamp = tuple((str(f), f.stat().st_mtime_ns) for f in files)
    with _lock:
        cached = _alias_cache if _alias_cache and _alias_cache[0] == stamp else None
    if cached is None:
        index: dict[str, list[tuple[str, str]]] = {}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for k, doc in (data.items() if isinstance(data, dict) else ()):
                index.setdefault(_norm_cite(str(k)), []).append((f.parent.name.lower(), str(doc)))
        cached = (stamp, index)
        with _lock:
            _alias_cache = cached
    return cached[1].get(_norm_cite(citation or ""), [])
