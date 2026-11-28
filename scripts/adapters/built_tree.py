"""A tree already built for the legislation explorer (tree.json + sections/*.md) -> a collection.

For the CCH guides (master-tax-guide, master-gst-guide, master-tax-examples) and the NZ Master
Tax Guide, whose original exports are no longer kept. Section ids are kept, so existing links
still resolve; front matter is rewritten clean (the old builder wrote titles containing quotes
unescaped, and "None" for a missing paragraph number).

    python scripts/ingest_collection.py data/master-tax-guide --collection master-tax-guide \\
        --adapter built_tree --title "Australian Master Tax Guide" --group Commentary --access restricted

Duplicate ids: the old builder named files by heading title, so two headings with the same
title in different chapters shared one file and the later overwrote the earlier. Only the
entry whose chapter matches the surviving file's front matter is kept; the others are dropped
(their text is lost) and listed in ISSUES, which the ingest checks log as warnings.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

GROUP = "Commentary"
MAX_BYTES = 5 * 1024 * 1024
_FM = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_EXAMPLE_NO = re.compile(r"^(\d+-\d+)\s*[-–—]\s")  # Master Tax Examples: "1-000 - Title"

ISSUES: list[str] = []


def _front(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    m = _FM.match(text)
    if not m:
        return {}, text
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]  # the old builder never escaped inner quotes: take the raw span
        out[k.strip()] = v
    return out, text[m.end():]


def _chapter(part_id: str) -> str:
    return re.sub(r"^(?:ch|topic)-0*", "", part_id.lower()) or part_id


def parts(src: Path) -> list[dict]:
    ISSUES.clear()
    src = Path(src)
    tree = json.loads((src / "tree.json").read_text(encoding="utf-8"))
    base = (src / "sections").resolve()
    seen: dict[str, list[tuple[str, dict]]] = {}
    for p in tree.get("parts", []):
        for s in p.get("sections") or []:
            seen.setdefault(str(s.get("id")), []).append((str(p.get("id")), s))
    out = []
    for p in tree.get("parts", []):
        docs = []
        for s in p.get("sections") or []:
            sid = str(s.get("id") or "")
            f = (base / str(s.get("path") or f"{sid}.md")).resolve()
            if not sid or not f.is_relative_to(base) or not f.is_file() or f.stat().st_size > MAX_BYTES:
                ISSUES.append(f"{sid or s}: no readable section file, skipped")
                continue
            fm, body = _front(f.read_text(encoding="utf-8", errors="replace"))
            if len(seen[sid]) > 1:
                owner = [pid for pid, _ in seen[sid] if _chapter(pid) == _chapter(str(fm.get("part", "")))]
                if owner and owner[0] != str(p.get("id")):
                    ISSUES.append(f"{sid} in {p.get('title') or p.get('id')}: text lost (file holds "
                                  f"{fm.get('paragraph') or owner[0]}); dropped until the source is re-exported")
                    continue
            title = str(s.get("title") or fm.get("title") or sid)
            para = str(fm.get("paragraph") or "").strip()
            if para in ("", "None", "null"):
                m = _EXAMPLE_NO.match(title)
                para = f"¶{m.group(1)}" if m else ""
            body = re.sub(r"\A#\s[^\n]*\n+", "", body.lstrip())  # the title is the page heading already
            docs.append({"id": sid, "title": title, "text": body,
                         **({"citation": para} if para else {})})
        if docs:
            out.append({"title": str(p.get("title") or p.get("id")), "docs": docs})
    return out
