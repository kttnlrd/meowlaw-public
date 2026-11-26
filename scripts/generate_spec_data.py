#!/usr/bin/env python3
"""
Generate data/spec/tree.json and data/spec/sections/*.md from docs/DISPLAY_SPEC.md.

Idempotent — safe to re-run after doc edits.
Reads the spec doc, splits on `## N.` headings (top-level only, NOT `###`),
and writes out tree.json + one .md file per section.
"""

import json, os, re, sys
from pathlib import Path

DOC = Path(f"{Path(__file__).resolve().parent.parent}/docs/DISPLAY_SPEC.md")
DATA = Path(f"{Path(__file__).resolve().parent.parent}/data/spec")
SECTIONS = DATA / "sections"

# ── helpers ────────────────────────────────────────────────────────────────

def slugify(title: str) -> str:
    """Turn a heading title into a lowercase-hyphen slug.
    
    - Strip parenthetical suffixes (e.g. "(apply to all types)")
    - Replace "/" with "-"
    - Collapse multiple hyphens
    """
    slug = title.strip()
    # Remove parenthetical suffixes
    slug = re.sub(r"\s*\(.*?\)\s*", "", slug)
    # Remove leading/trailing dashes that might remain
    slug = slug.strip()
    slug = slug.lower().replace(" ", "-").replace("/", "-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


def extract_sections(text: str):
    """
    Split the markdown on `## N.` headings.

    Returns list of (number, title, body_lines).
    """
    lines = text.splitlines()
    sections: list[tuple[str, str, list[str]]] = []
    current_num: str | None = None
    current_title: str | None = None
    current_body: list[str] = []
    header_re = re.compile(r"^## (\d+)\.\s+(.+)$")

    # Track whether we're inside a fenced code block to avoid false matches
    in_code = False

    for line in lines:
        # Track code fences
        if line.strip().startswith("```"):
            in_code = not in_code

        m = header_re.match(line)
        if m and not in_code:
            if current_num is not None:
                sections.append((current_num, current_title, current_body))
            current_num = m.group(1)
            current_title = m.group(2).strip()
            current_body = []
        else:
            if current_num is not None:
                current_body.append(line)

    if current_num is not None:
        sections.append((current_num, current_title, current_body))

    return sections


# ── main ───────────────────────────────────────────────────────────────────

def main():
    text = DOC.read_text(encoding="utf-8")
    sections = extract_sections(text)

    # ── build tree.json ──────────────────────────────────────────────────
    parts = []
    for num, title, _body in sections:
        slug = slugify(title)
        parts.append({
            "id": num,
            "title": title,
            "divisions": [],
            "sections": [
                {"id": num, "title": title, "path": f"{slug}.md"}
            ],
        })

    tree = {"act": "Display Spec", "parts": parts}

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "tree.json").write_text(json.dumps(tree, indent=2) + "\n", encoding="utf-8")
    print(f"✓ wrote data/spec/tree.json  ({len(parts)} parts)")

    # ── write section .md files ──────────────────────────────────────────
    SECTIONS.mkdir(parents=True, exist_ok=True)

    for num, title, body in sections:
        slug = slugify(title)
        # Build frontmatter
        frontmatter = (
            "---\n"
            f'act: spec\n'
            f'section: "{num}"\n'
            f'title: "{title.replace(chr(34), chr(39))}"\n'
            f'part: "{num}"\n'
            f'division: ""\n'
            "---\n"
        )
        # Body — join lines, no trailing newline mangling
        body_text = "\n".join(body)
        if body_text and not body_text.endswith("\n"):
            body_text += "\n"

        content = frontmatter + body_text

        out_path = SECTIONS / f"{slug}.md"
        out_path.write_text(content, encoding="utf-8")
        print(f"  wrote data/spec/sections/{slug}.md")

    print("\nDone. Run the verification steps to confirm.")


if __name__ == "__main__":
    main()
