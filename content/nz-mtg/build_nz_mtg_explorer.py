#!/usr/bin/env python3
"""Build the NZ Master Tax Guide into corpus act format.

Same output shape as the Australian Master Tax Guide (frontmatter, tree.json,
section_index.json) but adds SLUG DE-DUPLICATION: the shared builder lets two
topics with the same slugified title overwrite each other (tree entries then
outnumber files and the earlier topic renders the later one's content).

Fix: when a slug is already used, suffix it with the paragraph number
(e.g. "tax-rates-¶50-020"), falling back to a counter.
"""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# The shared CCH builder lives in content/commentary/.
sys.path.insert(0, str(HERE.parent / "commentary"))

import build_cch_explorer as cch  # noqa: E402

OUTPUT_BASE = cch.OUTPUT_BASE
INPUT_DIR = Path(os.environ.get("NZ_MTG_OUT", ROOT / "pipeline" / "output" / "nz_master_tax_guide.json")).parent
META = {
    "id": "nz-master-tax-guide",
    "name": "New Zealand Master Tax Guide",
    "ch_label": "Ch",
    "compilation_no": 1,
    "compilation_date": "2026-01-01",
}


def build_pub(json_file: str, meta: dict) -> dict:
    pub_id, pub_name = meta["id"], meta["name"]
    pub_dir = OUTPUT_BASE / pub_id
    sections_dir = pub_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads((INPUT_DIR / json_file).read_text(encoding="utf-8"))
    tree = {"act": pub_name, "compilation_no": meta.get("compilation_no", ""),
            "compilation_date": meta.get("compilation_date", ""), "parts": []}
    section_index = []
    used_slugs: dict[str, int] = {}
    collisions = 0

    for ch in data.get("chapters", []):
        ch_num = ch.get("number", "")
        ch_title = cch.normalize_quotes(ch.get("title", ""))
        part_id = f"ch-{ch_num}" if ch_num else cch.slugify(ch_title)
        part = {"id": part_id,
                "title": f"{meta['ch_label']} {ch_num} — {ch_title}" if ch_num else ch_title,
                "sections": []}

        for mh in ch.get("major_headings", []):
            heading_title = cch.normalize_quotes(mh.get("title", ""))
            para = mh.get("paragraph_number", "")
            base = cch.slugify(heading_title) or f"{part_id}-{len(part['sections'])}"
            sec_id = base
            if sec_id in used_slugs:
                collisions += 1
                suffix = re.sub(r'[^\d]+', '-', para).strip('-') or str(used_slugs[sec_id] + 1)
                sec_id = f"{base}-p{suffix}"
                n = 2
                while sec_id in used_slugs:
                    sec_id = f"{base}-p{suffix}-{n}"
                    n += 1
            used_slugs[sec_id] = used_slugs.get(base, 0) + 1

            md_lines = [f"# {heading_title} {para}\n" if para else f"# {heading_title}\n"]
            for cb in mh.get("content_blocks", []):
                if cb.get("text"):
                    md_lines += [cch.clean_markdown_text(cb["text"]), ""]
                if cb.get("section_refs"):
                    md_lines += [f"*Refs: {', '.join(cb['section_refs'])}*", ""]
            for sh in mh.get("sub_headings", []):
                md_lines.append(f"## {cch.normalize_quotes(sh.get('title', ''))}\n")
                for cb in sh.get("content_blocks", []):
                    if cb.get("text"):
                        md_lines += [cch.clean_markdown_text(cb["text"]), ""]

            sec_path = f"{sec_id}.md"
            body = "\n".join(md_lines).strip()
            frontmatter = (f'---\nact: "{pub_name}"\npart: "{ch_num}"\nsection: "{sec_id}"\n'
                           f'title: "{heading_title}"\nparagraph: "{para}"\n---\n')
            (sections_dir / sec_path).write_text(frontmatter + body, encoding="utf-8")

            part["sections"].append({"id": sec_id, "title": heading_title, "path": sec_path})
            section_index.append({"id": sec_id, "title": heading_title, "paragraph": para,
                                  "chapter": ch_num, "chapter_title": ch_title})

        tree["parts"].append(part)

    tree["parts"].sort(key=lambda p: cch._natural_key(p["id"]))
    for part in tree["parts"]:
        part["sections"].sort(key=lambda s: cch._natural_key(s["id"]))

    (pub_dir / "tree.json").write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")
    (pub_dir / "section_index.json").write_text(json.dumps(section_index, indent=2, ensure_ascii=False),
                                                encoding="utf-8")
    print(f"  {pub_id}: {len(tree['parts'])} parts, {len(section_index)} sections, "
          f"{collisions} slug collisions de-duplicated")
    return tree


def main() -> int:
    src = INPUT_DIR / "nz_master_tax_guide.json"
    if not src.exists():
        print("missing input JSON:", src)
        return 1
    tree = build_pub("nz_master_tax_guide.json", META)

    # verify: every tree path exists on disk; no orphan files
    pub_dir = OUTPUT_BASE / META["id"]
    tree_paths = {s["path"] for p in tree["parts"] for s in p["sections"]}
    disk = {f.name for f in (pub_dir / "sections").glob("*.md")}
    print(f"tree entries: {len(tree_paths)} | files on disk: {len(disk)}")
    print(f"missing on disk: {len(tree_paths - disk)} | orphans: {len(disk - tree_paths)}")
    for m in sorted(tree_paths - disk)[:5]:
        print("   MISSING:", m)
    return 0 if tree_paths == disk else 2


if __name__ == "__main__":
    sys.exit(main())
