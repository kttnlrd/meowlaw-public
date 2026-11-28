#!/usr/bin/env python3
"""Ingest OECD Model Tax Convention 2017 (full version) into the corpus. v2"""
import json, os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# ponytail: parse rules ship, inputs/outputs never do — paths are repo-relative and
# env-overridable; OPTIONAL_DATA_DIR (default <repo>/data) is where outputs land.
SRC = os.environ.get("OECD_MTC_TXT", "/tmp/oecd_mtc.txt")
OUT = f"{os.environ.get('OPTIONAL_DATA_DIR', ROOT / 'data')}/oecd-mtc-2017"
L = open(SRC, encoding="utf-8").read().split("\n")

ART = {1:1120, 2:1167, 3:1252, 4:1511, 5:1714, 6:2019, 7:2114, 8:2277, 9:2352, 10:2399,
       11:2618, 12:2819, 13:2929, 15:3112, 16:3223, 17:3253, 18:3373, 19:3402, 20:3554,
       21:3587, 22:3648, "23a":3740, "23b":3848, 24:3937, 25:4125, 26:4280, 27:4504,
       28:4644, 29:4687, 30:4875, 31:4976, 32:5049}
ART_ORDER = [1,2,3,4,5,6,7,8,9,10,11,12,13,15,16,17,18,19,20,21,22,"23a","23b",24,25,26,27,28,29,30,31,32]

def first_commentary_starts():
    """First occurrence (per article) of a commentary running header, allowing
    trailing page glue. Also catches the joint 23 A/B and 31/32 headers."""
    starts = {}
    pat23 = re.compile(r"^COMMENTARY ON ARTICLES 23 A AND 23 B")
    pat3132 = re.compile(r"^COMMENTARY ON ARTICLES 31 AND 32")
    pat = re.compile(r"^COMMENTARY ON ARTICLE (\d+)")
    for i, ln in enumerate(L, 1):
        s = ln.replace("\f", "").strip()
        if s.startswith("COMMENTARY ON ARTICLE 14") or s.startswith("COMMENTARY ON ARTICLE 15"):
            pass
        m = pat.match(s)
        if m:
            n = int(m.group(1))
            if n not in starts:
                starts[n] = i
        if pat23.match(s) and "23" not in starts:
            starts["23"] = i
        if pat3132.match(s) and "31-32" not in starts:
            starts["31-32"] = i
    return starts

COM = first_commentary_starts()
COM_END = 99101
COM_ORDER = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,"23",24,25,26,27,28,29,30,"31-32"]

RUNNING = re.compile(r"^(Chapter [IVX]+|ARTICLE [0-9]+( [AB])?|COMMENTARY ON ARTIC?LE)", re.I)

def split_article(block):
    """Return (title, body_lines). Title = uppercase line(s) right after ARTICLE heading."""
    title_parts = []
    i = 0
    for ln in block:
        s = ln.replace("\f", "").strip()
        if not s:
            i += 1
            continue
        if re.match(r"^ARTICLE ", s, re.I):
            i += 1
            continue
        if s == s.upper() and 2 < len(s) < 90 and not re.search(r"\d", s):
            title_parts.append(s)
            i += 1
            continue
        break
    return " ".join(title_parts), block[i:]

def build_body(raw_lines):
    """Group wrapped lines into paragraphs; markers -> bold/blockquote."""
    paras = []
    cur = []
    def flush():
        nonlocal cur
        if cur:
            paras.append(" ".join(cur)); cur = []
    for ln in raw_lines:
        s = ln.replace("\f", "").strip()
        if not s:
            flush(); continue
        if re.match(r"^(Chapter [IVX]+|[A-Z][A-Z ,’'\-&]{3,})$", s) and s == s.upper() and len(s) < 80:
            flush(); continue  # running header
        if re.match(r"^COMMENTARY ON ARTIC?LE", s):
            flush(); continue  # commentary running header
        if re.match(r"^MODEL TAX CONVENTION", s) or re.match(r"^© OECD", s) or re.match(r"^\d+ MTC$", s) or s == "MTC":
            flush(); continue  # page footer
        if re.match(r"^Paragraph \d+$", s):
            flush(); cur.append(f"**{s}**"); continue
        if re.match(r"^\d+\.\s", s) or re.match(r"^\(?[a-z]\)\s", s):
            flush()
        cur.append(s)
    flush()
    out = []
    for p in paras:
        m = re.match(r"^(\d+)\.\s+(.*)$", p)
        if m:
            out.append(f"**{m.group(1)}.** {m.group(2)}")
        elif re.match(r"^\(?[a-z]\)\s", p):
            out.append(f"> {p}")
        else:
            out.append(p)
    return out

def section_file(sid, title, chapter, paras, path):
    fm = {"act": "oecd-mtc-2017", "section": sid, "title": title, "chapter": chapter,
          "compilation_no": 2017, "compilation_date": "2017-11-21"}
    md = "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fm.items()) + "---\n\n" + "\n\n".join(paras) + "\n"
    open(path, "w", encoding="utf-8").write(md)

chap = {1:"I",2:"I",3:"I",4:"I",5:"I",6:"II",7:"II",8:"II",9:"II",10:"III",11:"III",12:"III",
        13:"III",15:"III",16:"III",17:"III",18:"III",19:"III",20:"III",21:"III",22:"III",
        "23a":"IV","23b":"IV",24:"V",25:"V",26:"V",27:"V",28:"V",29:"V",30:"VI",31:"VI",32:"VII"}
os.makedirs(f"{OUT}/sections", exist_ok=True)
for idx, key in enumerate(ART_ORDER):
    start = ART[key]; end = ART[ART_ORDER[idx+1]] if idx+1 < len(ART_ORDER) else 5100
    title, raw = split_article(L[start-1:end-1])
    paras = build_body(raw)
    sid = str(key)
    section_file(sid, title or f"Article {sid}", chap[key], paras, f"{OUT}/sections/{sid}.md")
print("articles:", len(ART_ORDER))

for idx, key in enumerate(COM_ORDER):
    start = COM[key]; end = COM[COM_ORDER[idx+1]] if idx+1 < len(COM_ORDER) else COM_END
    raw = L[start-1:end-1]
    paras = build_body(raw)
    sid = f"c{key}"
    title = f"Commentary on Article {key}" if isinstance(key, int) else ("Commentary on Articles 23 A and 23 B" if key == "23" else "Commentary on Articles 31 and 32")
    section_file(sid, title, "C", paras, f"{OUT}/sections/{sid}.md")
print("commentary:", len(COM_ORDER))
