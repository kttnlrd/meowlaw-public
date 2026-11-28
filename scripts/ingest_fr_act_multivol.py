#!/usr/bin/env python3
"""Multi-volume driver for ingest_fr_act.py.

The Federal Register publishes large Acts as several Word volumes (e.g. the
Migration Act 1958 compilation 171 is 2 volumes: "s 1-261K" and
"s 262-507, sch, Endnotes"). Each volume carries its own Contents page, and
only the volume that opens the Act has the LongT ("An Act ...") marker that
ingest_fr_act.build_body_map uses to detect the start of the body text. The
single-volume ingest_fr_act.py therefore cannot ingest these Acts on its own:
running it per volume yields a partial TOC with no body for volume 2.

This driver adapts the single-volume pipeline to N volumes without touching
ingest_fr_act.py (which is shared):

  1. parses each volume's Contents page and merges the resulting
     Part/Division/Subdivision trees by id, so a Part whose divisions are split
     across volumes is reassembled in document order;
  2. builds one combined paragraph stream: volume 1 in full (so the LongT that
     flips build_body_map into "in body" mode is present) plus, for every later
     volume, only the paragraphs from the start of its body onward (its
     front matter and Contents page are dropped so they are not swallowed into
     the previous section's text);
  3. calls ingest_fr_act.build_body_map / get_structure_titles / write_section
     unchanged, against a light stand-in for the Document that simply exposes
     the combined paragraph list.

Body start per volume: the LongT paragraph if present, otherwise the first
ActHead heading after the last Contents entry (later volumes have no LongT).

Usage:
    python3 scripts/ingest_fr_act_multivol.py \
        --docx data/<slug>/raw/<slug>-vol1.docx \
        --docx data/<slug>/raw/<slug>-vol2.docx \
        --out-dir data/<slug> \
        --act-name 'Migration Act 1958' \
        --compilation-no 171 \
        --compilation-date '4 June 2026'

Produces the same layout as ingest_fr_act.py:
  data/<slug>/tree.json
  data/<slug>/sections/{part-*/division-*}/{section}.md
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

from docx import Document

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ingest_fr_act", _HERE / "ingest_fr_act.py")
ifa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ifa)

_HEAD_STYLES = ("ActHead 1", "ActHead 2", "ActHead 3", "ActHead 4", "ActHead 5")


class CombinedDoc:
    """Stand-in for a python-docx Document exposing one flat paragraph list."""

    def __init__(self, paragraphs):
        self.paragraphs = paragraphs


def _last_toc_index(doc: Document) -> int:
    idx = -1
    for i, p in enumerate(doc.paragraphs):
        if (p.style.name or "").startswith("toc"):
            idx = i
    return idx


def body_start(doc: Document) -> int:
    """Index of the first body paragraph of a single volume."""
    for i, p in enumerate(doc.paragraphs):
        if (p.style.name or "") == "LongT":
            return i
    last_toc = _last_toc_index(doc)
    for i, p in enumerate(doc.paragraphs):
        if i > last_toc and (p.style.name or "") in _HEAD_STYLES:
            return i
    return 0


def volume_slice(doc: Document, is_first: bool) -> list:
    """Paragraphs of one volume to feed into the combined stream.

    The first volume is taken whole so that build_body_map can find the LongT
    that switches it into body mode. Later volumes are trimmed to their body;
    a leading LongT (if any) is dropped because build_body_map would otherwise
    append it to the preceding volume's last section.
    """
    if is_first:
        return list(doc.paragraphs)
    start = body_start(doc)
    paras = list(doc.paragraphs)
    if start < len(paras) and (paras[start].style.name or "") == "LongT":
        start += 1
    return paras[start:]


def schedule_parts(doc: Document, existing_ids: set) -> list[dict]:
    """TOC 'Schedule ...' entries the base parser misses.

    ingest_fr_act.parse_toc only recognises toc 1 entries of the form
    "Schedule <n>—...". Several Acts instead head the schedule "The
    Schedule—...", which the base parser silently drops.
    """
    out = []
    for p in doc.paragraphs:
        if (p.style.name or "") != "toc 1":
            continue
        text = ifa.para_text(p).strip()
        m = re.match(r"(?:The\s+)?Schedule(?:\s+(\d+))?\s*[\u2014\-\u2013]", text)
        if not m:
            continue
        pid = f"schedule-{m.group(1) or 1}"
        if pid in existing_ids:
            continue
        title = ifa.clean(ifa.strip_toc_page(text))
        out.append({
            "id": pid,
            "title": title,
            "divisions": [{"id": "1", "title": title, "subdivisions": [], "sections": []}],
        })
        existing_ids.add(pid)
    return out


def merge_parts(dst: list[dict], src: list[dict]) -> list[dict]:
    """Merge src's Part/Division/Subdivision tree into dst, keyed by id."""
    index = {p["id"]: p for p in dst}
    for part in src:
        if part["id"] not in index:
            dst.append(part)
            index[part["id"]] = part
            continue
        tgt = index[part["id"]]
        if not tgt.get("divisions") and part.get("divisions"):
            tgt["divisions"] = part["divisions"]
            continue
        dmap = {d["id"]: d for d in tgt["divisions"]}
        for div in part["divisions"]:
            if div["id"] not in dmap:
                tgt["divisions"].append(div)
                dmap[div["id"]] = div
                continue
            tdiv = dmap[div["id"]]
            smap = {s["id"]: s for s in tdiv["subdivisions"]}
            for sub in div["subdivisions"]:
                if sub["id"] not in smap:
                    tdiv["subdivisions"].append(sub)
                    smap[sub["id"]] = sub
                else:
                    smap[sub["id"]]["sections"].extend(sub["sections"])
            tdiv["sections"].extend(div["sections"])
    return dst


def merge_titles(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        dst.setdefault(k, v)
    return dst


def main():
    ap = argparse.ArgumentParser(description="Ingest a multi-volume FR DOCX act")
    ap.add_argument("--docx", action="append", required=True,
                    help="volume DOCX (repeat once per volume, in order)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", required=True)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"
    act_name = args.act_name
    compilation_no = args.compilation_no
    compilation_date = args.compilation_date

    docs = []
    for path in args.docx:
        print(f"Reading DOCX: {path}")
        docs.append(Document(str(path)))

    parts: list[dict] = []
    part_titles: dict = {}
    div_titles: dict = {}
    subdiv_titles: dict = {}
    combined_paras: list = []

    for vi, doc in enumerate(docs):
        print(f"Parsing TOC (volume {vi + 1}/{len(docs)})...")
        vol_parts = ifa.parse_toc(doc)
        vol_parts += schedule_parts(doc, {p["id"] for p in vol_parts} |
                                    {p["id"] for p in parts})
        merge_parts(parts, vol_parts)
        pt, dt, st = ifa.get_structure_titles(doc)
        merge_titles(part_titles, pt)
        merge_titles(div_titles, dt)
        merge_titles(subdiv_titles, st)
        combined_paras.extend(volume_slice(doc, vi == 0))
        print(f"  volume {vi + 1}: {len(vol_parts)} parts in TOC, "
              f"{len(volume_slice(doc, vi == 0))} paragraphs in stream")

    doc = CombinedDoc(combined_paras)

    print("Building section content map (combined stream)...")
    body_map = ifa.build_body_map(doc)
    print(f"Found {len(body_map)} body section entries")
    if not body_map:
        raise SystemExit("No body sections found — no volume carries a LongT "
                         "marker; this driver needs one to enter body mode.")

    tree = {
        "act": act_name,
        "compilation_no": compilation_no,
        "compilation_date": compilation_date,
        "parts": [],
    }
    total_sections = 0
    empty_sections = []

    for part in parts:
        pid = part["id"]
        pt = ifa.clean(part_titles.get(pid, part["title"]))
        part_entry = {"id": pid, "title": pt, "divisions": []}

        for div in part["divisions"]:
            did = div["id"]
            dt = ifa.clean(div_titles.get((pid, did), div["title"]))
            div_entry = {"id": did, "title": dt, "subdivisions": [], "sections": []}

            for sec in div["sections"]:
                sid = sec["id"]
                info = ifa._find_body_entry(body_map, sid, pid, doc)
                st = info.get("title") or ifa.clean(sec["title"])
                body_text = info.get("content", "")
                path = f"part-{pid}/division-{did}/{sid}.md"
                div_entry["sections"].append({"id": sid, "title": st, "path": path})
                ifa.write_section(section_dir / f"part-{pid}" / f"division-{did}",
                                  sid, st, body_text, act_name, compilation_no,
                                  compilation_date, pid, pt, did, dt)
                total_sections += 1
                if not body_text:
                    empty_sections.append(f"{pid}/{did}/{sid}")

            for subdiv in div["subdivisions"]:
                sd_id = subdiv["id"]
                sd_title = ifa.clean(subdiv_titles.get((pid, did, sd_id), subdiv["title"]))
                subdiv_entry = {"id": sd_id, "title": sd_title, "sections": []}
                for sec in subdiv["sections"]:
                    sid = sec["id"]
                    info = ifa._find_body_entry(body_map, sid, pid, doc)
                    st = info.get("title") or ifa.clean(sec["title"])
                    body_text = info.get("content", "")
                    path = f"part-{pid}/division-{did}/{sid}.md"
                    subdiv_entry["sections"].append({"id": sid, "title": st, "path": path})
                    ifa.write_section(section_dir / f"part-{pid}" / f"division-{did}",
                                      sid, st, body_text, act_name, compilation_no,
                                      compilation_date, pid, pt, did, dt, sd_id, sd_title)
                    total_sections += 1
                    if not body_text:
                        empty_sections.append(f"{pid}/{did}/{sid}")
                div_entry["subdivisions"].append(subdiv_entry)

            part_entry["divisions"].append(div_entry)
        tree["parts"].append(part_entry)

    tree_path = out_dir / "tree.json"
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    n_divs = sum(len(p["divisions"]) for p in parts)
    n_subdivs = sum(len(d["subdivisions"]) for p in parts for d in p["divisions"])
    print(f"\n{'=' * 60}\nSUMMARY\n{'=' * 60}")
    print(f"Volumes: {len(docs)}")
    print(f"Parts: {len(parts)}")
    print(f"Divisions: {n_divs}")
    print(f"Subdivisions: {n_subdivs}")
    print(f"Section files written: {total_sections}")
    print(f"Section files on disk: {len(sec_files)}")
    print(f"Empty-body sections: {len(empty_sections)}")
    if empty_sections:
        print("  first 15:", empty_sections[:15])
    print("Done.")


if __name__ == "__main__":
    main()
