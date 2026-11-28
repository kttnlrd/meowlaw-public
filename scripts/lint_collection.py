"""Lint a collection folder: the shape /api/tree, /api/section, search and MCP read.

    python scripts/lint_collection.py data/<id> [data/<id> ...]
    python scripts/lint_collection.py --all            # every data/*/collection.yaml

Errors (exit 1): collection.yaml missing, unparseable or not a mapping; access not one of
public | signed-in | restricted; tree.json missing or malformed; a part or section without
id/title; duplicate section ids; a section path that escapes sections/ or is missing; section
front matter missing or not naming this collection and section; a section file not in the tree;
aliases.json not a {citation: id} map or pointing at an unknown id; licence.json missing a
field licence_notice() needs.
Warnings: empty section bodies, a collection with no sections.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACCESS = ("public", "signed-in", "restricted")
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _fm(text: str, bad: list[str] | None = None) -> tuple[dict, str]:
    m = _FM.match(text.replace("\r\n", "\n"))
    if not m:
        return {}, text
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        try:
            out[k] = json.loads(v) if v.startswith('"') else v
        except ValueError:
            out[k] = v.strip('"')
            if bad is not None:
                bad.append(k)
        if bad is not None and out[k] in ("None", "null"):
            bad.append(f"{k} (None)")
    return out, text[m.end():]


def lint(folder: Path, meta: dict | None = None) -> tuple[list[str], list[str]]:
    """meta: lint a tree that has no collection.yaml yet (CCH, NZ MTG) as if it held this one;
    its front matter may then name the source by its display title instead of the folder id."""
    errs: list[str] = []
    warns: list[str] = []
    cid = folder.name
    if not _ID.match(cid):
        errs.append(f"collection id {cid!r} must be lowercase letters, digits and hyphens")

    meta_f = folder / "collection.yaml"
    in_place = meta is not None
    if not in_place:
        if not meta_f.is_file():
            return [f"{meta_f}: missing"], warns
        try:
            meta = yaml.safe_load(meta_f.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            return [f"{meta_f}: unreadable ({e.__class__.__name__})"], warns
    if not isinstance(meta, dict):
        return [f"{meta_f}: must be a mapping"], warns
    if meta.get("access") not in ACCESS:
        errs.append(f"{meta_f}: access {meta.get('access')!r} not one of {ACCESS}")
    for k in ("title", "group"):
        if not str(meta.get(k) or "").strip():
            errs.append(f"{meta_f}: {k} missing")

    tree_f = folder / "tree.json"
    try:
        tree = json.loads(tree_f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return errs + [f"{tree_f}: unreadable ({e.__class__.__name__})"], warns
    parts = tree.get("parts") if isinstance(tree, dict) else None
    if not isinstance(parts, list):
        return errs + [f"{tree_f}: no parts list"], warns

    sec_dir = (folder / "sections").resolve()
    ids: set[str] = set()
    paths: set[str] = set()
    n = 0
    for p in parts:
        if not isinstance(p, dict) or not str(p.get("id") or "") or not str(p.get("title") or ""):
            errs.append(f"{tree_f}: part without id/title: {str(p)[:80]}")
            continue
        secs = list(p.get("sections") or [])
        for d in p.get("divisions") or []:
            secs += d.get("sections") or []
        for s in secs:
            n += 1
            sid = str(s.get("id") or "")
            if not sid or not str(s.get("title") or "").strip():
                errs.append(f"{tree_f}: section without id/title in part {p['id']}: {str(s)[:80]}")
                continue
            if sid in ids:
                errs.append(f"{tree_f}: duplicate section id {sid!r}")
            ids.add(sid)
            rel = str(s.get("path") or f"{sid}.md")
            f = (sec_dir / rel).resolve()
            if not f.is_relative_to(sec_dir):
                errs.append(f"{tree_f}: section {sid!r} path {rel!r} escapes sections/")
                continue
            paths.add(str(f))
            if not f.is_file():
                errs.append(f"sections/{rel}: missing (section {sid!r})")
                continue
            badfm: list[str] = []
            fm, body = _fm(f.read_text(encoding="utf-8", errors="replace"), badfm)
            for k in badfm:
                warns.append(f"sections/{rel}: front matter {k} is not a clean quoted value")
            if not fm:
                errs.append(f"sections/{rel}: no front matter")
            else:
                if fm.get("act") != cid and not (in_place and fm.get("act") == tree.get("act")):
                    errs.append(f"sections/{rel}: front matter act {fm.get('act')!r} != {cid!r}")
                if str(fm.get("section")) != sid:
                    errs.append(f"sections/{rel}: front matter section {fm.get('section')!r} != {sid!r}")
            if not body.strip():
                warns.append(f"sections/{rel}: empty body")
    if n == 0:
        warns.append(f"{tree_f}: no sections")
    if sec_dir.is_dir():
        for f in sec_dir.rglob("*.md"):
            if str(f.resolve()) not in paths:
                errs.append(f"sections/{f.relative_to(sec_dir)}: not in tree.json")

    al = folder / "aliases.json"
    if al.exists():
        try:
            data = json.loads(al.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            errs.append(f"{al}: must be a {{citation: id}} map")
        else:
            bad = [k for k, v in data.items() if str(v) not in ids]
            if bad:
                errs.append(f"{al}: {len(bad)} alias(es) point at unknown ids, e.g. {bad[0]!r}")

    lic = folder / "licence.json"
    if lic.exists():
        try:
            d = json.loads(lic.read_text(encoding="utf-8"))
            missing = [k for k in ("jurisdiction", "source_url", "terms_url") if k not in d]
            missing += [f"licence.{k}" for k in ("name", "url") if k not in (d.get("licence") or {})]
        except (OSError, ValueError, AttributeError):
            missing = ["(unreadable)"]
        if missing:
            errs.append(f"{lic}: missing {', '.join(missing)}")
    return errs, warns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folders", nargs="*")
    ap.add_argument("--all", action="store_true", help="every data/*/collection.yaml")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    a = ap.parse_args()
    folders = [Path(f) for f in a.folders]
    if a.all:
        folders += sorted(f.parent for f in Path(a.data_dir).glob("*/collection.yaml"))
    if not folders:
        ap.error("name a folder or pass --all")
    bad = 0
    for f in folders:
        errs, warns = lint(f)
        print(f"{f.name}: {'FAIL' if errs else 'ok'} ({len(errs)} errors, {len(warns)} warnings)")
        for e in errs[:20]:
            print("  error:", e)
        for w in warns[:5]:
            print("  warn: ", w)
        if len(errs) > 20:
            print(f"  ... {len(errs) - 20} more errors")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------- run log
# Every ingest and real-data check appends one JSON line per collection, so recurring
# problems can be counted across runs (check_ingest_real.py --report).

def log_path() -> Path:
    import os
    return Path(os.environ.get("INGEST_LOG") or ROOT / "logs" / "ingest-checks.jsonl")


def issue_kind(msg: str) -> str:
    """The message with paths, ids and numbers blanked, so the same problem groups together."""
    msg = re.sub(r"\S+\.(?:md|json|yaml)\b", "<file>", msg)
    msg = re.sub(r"'[^']*'|\"[^\"]*\"", "<v>", msg)
    return re.sub(r"\d+", "<n>", msg)


def log_run(*, run: str, source: str, collection: str, adapter: str | None, docs: int | None,
            errors: list[str], warnings: list[str], status: str, note: str | None = None) -> None:
    import datetime
    import os
    kinds: dict[str, int] = {}
    for level, items in (("error", errors), ("warn", warnings)):
        for m in items:
            k = f"{level}: {issue_kind(m)}"
            kinds[k] = kinds.get(k, 0) + 1
    rec = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "run": run, "source": source, "collection": collection, "adapter": adapter,
           "docs": docs, "status": status, "errors": len(errors), "warnings": len(warnings),
           "kinds": kinds, "examples": (errors[:10] + warnings[:10]), "note": note,
           "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME")}
    p = log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:  # a read-only checkout must not fail the ingest
        print(f"(could not write {p}: {e})", file=sys.stderr)
