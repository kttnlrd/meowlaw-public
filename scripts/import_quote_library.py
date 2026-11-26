"""Import BD-Quote Library.xlsx into the quoting tool."""
import os, re, sys, zipfile
from xml.etree import ElementTree as ET

os.chdir(f"{Path(__file__).resolve().parent.parent}")
sys.path.insert(0, ".")
from backend.routes.quotes import add_quote, _load, _save  # noqa: E402
from pathlib import Path

SRC = os.path.join(os.path.expanduser(os.environ.get("HERMES_DIR", "~/.hermes")), "cache", "documents", "doc_b762046f9eb9_BD-Quote Library.xlsx")
TAG = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

z = zipfile.ZipFile(SRC)
sst = []
root = ET.fromstring(z.read("xl/sharedStrings.xml"))
for si in root.findall("m:si", NS):
    sst.append("".join(t.text or "" for t in si.iter(TAG + "t")))

def sheet_rows(target):
    root = ET.fromstring(z.read(target))
    out = []
    for row in root.findall(".//m:sheetData/m:row", NS):
        cells = {}
        for c in row.findall("m:c", NS):
            ref = c.get("r", "")
            col = re.match(r"([A-Z]+)", ref).group(1)
            t = c.get("t", "")
            v = c.find("m:v", NS)
            isv = c.find("m:is", NS)
            if t == "s" and v is not None:
                val = sst[int(v.text)]
            elif t == "inlineStr" and isv is not None:
                val = "".join(tt.text or "" for tt in isv.iter(TAG + "t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[col] = val
        out.append(cells)
    return out

IMPORT_DATE = "2026-08-28"
counts = {"imported": 0, "skipped_blank": 0, "skipped_no_text": 0, "dupe": 0}
existing = {(q.get("title"), q.get("text")) for q in _load()}

for target in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
    rows = sheet_rows(target)
    for cells in rows[1:]:  # skip header
        title = (cells.get("A") or "").strip()
        text = (cells.get("F") or "").strip()
        alt = (cells.get("G") or "").strip()
        if not title and not text and not alt:
            counts["skipped_blank"] += 1
            continue
        if not text:
            counts["skipped_no_text"] += 1
            print("  no text:", title)
            continue
        if (title, text) in existing:
            counts["dupe"] += 1
            continue
        quote = add_quote(
            title=title,
            date=IMPORT_DATE,
            text=text,
            tag=(cells.get("B") or "").strip() or None,
            cost=(cells.get("C") or "").strip() or None,
            currency=(cells.get("D") or "").strip() or None,
            terms=(cells.get("E") or "").strip() or None,
            alt=alt or None,
            anonymise=False,  # firm's own library: store verbatim, no heuristics
        )
        existing.add((quote["title"], quote["text"]))
        counts["imported"] += 1

print(counts)
print("total quotes now:", len(_load()))
