"""
parse_nz_it.py — NZ Income Tax Act 2007 HTML-to-markdown parser.

Reads the 'whole.html' download from legislation.govt.nz and emits one
markdown file per provision (section) under data/nz-it-2007/sections/,
laid out as part-<P>/division-<SP>/<SECTION>.md so build_tree.py can map
Part -> subpart(division) -> section.

NZ structure:
    div.part      -> <h2 class="part"><span class="label">Part A</span> Title</h2>
    div.subpart   -> <h3 class="subpart"><span class="label">Subpart BA</span>—Title</h3>
    div.prov      -> <h5 class="prov"><span class="label">BA 1</span> Title</h5>
                     followed by div.prov-body (subprov / para / label-para)

Source labels already carry their own delimiters: subsections are "(1)",
paragraphs are "(a)" — so they are emitted verbatim, never re-wrapped.

Usage:
    backend/venv/bin/python pipeline/parse_nz_it.py \
        --html-file source/nz-it-2007/whole.html \
        --out-dir data/nz-it-2007/sections \
        --compilation-no 935 --compilation-date 2026-06-06
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from bs4 import BeautifulSoup, NavigableString
except ImportError as exc:
    raise SystemExit("beautifulsoup4 is required: pip install beautifulsoup4") from exc


BOM = "\ufeff"

# Endnote copies of amending Acts and the Tax Administration Act 1994 amendment text are
# carried in the same document as the consolidated Act. Their div.part nodes reuse part ids
# "1"/"2"/"3", so scanning every div.part in the soup wrote 445 amendment-history "sections"
# into the consolidated Parts A-Z tree (CDN-0173). A part is the consolidated Act only when
# it sits outside these containers.
ENDNOTE_CONTAINER_CLASSES = frozenset({"end", "skeletons", "schedule-amendments", "amend"})


def clean_text(text: str) -> str:
    # U+FEFF (BOM) is not matched by \s, so a BOM between subsection brackets survived
    # ("subsection (1)\ufeff(a)", E-d). Delete it rather than collapsing it to a space: the
    # bracket pair is one citation, not a word break.
    return re.sub(r"\s+", " ", text.replace(BOM, "")).strip()


def inside_endnote(part) -> bool:
    """True when a div.part is nested in an endnote/amendment container (CDN-0173)."""
    for parent in part.parents:
        if getattr(parent, "name", None) != "div":
            continue
        if ENDNOTE_CONTAINER_CLASSES.intersection(parent.get("class") or ()):
            return True
    return False


def label_of(element) -> str:
    span = element.find("span", class_="label")
    return clean_text(span.get_text()) if span else ""


def is_discontinued(element) -> bool:
    return "js-discontinued-info" in element.get("class", [])


def para_text(para) -> str:
    """Text of a div.para, ignoring nested label-paras and history notes."""
    parts = []
    for child in para.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if child.name is None:
            continue
        classes = child.get("class", [])
        if "label-para" in classes or "history" in classes or is_discontinued(child):
            continue
        parts.append(child.get_text())
    return clean_text(" ".join(parts))


def render_label_para(lp, depth: int) -> list[str]:
    """A label-para -> one bullet line plus any nested label-para bullets."""
    if is_discontinued(lp):
        return []
    label = ""
    head = lp.find(["h5", "h6"], class_="label-para")
    if head:
        label = label_of(head)

    lines: list[str] = []
    indent = "  " * depth
    for para in lp.find_all("div", class_="para", recursive=False):
        text = para_text(para)
        prefix = f"{label} " if label else ""
        if text:
            lines.append(f"{indent}- {prefix}{text}")
            prefix = ""  # only the first line of this item carries the label
        for nested in para.find_all("div", class_="label-para", recursive=False):
            lines.extend(render_label_para(nested, depth + 1))
    return lines


def render_subprov(sp) -> list[str]:
    """A subprov (subsection) -> lines of markdown."""
    if is_discontinued(sp):
        return []
    label = ""
    head = sp.find("p", class_="subprov", recursive=False)
    if head:
        label = label_of(head)

    lines: list[str] = []
    prefix = f"**{label}**  " if label else ""
    for para in sp.find_all("div", class_="para", recursive=False):
        text = para_text(para)
        if text:
            lines.append(f"{prefix}{text}")
            prefix = ""
        for lp in para.find_all("div", class_="label-para", recursive=False):
            lines.extend(render_label_para(lp, depth=0))
    if prefix:  # label with no leading text (e.g. a bare "(1)" introducing paras)
        lines.insert(0, prefix.rstrip())
    return lines


def render_prov_body(body) -> str:
    lines: list[str] = []
    for child in body.children:
        if child.name is None:
            continue
        classes = child.get("class", [])
        if is_discontinued(child) or "history" in classes:
            continue
        if child.name == "h6" and "subprov-crosshead" in classes:
            text = clean_text(child.get_text())
            if text:
                lines.append(f"**{text}**")
        elif child.name == "div" and "subprov" in classes:
            lines.extend(render_subprov(child))
    text = "\n\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def defined_terms(prov) -> str:
    tl = prov.find("p", class_="term-list")
    if not tl:
        return ""
    return clean_text(tl.get_text())


# The act name in each file footer. One NZ act was parsed when this script was written
# (the Income Tax Act 2007), so the name was hardcoded; every other NZ act needs its own.
# Set from --act-name in main().
ACT_NAME = "NZ Income Tax Act 2007"


def write_section(out_dir: Path, fm: dict, heading: str, body: str,
                  defined: str, comp_no: int, comp_date: str, rel_dir: str) -> None:
    parts = [
        "---",
        f"part: {fm['part']}",
        f"part_title: {fm['part_title']}",
        f"division: {fm['division']}",
        f"division_title: {fm['division_title']}",
        f"section: {fm['section']}",
        f"section_title: {fm['section_title']}",
        f"compilation_no: {comp_no}",
        f"compilation_date: {comp_date}",
        "---",
        "",
        f"# {heading}",
        "",
        body if body else "*[No operative text]*",
    ]
    if defined:
        parts += ["", f"*{defined}*"]
    parts += ["", "---", f"*{ACT_NAME} — Compilation {comp_no} ({comp_date})*", ""]

    target = out_dir / rel_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{fm['section']}.md").write_text("\n".join(parts), encoding="utf-8")


def parse_prov(prov, part_id, part_title, div_id, div_title,
               out_dir, comp_no, comp_date, stats) -> None:
    head = prov.find("h5", class_="prov", recursive=False)
    if head is None or is_discontinued(head):
        return  # repealed / no heading
    label = label_of(head)
    if not label:
        return
    section_id = re.sub(r"\s+", "-", label)
    title = clean_text(head.get_text().replace(label, "", 1))

    body_el = prov.find("div", class_="prov-body", recursive=False)
    body = render_prov_body(body_el) if body_el else ""

    rel_dir = f"part-{part_id}/division-{div_id}" if div_id else f"part-{part_id}"
    fm = {
        "part": part_id, "part_title": part_title,
        "division": div_id, "division_title": div_title,
        "section": section_id, "section_title": title,
    }
    write_section(out_dir, fm, f"{label}  {title}".strip(), body,
                  defined_terms(prov), comp_no, comp_date, rel_dir)
    stats["sections"] += 1


def parse_nz_income_tax(html_path: Path, out_dir: Path,
                        comp_no: int, comp_date: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    stats = {"parts": 0, "subparts": 0, "sections": 0, "skipped_endnote_parts": 0}

    for part in soup.find_all("div", class_="part"):
        if inside_endnote(part):
            stats["skipped_endnote_parts"] += 1
            continue
        head = part.find("h2", class_="part")
        if not head:
            continue
        plabel = label_of(head)  # "Part A"
        part_id = re.sub(r"^Part\s+", "", plabel).strip() or plabel
        part_title = clean_text(head.get_text().replace(plabel, "", 1))
        if not part_id:
            continue
        stats["parts"] += 1

        seen: set[int] = set()
        for sp in part.find_all("div", class_="subpart"):
            sp_head = sp.find("h3", class_="subpart")
            if not sp_head:
                continue
            sp_label = label_of(sp_head)  # "Subpart BA"
            div_id = re.sub(r"^Subpart\s+", "", sp_label).strip()
            div_title = re.sub(r"^[\s—–-]+", "", sp_head.get_text().replace(sp_label, "", 1))
            div_title = clean_text(div_title)
            stats["subparts"] += 1
            for prov in sp.find_all("div", class_="prov"):
                seen.add(id(prov))
                parse_prov(prov, part_id, part_title, div_id, div_title,
                           out_dir, comp_no, comp_date, stats)

        # Provisions sitting directly under the part (no subpart)
        for prov in part.find_all("div", class_="prov"):
            if id(prov) in seen:
                continue
            parse_prov(prov, part_id, part_title, "", "",
                       out_dir, comp_no, comp_date, stats)

    print(json.dumps(stats, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-file", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--compilation-no", type=int, default=935)
    ap.add_argument("--compilation-date", type=str, default="2026-06-06")
    ap.add_argument("--act-name", type=str, default="NZ Income Tax Act 2007",
                    help="Name written in each section file footer")
    args = ap.parse_args()
    global ACT_NAME
    ACT_NAME = args.act_name
    parse_nz_income_tax(args.html_file, args.out_dir,
                        args.compilation_no, args.compilation_date)


if __name__ == "__main__":
    main()
