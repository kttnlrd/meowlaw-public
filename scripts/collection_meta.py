"""Per-collection metadata: citation aliases and the licence line.

    python scripts/collection_meta.py aliases COLLECTION [--prefix MTG]
    python scripts/collection_meta.py licence COLLECTION --name N --holder H [--url U] [--statement S]

aliases: reads each section's front matter `citation` (or `paragraph`, the CCH style ¶25-320)
and writes data/<id>/aliases.json {normalised citation: doc id}. With --prefix the keys are also
written as "<prefix> <citation>" so "MTG ¶25-320" resolves to that guide only.

licence: writes data/<id>/licence.json in the format licence_notice() reads (one line under the
tree, and the `notice` MCP tool).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("¶", " ").strip().lower()).strip()


def front_matter(path: Path) -> dict:
    head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    m = _FM.match(head.replace("\r\n", "\n"))
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.strip('"')
        out[k.strip()] = v
    return out


def aliases(out: Path, prefix: str | None) -> int:
    tree = json.loads((out / "tree.json").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for part in tree.get("parts", []):
        secs = list(part.get("sections", []))
        for div in part.get("divisions", []):
            secs += div.get("sections", [])
        for s in secs:
            f = out / "sections" / str(s.get("path") or f"{s['id']}.md")
            if not f.is_file():
                continue
            fm = front_matter(f)
            cite = str(fm.get("citation") or fm.get("paragraph") or "").strip()
            if not cite:
                continue
            for key in {norm(cite)} | ({norm(f"{prefix} {cite}")} if prefix else set()):
                found.setdefault(key, str(s["id"]))
    (out / "aliases.json").write_text(json.dumps(found, ensure_ascii=False, indent=0) + "\n",
                                      encoding="utf-8", newline="\n")
    return len(found)


def licence(out: Path, cid: str, a) -> None:
    line = a.statement or f"{a.name}. Reproduced under licence from {a.holder} for licensed users only."
    rec = {
        "act": cid, "slug": cid, "jurisdiction": a.jurisdiction,
        "source_url": a.url or "", "terms_url": a.url or "",
        "licence": {"name": a.name, "url": a.url or "", "statement": line},
        "attribution": a.holder, "attribution_display": a.holder,
        "notice_line": line, "notice_badge": a.holder, "render": True,
    }
    (out / "licence.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n",
                                      encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=("aliases", "licence"))
    ap.add_argument("collection")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--prefix")
    ap.add_argument("--name", help="licence name, e.g. 'CCH licence'")
    ap.add_argument("--holder", help="rights holder, e.g. 'Wolters Kluwer (CCH)'")
    ap.add_argument("--url")
    ap.add_argument("--statement")
    ap.add_argument("--jurisdiction", default="AU")
    a = ap.parse_args()
    out = Path(a.data_dir) / a.collection
    if not (out / "tree.json").is_file():
        raise SystemExit(f"{out / 'tree.json'} not found")
    if a.what == "aliases":
        print(f"{a.collection}: {aliases(out, a.prefix)} aliases")
    else:
        if not (a.name and a.holder):
            raise SystemExit("licence needs --name and --holder")
        licence(out, a.collection, a)
        print(f"{a.collection}: licence.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
