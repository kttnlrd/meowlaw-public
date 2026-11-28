"""Ingest any source as a collection: data/<id>/collection.yaml + tree.json + sections/*.md.

    python scripts/ingest_collection.py SOURCE --collection ID [--title T] [--group G] [--access LEVEL]
    python scripts/ingest_collection.py SOURCE --collection ID --adapter NAME [...]
    python scripts/ingest_collection.py --collection ID [--title T] [--group G] [--access LEVEL]

SOURCE is one of:
  - a markdown file: "# Heading" starts a part, "## Heading" starts a document;
  - a folder of .md files: each subfolder is a part, each file a document (its first
    "# " line is the title, else the file name); files at the top level go in "Documents";
  - a .json file holding an array of {"title", "text", "part"?} objects;
  - with --adapter NAME, whatever scripts/adapters/NAME.py reads (e.g. keays). An adapter
    is one small file with parts(source) -> [{"title", "docs": [{"title", "text", "id"?}]}]
    and optional TITLE / GROUP defaults.

With no SOURCE, only collection.yaml is written: for a data/<id>/ that already holds a
tree.json and sections/ (the CCH guides, the NZ Master Tax Guide, the OECD commentary):

    ingest_collection.py --collection master-tax-guide --title "Australian Master Tax Guide" \\
        --group Commentary --access restricted

LEVEL is public, signed-in or restricted (default restricted). Rerunning replaces the
collection's documents and keeps its access unless --access is given. Nothing in SOURCE is
executed; it is only read as text.
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACCESS = ("public", "signed-in", "restricted")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "doc"


def from_markdown(text: str) -> list[dict]:
    parts: list[dict] = []
    part = doc = None
    for line in text.splitlines():
        if line.startswith("# "):
            part, doc = {"title": line[2:].strip(), "docs": []}, None
            parts.append(part)
        elif line.startswith("## "):
            if part is None:
                part = {"title": "Documents", "docs": []}
                parts.append(part)
            doc = {"title": line[3:].strip(), "text": ""}
            part["docs"].append(doc)
        elif doc is not None:
            doc["text"] += line + "\n"
    return parts


def from_folder(folder: Path) -> list[dict]:
    def doc_of(f: Path) -> dict:
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"\s*# (.+)\n", text)
        return {"title": m.group(1).strip() if m else f.stem.replace("-", " "),
                "text": text[m.end():] if m else text}
    parts = []
    top = sorted(f for f in folder.glob("*.md") if f.is_file() and not f.is_symlink())
    if top:
        parts.append({"title": "Documents", "docs": [doc_of(f) for f in top]})
    for sub in sorted(d for d in folder.iterdir() if d.is_dir() and not d.is_symlink()):
        files = sorted(f for f in sub.rglob("*.md") if f.is_file() and not f.is_symlink())
        if files:
            parts.append({"title": sub.name.replace("-", " ").strip(), "docs": [doc_of(f) for f in files]})
    return parts


def from_json(data) -> list[dict]:
    if not isinstance(data, list):
        raise SystemExit("JSON source must be an array of {title, text, part?} objects")
    parts: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        p = str(item.get("part") or "Documents")
        parts.setdefault(p, {"title": p, "docs": []})["docs"].append(
            {"title": str(item.get("title") or "Untitled"), "text": str(item.get("text") or "")})
    return list(parts.values())


def write_meta(out: Path, cid: str, title: str | None, group: str | None, access: str | None) -> dict:
    meta_path = out / "collection.yaml"
    meta = (yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}) if meta_path.exists() else {}
    meta["title"] = title or meta.get("title") or cid
    meta["group"] = group or meta.get("group") or "Collections"
    meta["access"] = access or meta.get("access") or "restricted"
    meta_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8",
                         newline="\n")
    return meta


def adapter(name: str):
    if not re.fullmatch(r"[a-z0-9_]+", name or ""):
        raise SystemExit(f"bad adapter name {name!r}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        return importlib.import_module(f"adapters.{name}")
    except ModuleNotFoundError:
        raise SystemExit(f"no adapter scripts/adapters/{name}.py")


def write(cid: str, parts: list[dict], title: str | None, group: str | None, access: str | None,
          data_dir: Path) -> tuple[int, int]:
    out = data_dir / cid
    out.mkdir(parents=True, exist_ok=True)
    meta = write_meta(out, cid, title, group, access)

    sections = out / "sections"
    if sections.exists():
        shutil.rmtree(sections)
    sections.mkdir()
    tree_parts, seen, n = [], set(), 0

    def put(docs: list[dict], part: str, division: str) -> list[dict]:
        nonlocal n
        secs = []
        for d in docs:
            sid = slug(d.get("id") or d["title"])
            while sid in seen:
                sid += "-2"
            seen.add(sid)
            fm = {"act": cid, "section": sid, "title": d["title"], "part": part, "division": division}
            # Minimal standard front matter an adapter may supply: citation, date, status.
            fm.update({k: d[k] for k in ("citation", "date", "status") if d.get(k)})
            (sections / f"{sid}.md").write_text(
                "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fm.items())
                + "---\n" + d["text"].strip() + "\n", encoding="utf-8", newline="\n")
            secs.append({"id": sid, "title": d["title"], "path": f"{sid}.md"})
            n += 1
        return secs

    for i, p in enumerate(parts, 1):
        # A part may split its docs into titled groups ("groups": [{"title", "docs"}]): one
        # division each, so a very large part (a court's cases) opens one group at a time.
        divs = []
        for j, g in enumerate(p.get("groups") or [], 1):
            gs = put(g["docs"], str(i), f"{i}.{j}")
            if gs:
                divs.append({"id": f"{i}.{j}", "title": g["title"], "sections": gs})
        secs = put(p.get("docs") or [], str(i), "")
        if secs or divs:
            tree_parts.append({"id": str(i), "title": p["title"], "divisions": divs, "sections": secs})
    (out / "tree.json").write_text(json.dumps({"act": meta["title"], "parts": tree_parts}, indent=1,
                                              ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return len(tree_parts), n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", nargs="?")
    ap.add_argument("--collection", required=True)
    ap.add_argument("--title")
    ap.add_argument("--group")
    ap.add_argument("--access", choices=ACCESS)
    ap.add_argument("--adapter", help="scripts/adapters/<NAME>.py converts SOURCE")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    a = ap.parse_args()
    cid = slug(a.collection)
    if a.source is None:  # metadata only, for a tree that is already in place
        out = Path(a.data_dir) / cid
        if not (out / "tree.json").is_file():
            print(f"{out / 'tree.json'} not found: pass a SOURCE to ingest", file=sys.stderr)
            return 1
        print(f"{cid}: {write_meta(out, cid, a.title, a.group, a.access)}")
        return 0
    src = Path(a.source)
    if a.adapter:
        mod = adapter(a.adapter)
        parts = mod.parts(src)
        a.title = a.title or getattr(mod, "TITLE", None)
        a.group = a.group or getattr(mod, "GROUP", None)
    elif src.is_dir():
        parts = from_folder(src)
    elif src.suffix.lower() == ".json":
        parts = from_json(json.loads(src.read_text(encoding="utf-8")))
    else:
        parts = from_markdown(src.read_text(encoding="utf-8", errors="replace"))
    if not any(p.get("docs") or any(g["docs"] for g in p.get("groups") or []) for p in parts):
        print("nothing to ingest: no documents found", file=sys.stderr)
        return 1
    np, nd = write(cid, parts, a.title, a.group, a.access, Path(a.data_dir))
    print(f"{cid}: {np} parts, {nd} documents -> {Path(a.data_dir) / cid}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lint_collection import lint, log_run
    errs, warns = lint(Path(a.data_dir) / cid)
    if a.adapter:
        warns = list(getattr(mod, "ISSUES", [])) + warns
        for w in getattr(mod, "ISSUES", [])[:20]:
            print("  adapter warning:", w, file=sys.stderr)
    log_run(run="ingest", source=str(src), collection=cid, adapter=a.adapter, docs=nd,
            errors=errs, warnings=warns, status="fail" if errs else "ok")
    for e in errs[:20]:
        print("  lint error:", e, file=sys.stderr)
    print(f"lint: {len(errs)} errors, {len(warns)} warnings")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
