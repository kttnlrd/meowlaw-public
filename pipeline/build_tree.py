"""
build_tree.py — Walk emitted markdown and produce tree.json.

Usage:
    python3 pipeline/build_tree.py --sections-dir data/itaa-1997/sections \
                                   --out-file data/itaa-1997/tree.json \
                                   --act "ITAA 1997" \
                                   --compilation-no 263 \
                                   --compilation-date 2026-04-01
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _natural_key(s: str):
    """Natural sort key: '2' < '10', '83A' after '83'."""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]


_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}


def _roman_numeral_value(s: str) -> int:
    """Convert a Roman numeral to an integer (I, II, IIIB, IVA, ...)."""
    total = 0
    prev = 0
    for ch in reversed(s):
        v = _ROMAN_VALUES.get(ch, 0)
        if v == 0:
            continue
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total


def _part_sort_key(part_id: str):
    """Part ordering: Roman-numeral body parts first (in Act order), then schedules.

    ITAA 1936 body parts are Roman numerals (I, II, III, IV, IVA, ...) and the
    schedules (2D, 2F, 2H) sit AFTER them in the Act. A plain natural sort puts
    '2D' before 'I' because digits sort before letters — wrong.
    """
    m = re.match(r'^([IVXLCDM]+)([A-Z]*)$', part_id)
    if m:
        base = _roman_numeral_value(m.group(1))
        suffix = m.group(2)
        # Lettered sub-parts (IVA, VIIB) sort after their base part.
        if suffix:
            base += sum(_ROMAN_VALUES.get(c, 0) for c in suffix) / 100.0
        return (0, base)
    return (1, 0, _natural_key(part_id))


def build_tree(sections_dir: Path, act: str, compilation_no: int, compilation_date: str, flat: bool = False) -> dict:
    """Walk sections directory and build hierarchical tree."""
    tree: dict = {
        "act": act,
        "compilation_no": compilation_no,
        "compilation_date": compilation_date,
        "parts": [],
    }

    # parts_map: part_id -> part_node
    parts_map: dict[str, dict] = {}
    # divisions_map: (part_id, division_id) -> division_node
    divisions_map: dict[tuple[str, str], dict] = {}

    md_files = sorted(sections_dir.rglob("*.md"))

    for md_file in md_files:
        # Read frontmatter
        content = md_file.read_text(encoding="utf-8")
        fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            continue

        fm: dict[str, str] = {}
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                fm[key.strip()] = val.strip().strip('"')

        part_id = fm.get("part", "")
        part_title = fm.get("part_title", "")
        # Fall back to schedule for schedule-based sections (ITAA 1936 vol05)
        if not part_id:
            part_id = fm.get("schedule", "")
            part_title = fm.get("schedule_title", "")
        division_id = fm.get("division", "")
        division_title = fm.get("division_title", "")
        if division_id.lower() == "unknown":
            division_id = ""
            division_title = ""
        subdivision_id = fm.get("subdivision", "")
        subdivision_title = fm.get("subdivision_title", "")
        section_id = fm.get("section", "")
        section_title = fm.get("section_title", "")

        if not part_id or not section_id:
            continue

        # Ensure part exists
        if part_id not in parts_map:
            part_node = {
                "id": part_id,
                "title": part_title,
                "divisions": [],
                "sections": [],
            }
            parts_map[part_id] = part_node
            tree["parts"].append(part_node)

        part_node = parts_map[part_id]

        # Ensure division exists
        div_key = (part_id, division_id)
        if division_id and div_key not in divisions_map:
            div_node = {
                "id": division_id,
                "title": division_title,
                "subdivisions": [],
                "sections": [],
            }
            divisions_map[div_key] = div_node
            part_node["divisions"].append(div_node)

        # If no division, add section directly to part (rare)
        if not division_id:
            part_node.setdefault("sections", []).append({
                "id": section_id,
                "title": section_title,
                "path": str(md_file.relative_to(sections_dir)),
            })
            continue

        div_node = divisions_map[div_key]

        # Ensure subdivision exists
        if subdivision_id:
            sub_node = None
            for existing in div_node["subdivisions"]:
                if existing["id"] == subdivision_id:
                    sub_node = existing
                    break
            if sub_node is None:
                sub_node = {
                    "id": subdivision_id,
                    "title": subdivision_title,
                    "sections": [],
                }
                div_node["subdivisions"].append(sub_node)
            sub_node["sections"].append({
                "id": section_id,
                "title": section_title,
                "path": str(md_file.relative_to(sections_dir)),
            })
        else:
            div_node["sections"].append({
                "id": section_id,
                "title": section_title,
                "path": str(md_file.relative_to(sections_dir)),
            })

    # Sort everything by id for determinism
    tree["parts"].sort(key=lambda p: _part_sort_key(p["id"]))
    for part in tree["parts"]:
        part["divisions"].sort(key=lambda d: _natural_key(d["id"]))
        part.get("sections", []).sort(key=lambda s: _natural_key(s["id"]))
        for div in part["divisions"]:
            div["subdivisions"].sort(key=lambda s: _natural_key(s["id"]))
            div["sections"].sort(key=lambda s: _natural_key(s["id"]))
            for sub in div["subdivisions"]:
                sub["sections"].sort(key=lambda s: _natural_key(s["id"]))

    if flat:
        flat_parts = []
        for part in tree["parts"]:
            # Signpost node for the Part group
            flat_parts.append({
                "id": part["id"],
                "title": part.get("title", ""),
                "is_signpost": True,
            })
            for div in part["divisions"]:
                flat_parts.append({
                    "id": div["id"],
                    "title": div.get("title", ""),
                    "subdivisions": div.get("subdivisions", []),
                    "sections": div.get("sections", []),
                })
        tree["parts"] = flat_parts

    return tree


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections-dir", type=Path, required=True)
    ap.add_argument("--out-file", type=Path, required=True)
    ap.add_argument("--act", type=str, required=True)
    ap.add_argument("--compilation-no", type=int, required=True)
    ap.add_argument("--compilation-date", type=str, required=True)
    ap.add_argument("--flat-divisions", action="store_true", help="Flatten parts: divisions become top-level nodes")
    args = ap.parse_args()

    tree = build_tree(args.sections_dir, args.act, args.compilation_no, args.compilation_date, flat=args.flat_divisions)
    args.out_file.parent.mkdir(parents=True, exist_ok=True)
    args.out_file.write_text(json.dumps(tree, indent=2), encoding="utf-8")
    print(f"Wrote {args.out_file} with {len(tree['parts'])} parts.")


if __name__ == "__main__":
    main()
