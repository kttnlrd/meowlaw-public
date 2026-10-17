#!/usr/bin/env python3
"""Convert a LawKitty Act directory (tree.json + sections/**/*.md) into
MeowLaw's canonical JSON. Glue for this example only: any converter that
writes the same JSON works.

usage: convert_lawkitty.py <act dir> <corpus id> <title> <out.json> [--alias A ...]
       [--as-at DATE] [--source-url URL] [--licence TEXT]
"""
import argparse, json, re, sys
from pathlib import Path

ANCHOR = re.compile(r'<a id="[^"]*"></a>\s*')
FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)


def clean(md: str) -> str:
    md = FRONT.sub("", md)
    out = []
    for line in md.splitlines():
        if line.startswith("# "):          # section heading: carried in title
            continue
        line = ANCHOR.sub("", line)
        line = re.sub(r"^>\s?", "", line)  # blockquoted sub-paragraphs
        out.append(line.rstrip())
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("act_dir"); ap.add_argument("corpus"); ap.add_argument("title"); ap.add_argument("out")
    ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--as-at"); ap.add_argument("--source-url"); ap.add_argument("--licence")
    a = ap.parse_args()
    root = Path(a.act_dir)
    tree = json.loads((root / "tree.json").read_text())
    cid = a.corpus
    nodes = [{"id": cid, "corpus": cid, "kind": "root", "title": a.title, "text": ""}]
    used = {cid}
    counts = {}

    def add(parent, kind, label, number, title, text, seg, flat):
        pid = parent["id"]
        nid = f"{cid}:{seg}" if flat else (f"{pid}/{seg}" if pid != cid else f"{cid}:{seg}")
        if nid in used:
            nid = f"{pid}/{seg}" if pid != cid else f"{nid}~2"
        n = 2
        base = nid
        while nid in used:
            nid = f"{base}~{n}"; n += 1
        used.add(nid)
        counts[pid] = counts.get(pid, 0) + 1
        node = {"id": nid, "corpus": cid, "kind": kind, "label": label, "number": number,
                "title": title, "text": text, "parent": pid, "ordinal": counts[pid]}
        nodes.append(node)
        return node

    missing = 0

    def sections(parent, secs):
        nonlocal missing
        for s in secs:
            p = root / "sections" / s["path"]
            if not p.exists():
                missing += 1
                continue
            add(parent, "provision", "Section", s["id"], s.get("title"),
                clean(p.read_text(encoding="utf-8")), "s" + slug(s["id"]), True)

    for part in tree["parts"]:
        pn = add(nodes[0], "container", "Part", part["id"], part.get("title"), "", "pt" + slug(part["id"]), False)
        sections(pn, part.get("sections", []))
        for div in part.get("divisions", []):
            if div["id"] in ("unknown", ""):
                dn = pn
            else:
                dn = add(pn, "container", "Division", div["id"], div.get("title"), "", "div" + slug(div["id"]), False)
            sections(dn, div.get("sections", []))
            for sd in div.get("subdivisions", []):
                sn = add(dn, "container", "Subdivision", sd["id"], sd.get("title"), "", "sdiv" + slug(sd["id"]), False)
                sections(sn, sd.get("sections", []))

    corpus = {"id": cid, "title": a.title, "kind": "legislation", "jurisdiction": "AU-CTH",
              "profile": "au", "aliases": a.alias, "as_at": a.as_at or tree.get("compilation_date"),
              "source_url": a.source_url, "licence": a.licence,
              "meta": {"compilation_no": tree.get("compilation_no")}}
    Path(a.out).write_text(json.dumps({"corpus": corpus, "nodes": nodes}, ensure_ascii=False))
    print(f"{cid}: {len(nodes)} nodes, {sum(1 for n in nodes if n['kind']=='provision')} sections, {missing} missing files", file=sys.stderr)


if __name__ == "__main__":
    main()
