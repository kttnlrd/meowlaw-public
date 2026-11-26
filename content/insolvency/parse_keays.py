#!/usr/bin/env python3
"""Extract Keays Insolvency textbook into chapter markdown files."""

import re
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The shared normaliser lives in <repo>/pipeline; works run as a script or imported.
sys.path.insert(0, str(ROOT / "pipeline"))
from text_normalize import nfkc_ligatures  # noqa: E402

# ponytail: parse rules ship, inputs/outputs never do — paths are repo-relative and
# env-overridable; OPTIONAL_DATA_DIR (default <repo>/data) is where outputs land.
_DATA = Path(os.environ.get("OPTIONAL_DATA_DIR", ROOT / "data"))
RAW = os.environ.get("KEAYS_RAW", str(_DATA / "keays-raw.txt"))
OUT = str(_DATA / "insolvency-keays" / "chapters")

CHAPTERS = [
    (1, "Introduction to Insolvency – the Law, Policy and Current Issues"),
    (2, "Introduction to Bankruptcy and its Administration"),
    (3, "Going Bankrupt – Voluntary and Compulsory Bankruptcy"),
    (4, "Impact of Bankruptcy"),
    (5, "Recovery of Assets for Creditors"),
    (6, "Administration of the Bankruptcy"),
    (7, "End of a Bankruptcy and Beyond"),
    (8, "Personal Insolvency Agreements"),
    (9, "Debt Agreements"),
    (10, "Introduction to Liquidation and its Administration"),
    (11, "Voluntary and Compulsory Winding Up"),
    (12, "Provisional Liquidation"),
    (13, "Effects of Winding Up"),
    (14, "Assets Available to the Liquidator"),
    (15, "Administration of the Winding Up"),
    (16, "Criminal Offences and Civil Actions Against Company Directors"),
    (17, "Termination of the Winding Up: Deregistration and Reinstatement"),
    (18, "Receivership"),
    (19, "Voluntary Administration"),
    (20, "Deeds of Company Arrangement"),
    (21, "Restructuring and Workouts"),
]

# Chapter start patterns - line number of the \f\f or section marker
# These were found by analysing the raw text for \f\f markers
# Each tuple: (chapter_number, start_line_inclusive, end_line_exclusive)
BOUNDARIES = [
    (1, 5549, 7482),
    (2, 7482, 9855),
    (3, 9855, 12162),
    (4, 12162, 13828),
    (5, 13828, 15373),
    (6, 15373, 18629),
    (7, 18629, 19871),
    (8, 19871, 21490),
    (9, 21490, 22478),
    (10, 22478, 25255),
    (11, 25255, 27359),
    (12, 27359, 27888),
    (13, 27888, 28697),
    (14, 28697, 30935),
    (15, 30935, 33912),
    (16, 33912, 35404),
    (17, 35404, 36203),
    (18, 36203, 39655),
    (19, 39655, 43581),
    (20, 43581, 45689),
    (21, 45689, None),  # until end
]

def clean_text(text: str) -> str:
    """Clean extracted text for markdown output.

    E-c: the PDF's typographic ligatures (U+FB00-U+FB06) are normalised here, in the one
    function every chapter body passes through (:116), so "ﬁnancial" is indexed as
    "financial" - the insolvency FTS reads this text raw, and "financial" searched 9 of the
    21 chapters while "ﬁnancial" searched 19.  The mapping touches the ligature block only:
    full NFKC would also rewrite superscripts and fractions elsewhere in this corpus.
    """
    # Remove \x03 (ETX) characters
    text = text.replace("\x03", "")
    # Normalise the ligature block (E-c) - everything else is copied byte for byte
    text = nfkc_ligatures(text)
    # Replace form feeds with clean page breaks
    text = text.replace("\f", "")
    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace per line
    lines = [l.rstrip() for l in text.split("\n")]
    text = "\n".join(lines)
    # Remove very short standalone page numbers (running headers/footers)
    text = re.sub(r"\n\d+\n(?=\[|\n|Table|Part|Chapter)", "\n", text)
    return text.strip()

def strip_running_headers(text: str) -> str:
    """Remove running headers like '4       Keay's Insolvency: Personal and Corporate Law and Practice     [1.05]'"""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        # Remove running header lines that match the Keay's pattern
        if re.search(r"Keay'?s?\s+Insolvency", line) and re.search(r"\[\d+\.\d{2}\]", line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)

def slugify(title: str) -> str:
    """Create a filesystem-safe slug from a chapter title."""
    s = title.lower()
    s = re.sub(r"[–—/,:]+", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def main():
    with open(RAW, "r") as f:
        lines = f.readlines()

    os.makedirs(OUT, exist_ok=True)
    os.makedirs(str(Path(OUT).parent / "raw"), exist_ok=True)

    ch_tree = []

    for ch_num, start_line, end_line in BOUNDARIES:
        title = CHAPTERS[ch_num - 1][1]
        slug = slugify(title)

        # Extract lines (1-indexed to 0-indexed)
        start_idx = start_line - 1
        end_idx = end_line - 1 if end_line else len(lines)
        chapter_text = "".join(lines[start_idx:end_idx])

        # Clean
        chapter_text = strip_running_headers(chapter_text)
        chapter_text = clean_text(chapter_text)

        # Write chapter markdown
        md = f"# Chapter {ch_num}: {title}\n\n{chapter_text}\n"
        out_path = os.path.join(OUT, f"{ch_num:02d}-{slug}.md")
        with open(out_path, "w") as f:
            f.write(md)

        # Also save raw extract (for debugging / FTS5 index setup)
        raw_path = os.path.join(OUT, "..", "raw", f"{ch_num:02d}-{slug}.txt")
        with open(raw_path, "w") as f:
            f.write("".join(lines[start_idx:end_idx]))

        ch_tree.append({
            "chapter": ch_num,
            "title": title,
            "slug": f"{ch_num:02d}-{slug}",
            "file": f"chapters/{ch_num:02d}-{slug}.md",
            "lines": end_idx - start_idx,
        })
        print(f"  Ch {ch_num:2d}: {title[:50]:50s} -> {out_path} ({end_idx - start_idx:6d} lines)")

    # Write chapter tree
    import json
    tree_path = os.path.join(OUT, "..", "ch-tree.json")
    with open(tree_path, "w") as f:
        json.dump({"chapters": ch_tree, "total": len(ch_tree)}, f, indent=2)
    print(f"\nWrote chapter tree: {tree_path}")
    print(f"Total: {len(ch_tree)} chapters")

if __name__ == "__main__":
    main()