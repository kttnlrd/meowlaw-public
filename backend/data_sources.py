"""data_sources manifest (plan §3): where each content type's data lives here.

Composition, later wins per source key:
  1. data_sources.yaml          committed, default sources only (checked at import, §6.1)
  2. data_sources.local.yaml    git-ignored overlay; {path: null} unsets a default
  3. env LAWKITTY_SOURCE_<KEY>  (plus the legacy *_DIR shim, deprecated)
allow_optional_sources is a file decision only: env never sets it.

    python -m backend.data_sources --dump              key status count path
    python -m backend.data_sources --assert-default-only  non-zero if an optional source resolves
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from content.registry import REGISTRY, loader

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "data_sources.yaml"
LOCAL = ROOT / "data_sources.local.yaml"

# Legacy env names -> registry key. ponytail: one release, then delete.
LEGACY_ENV = {
    "CASE_DIR": "case",
    "RULING_DIR": "ruling",
    "ATO_RULING_DIR": "ruling",
    "NZ_RULING_DIR": "nz-ruling",
    "COMMENTARY_DIR": "commentary",
    "PRIVATE_RULINGS_ROOT": "ato-private-ruling",  # corpus root; JSON lives in data/json
}
_OPTIONAL_SOURCES = {t["source_key"] for t in REGISTRY.values() if not t["default_configured"]}


@dataclass(frozen=True)
class Source:
    key: str
    path: Path | None
    status: str  # ok | not_configured | missing


def _read(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}


def check_committed(m: dict) -> None:
    """§6.1: the committed manifest may never enable or point at optional data."""
    if m.get("allow_optional_sources") is not False:
        raise RuntimeError("data_sources.yaml must set allow_optional_sources: false")
    bad = sorted(_OPTIONAL_SOURCES & set(m.get("sources") or {}))
    if bad:
        raise RuntimeError(f"data_sources.yaml lists optional sources {bad}; "
                           "move them to data_sources.local.yaml")


def compose(base: dict, local: dict, env=os.environ) -> dict:
    m = {**base, **{k: v for k, v in local.items() if k != "sources"}}
    sources = {k: dict(v or {}) for k, v in (base.get("sources") or {}).items()}
    for k, v in (local.get("sources") or {}).items():
        sources[k] = {**sources.get(k, {}), **(v or {})}
    for name, key in LEGACY_ENV.items():
        if env.get(name):
            logger.warning("DeprecationWarning: env %s is deprecated, use LAWKITTY_SOURCE_%s",
                           name, key.upper().replace("-", "_"))
            val = env[name]
            if name == "PRIVATE_RULINGS_ROOT":
                val = str(Path(val) / "data" / "json")
            sources.setdefault(key, {})["path"] = val
    for key in {t["source_key"] for t in REGISTRY.values()}:
        val = env.get("LAWKITTY_SOURCE_" + key.upper().replace("-", "_"))
        if val:
            sources.setdefault(key, {})["path"] = val
    m["sources"] = sources
    return m


def manifest(committed: Path = COMMITTED, local: Path = LOCAL, env=os.environ) -> dict:
    base = _read(committed)
    check_committed(base)
    return compose(base, _read(local), env)


def _abs(raw: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    return p if p.is_absolute() else ROOT / p


def resolve(m: dict | None = None) -> dict[str, Source]:
    """Every registered type -> Source(path, status)."""
    m = manifest() if m is None else m
    out = {}
    for key, t in REGISTRY.items():
        raw = (m["sources"].get(t["source_key"]) or {}).get("path")
        if raw is None or (not t["default_configured"] and m.get("allow_optional_sources") is not True):
            out[key] = Source(key, None, "not_configured")
            continue
        p = _abs(raw)
        if p.exists() and os.access(p, os.R_OK):
            out[key] = Source(key, p, "ok")
        else:
            logger.error("data source %r: %s is missing or unreadable", key, p)
            out[key] = Source(key, p, "missing")
    return out


def content_types(sources: dict[str, Source]) -> list[dict]:
    """The /api/content-types rows; counts come from each type's loader."""
    rows = []
    for key, t in REGISTRY.items():
        s = sources[key]
        count = 0
        if s.status == "ok":
            try:
                count = loader(key).count(s.path)
            except Exception:
                logger.exception("content type %r: count failed", key)
                count = None
        rows.append({"key": key, "label": t["label"], "default_configured": t["default_configured"],
                     "display": t["display"],
                     "status": s.status, "count": count, "jurisdiction": t["jurisdiction"]})
    return rows


def _main(argv: list[str]) -> int:
    sources = resolve()
    if "--assert-default-only" in argv:
        leaked = [k for k, s in sources.items()
                  if not REGISTRY[k]["default_configured"] and s.status != "not_configured"]
        if leaked:
            print(f"optional sources resolve on this instance: {leaked}", file=sys.stderr)
            return 1
        print("ok: every optional source is not_configured")
    if "--dump" in argv:
        for row in content_types(sources):
            print(f"{row['key']:<20} {row['status']:<15} {row['count']!s:>7}  {sources[row['key']].path or '-'}")
    if not {"--assert-default-only", "--dump"} & set(argv):
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
