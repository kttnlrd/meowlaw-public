"""Content-type registry (plan §2): one content/<type>/type.yaml per type.

REGISTRY maps key -> type dict. Import fails on a duplicate key, a recommend
target naming an unknown type, or a non-default type without data_markers.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parent
_REQUIRED = ("key", "label", "default_configured", "loader", "display", "jurisdiction", "auth")
_ENUMS = {
    "display": {"tree", "paragraphs", "table", "sections", "document"},
    "jurisdiction": {"AU", "NZ", "INT"},
    "auth": {"user", "admin"},
}


def load(content_dir: Path = CONTENT_DIR) -> dict[str, dict]:
    reg: dict[str, dict] = {}
    for f in sorted(content_dir.glob("*/type.yaml")):
        t = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        missing = [k for k in _REQUIRED if k not in t or (k != "default_configured" and not t[k])]
        if missing:
            raise ValueError(f"{f}: missing {missing}")
        for field, allowed in _ENUMS.items():
            if t[field] not in allowed:
                raise ValueError(f"{f}: {field}={t[field]!r} not in {sorted(allowed)}")
        if not isinstance(t.get("default_configured"), bool):
            raise ValueError(f"{f}: default_configured must be true/false")
        if t["key"] in reg:
            raise ValueError(f"{f}: duplicate content type key {t['key']!r}")
        if not t["default_configured"] and not t.get("data_markers"):
            raise ValueError(f"{f}: non-default type {t['key']!r} has empty data_markers")
        t.setdefault("source_key", t["key"])
        t["recommend"] = t.get("recommend") or []
        t["data_markers"] = t.get("data_markers") or []
        reg[t["key"]] = t
    for t in reg.values():
        for rule in t["recommend"]:
            if rule.get("target") not in reg:
                raise ValueError(f"{t['key']}: recommend target {rule.get('target')!r} is not a registered type")
    return reg


REGISTRY = load()

# Stored/API source_type -> registry key (plan §7.2). A type whose key is its
# source_type with '_' for '-' (case, ruling, regulatory_guide, afsa_guide,
# treaty, commentary, ...) maps itself; the two exceptions are explicit.
SOURCE_TYPE_TO_CONTENT_TYPE = {k.replace("-", "_"): k for k in REGISTRY} | {
    "section": "legislation",
    "private_ruling": "ato-private-ruling",
}


def content_type_for(source_type: str | None) -> str | None:
    """Registry key for a search hit's source_type (None/blank = a section)."""
    return SOURCE_TYPE_TO_CONTENT_TYPE.get(source_type or "section")


def loader(key: str):
    # import_module accepts hyphenated package dirs (content.ato-private-ruling.loader)
    return importlib.import_module(REGISTRY[key]["loader"])


def tag(item: dict, source_type: str | None = None) -> dict:
    """Set item["content_type"] beside its source_type (kept for compat)."""
    st = source_type or item.get("source_type") or item.get("type")
    # same private-ruling test as search_facets.row_dims: some lanes emit
    # source_type "ruling" with act "private-rulings"
    if item.get("act") == "private-rulings" or "private_ruling" in (item.get("type"), item.get("source_type")):
        st = "private_ruling"
    ct = content_type_for(st)
    if ct == "ruling" and item.get("jurisdiction") == "NZ":
        ct = "nz-ruling"
    item["content_type"] = ct
    return item
