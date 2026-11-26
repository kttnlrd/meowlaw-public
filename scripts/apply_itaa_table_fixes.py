#!/usr/bin/env python3
"""CDN-0171 driver: extract ITAA 1997 tables from the authoritative PDFs and
replace the mangled table regions in the section markdown files.

Usage:
  python3 scripts/apply_itaa_table_fixes.py [--dry-run] [--only SECT,SECT]

Writing the live corpus requires the section to be staged under the
offline-staging convention (codex R4) — see scripts/table_rebuild_staging.py.
`--allow-unstaged` bypasses that and is forbidden during Phases 1-3.
"""

import os
import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "itaa-1997" / "sections"
# S1 (batch 5, 2026-09-26): the repo now vendors the comp-266 volumes itself, so the
# authoritative source is in-repo.  staging/source/itaa-1997 still holds the superseded
# comp-263 set; every candidate is compilation-checked before it is read, so a stale
# volume is skipped rather than silently reverting law text.
PDFDIR = REPO / "source" / "itaa-1997"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import table_rebuild_staging as STAGING  # noqa: E402  (codex R4 gate)

# PyMuPDF lives in python3.12 (/usr/bin/python3); python3 (3.11) lacks fitz.
# The extractor subprocess must use an interpreter that can import fitz.
def _find_python_with_fitz() -> list[str]:
    import shutil
    for cand in ("python3.12", "/usr/bin/python3"):
        path = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if not path:
            continue
        try:
            r = subprocess.run(
                [path, "-c", "import fitz"], capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                return [path]
        except Exception:
            continue
    return ["python3"]  # fall back; extraction will fail loudly if fitz absent

SECTIONS = {
    "30-25": "C2026C00324VOL01.pdf",
    "40-180": "C2026C00324VOL02.pdf",
    "40-190": "C2026C00324VOL02.pdf",
    "40-300": "C2026C00324VOL02.pdf",
    "118-300": "C2026C00324VOL03.pdf",
    "122-25": "C2026C00324VOL04.pdf",
    "109-55": "C2026C00324VOL03.pdf",
    "115-30": "C2026C00324VOL03.pdf",
    "126-15": "C2026C00324VOL04.pdf",
    "130-40": "C2026C00324VOL04.pdf",
    "130-60": "C2026C00324VOL04.pdf",
    "294-80": "C2026C00324VOL06.pdf",
    "376-135": "C2026C00324VOL07.pdf",
    "727-550": "C2026C00324VOL09.pdf",
    "832-615": "C2026C00324VOL09.pdf",
}

EXTRACTOR = REPO / "scripts" / "extract_itaa_tables_pdf.py"

# ── Source-compilation guard ────────────────────────────────────────────────
# The map above names the vendored source volumes in source/itaa-1997/ — compilation **266**,
# register C2026C00324 (S1, batch 5, 2026-09-26). That matches the corpus frontmatter/body,
# which is the point: the two disagreed until the comp-263 set was replaced. Extracting tables
# from an older volume would silently revert law text (266 -> 263) on the largest act, so every
# run still resolves a volume whose own footer compilation matches the corpus, and aborts
# otherwise. COMP266_DIR is the staging fallback (identical volumes, same compilation).
COMP266_DIR = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data", "itaa-1997", "raw", "comp266"))


def _pdf_compilation(pdf: Path) -> str | None:
    import re
    try:
        import fitz
        doc = fitz.open(pdf)
        text = "".join(doc[i].get_text() for i in range(min(40, doc.page_count)))
    except Exception:
        return None
    m = re.search(r"Compilation No\.?\s*(\d+)", text)
    return m.group(1) if m else None


def _corpus_compilation() -> str | None:
    import re
    for f in sorted(DATA.rglob("*.md"))[:50]:
        m = re.search(r'compilation_no:\s*"?(\d+)', f.read_text(errors="ignore")[:400])
        if m:
            return m.group(1)
    return None


def resolve_source_pdf(section: str) -> Path:
    """Source volume for `section` whose compilation matches the corpus; abort if none.

    Prefers the corpus-matching comp-266 volumes; falls through to the legacy map
    only when that volume's own footer agrees with the corpus compilation.
    """
    import re
    wanted = _corpus_compilation()
    mapped = SECTIONS[section]
    candidates: list[Path] = []
    vol = re.search(r"VOL(\d{2})\.pdf$", mapped)
    if vol:
        candidates.append(COMP266_DIR / f"vol{vol.group(1)}.pdf")
    candidates.append(PDFDIR / mapped)
    for pdf in candidates:
        if not pdf.exists():
            continue
        got = _pdf_compilation(pdf)
        if got and wanted and got != wanted:
            print(
                f"  REFUSING {pdf.name}: compilation {got} != corpus {wanted} "
                f"(would revert law text)",
                file=sys.stderr,
            )
            continue
        return pdf
    raise SystemExit(
        f"no source volume for section {section} matching corpus compilation "
        f"{wanted} — checked: {', '.join(str(c) for c in candidates)}"
    )


# Sections whose tables are too corrupted for auto-rebuild (multi-page
# 4-column tables with interleaved headers) — handled manually.
SKIP_AUTO = {"30-25", "118-300", "832-615"}

# Sections where the +1 continuation page leaks the NEXT section's table
# (e.g. 40-305's 'In this case: The amount is:' after 40-300). Map section
# -> required header prefix.
HEADER_FILTER = {
    "40-300": "For this balancing adjustment event:",
}


_DOCS: dict[str, object] = {}


def extract_tables(section: str) -> list[dict]:
    """Extract in-process when fitz is importable, subprocess otherwise (R10).

    One subprocess (and one PDF parse) per section costs seconds each over
    240+ rebuilds; in-process also keeps each volume open across sections.
    """
    pdf = resolve_source_pdf(section)
    try:
        import fitz
        sys.path.insert(0, str(EXTRACTOR.parent))
        import extract_itaa_tables_pdf as ex
        doc = _DOCS.get(str(pdf)) or _DOCS.setdefault(str(pdf), fitz.open(pdf))
        return ex.collect_tables(doc, section)
    except ImportError:
        pass
    py = _find_python_with_fitz()
    r = subprocess.run(
        py + [str(EXTRACTOR), str(pdf), section, "--json"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"  extractor failed for {section}: {r.stderr.strip()}", file=sys.stderr)
        return []
    return json.loads(r.stdout)


def find_section_file(section: str) -> Path | None:
    hits = list(DATA.rglob(f"{section}.md"))
    return hits[0] if hits else None


def md_table(t: dict) -> str:
    """Render a clean markdown table from an extracted table dict."""
    header = ["Item"] + t["header"]
    ncols = len(header)
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * ncols) + " |"]
    for r in t["rows"]:
        cells = [r["item"]]
        for i in range(len(t["header"])):
            cells.append(r["cols"].get(str(i + 1), ""))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def validate_md(txt: str, section: str) -> list[str]:
    """Structural integrity checks. Returns a list of problems (empty = ok).

    Guards the exact class of corruption that CDN-0171's driver introduced:
    table rows jammed into the YAML frontmatter (which silently breaks page
    rendering — keys split, frontmatter never closes). Also catches rows
    whose column count disagrees with the separator (renders broken).
    """
    problems = []
    # 1. frontmatter must parse as clean YAML-ish keys with NO pipe lines
    m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
    if not m:
        problems.append("no YAML frontmatter")
        return problems
    fm = m.group(1)
    for i, ln in enumerate(fm.splitlines(), start=2):
        if ln.strip().startswith("|"):
            problems.append(f"frontmatter line {i} contains table pipe")
    # required keys present
    for k in ("act", "part", "division", "section", "compilation_no"):
        if not re.search(rf"^{re.escape(k)}:", fm, re.M):
            problems.append(f"frontmatter missing key '{k}'")
    # 2. heading present
    if not re.search(rf"^#\s+{re.escape(section)}\b", txt, re.M):
        problems.append(f"section heading '# {section}' missing")
    # 3. every table structurally sound (separator, col counts)
    lines = txt.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("|") and ln.count("|") >= 2:
            # find separator
            j = i
            ncols = None
            while j < len(lines) and not ncols:
                s = lines[j].strip()
                if s.startswith("|") and "---" in s:
                    ncols = len([c for c in s.strip().strip("|").split("|") if c.strip() or True])
                    break
                j += 1
            if ncols is None:
                problems.append(f"line {i+1}: table has no separator row")
                # skip to next non-pipe
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                continue
            # header row (i) must match separator
            hdr_n = len([c for c in lines[i].strip().strip("|").split("|")])
            if hdr_n != ncols:
                problems.append(f"line {i+1}: header cols {hdr_n} != separator {ncols}")
            # rows until non-pipe
            i = j + 1
            while i < len(lines):
                s = lines[i].strip()
                if not (s.startswith("|") and s.count("|") >= 2):
                    break
                if "---" not in s:
                    n = len([c for c in s.strip().strip("|").split("|")])
                    if n != ncols:
                        problems.append(f"line {i+1}: row cols {n} != {ncols}")
                        break
                i += 1
            continue
        i += 1
    return problems


def find_table_start(txt: str, header: list[str]) -> int | None:
    """Line index of the first '| Item' header matching the table family.

    Skips the YAML frontmatter (which can contain '|' characters).
    """
    lines = txt.splitlines(keepends=True)
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    wanted = {w for w in " ".join(header).split() if len(w) > 2}
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if not re.match(r"^\s*\|?\s*Item\b", ln) or "|" not in ln:
            continue
        have = set(ln.split())
        if wanted and wanted.issubset(have):
            return i
        # fallback: any Item table header if no wanted words match
        if not wanted:
            return i
    return None


def find_table_region_end(txt: str, start: int) -> int:
    """Char offset of the end of the table region starting at line 'start'.

    Consumes table lines ('|'), separator lines, blockquotes (mangled row
    fragments), and blank lines between them, stopping at the first prose
    line or a new section element (e.g. '<a id=' or '**' heading).
    """
    lines = txt.splitlines(keepends=True)
    i = start
    # find the end of the LAST consecutive table-ish block
    last_table_end = start
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("|") or re.match(r"^\|?\s*:?-{3,}", ln):
            last_table_end = i
            i += 1
        elif not ln:  # blank — peek ahead: if next non-blank is table-ish
            # or a heading that precedes a table, continue; else stop
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("|") or nxt.startswith(">"):
                    i = j
                elif nxt.startswith("**"):
                    # heading — consume only if a table line follows it
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines) and (lines[k].strip().startswith("|") or lines[k].strip().startswith(">")):
                        last_table_end = j
                        i = k
                    else:
                        break
                else:
                    break
            else:
                break
        elif ln.startswith(">"):
            last_table_end = i
            i += 1
        elif ln.startswith("**"):
            # table intro heading (e.g. '**First element of the cost of a
            # depreciating asset**') — consume it only if followed by a
            # table line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and (lines[j].strip().startswith("|") or lines[j].strip().startswith(">")):
                last_table_end = i
                i = j
            else:
                break
        elif re.match(r"^<a id=", ln):
            break
        elif ln.startswith("#") or ln == "---":
            break
        elif not ln.startswith("|") and not ln.startswith(">"):
            # loose text — could be mangled table cell content (row text
            # with the leading '|' stripped) OR real prose. If the NEXT
            # non-blank line is table-ish, treat this as mangled content;
            # otherwise stop.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and (lines[j].strip().startswith("|") or lines[j].strip().startswith(">") or lines[j].strip().startswith("**")):
                last_table_end = i
                i = j
            else:
                break
        else:
            break
    end = sum(len(x) for x in lines[:last_table_end + 1])
    return end


def apply_fix(section: str, dry_run: bool = False, allow_unstaged: bool = False,
              emit_candidate: Path | None = None) -> bool:
    p = find_section_file(section)
    if p is None:
        print(f"{section}: NO FILE")
        return False
    tables = extract_tables(section)
    if not tables:
        print(f"{section}: NO TABLES from PDF")
        return False
    if section in HEADER_FILTER:
        pref = HEADER_FILTER[section]
        tables = [t for t in tables if t["header"] and t["header"][0].startswith(pref)]

    txt = p.read_text(errors="replace")
    new_txt = txt
    n_replaced = 0
    for t in tables:
        clean = md_table(t)
        # Collapse the whole page-split table region (first header line to
        # the end of the LAST table block) into one clean table.
        start = find_table_start(new_txt, t["header"])
        if start is None:
            continue
        end = find_table_region_end(new_txt, start)
        # convert line index to char offset
        lines_so_far = new_txt.splitlines(keepends=True)
        start_char = sum(len(x) for x in lines_so_far[:start])
        new_txt = new_txt[:start_char] + clean + "\n" + new_txt[end:]
        n_replaced += 1

    if n_replaced == 0:
        print(f"{section}: nothing replaced")
        return False

    if dry_run:
        print(f"{section}: WOULD replace {n_replaced} table(s) in {p.name}")
        return True

    # Safety guard: never write a file whose structure we just broke.
    problems = validate_md(new_txt, section)
    if problems:
        print(f"{section}: REFUSED TO WRITE — validation failed:")
        for prob in problems[:10]:
            print(f"  !! {prob}")
        return False

    # Produce the candidate outside the worktree for the gate/staging step.
    # Nothing is applied on this path (step 1 of the codex-R4 flow).
    if emit_candidate is not None:
        cand = Path(emit_candidate).expanduser().resolve()
        if cand == REPO or REPO in cand.parents:
            print(f"{section}: REFUSED — candidate {cand} is inside the worktree (codex R4)")
            return False
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(new_txt)
        print(f"{section}: candidate written to {cand} — nothing applied")
        return True

    # Codex R4 — offline-staging convention (2026-09-16).  A corpus write is
    # only allowed for a section staged OUTSIDE the worktree, gate-ACCEPTED, and
    # (where the staging report flags risk) explicitly reviewed against the
    # exact output hash.  Nothing below may run before this check passes.
    if not allow_unstaged:
        try:
            staged = STAGING.assert_ready("itaa-1997", section)
        except STAGING.StagingError as exc:
            print(f"{section}: REFUSED TO WRITE — {exc}")
            return False
        staged_text = (Path(staged["dir"]) / "output.md").read_text()
        if staged_text != new_txt:
            print(f"{section}: REFUSED TO WRITE — this run's output differs from the "
                  f"reviewed staging/{section}/output.md; re-run --emit-candidate, re-gate "
                  f"and re-stage before applying")
            return False

    p.write_text(new_txt)
    print(f"{section}: replaced {n_replaced} table(s) in {p.name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="comma-separated sections")
    ap.add_argument("--allow-unstaged", action="store_true",
                    help="bypass the codex-R4 staging gate (forbidden in Phases 1-3)")
    ap.add_argument("--emit-candidate", metavar="DIR",
                    help="write each section's proposed file to DIR/<section>.md and apply nothing")
    args = ap.parse_args()

    sections = [s for s in SECTIONS.keys() if s not in SKIP_AUTO]
    if args.only:
        sections = [s.strip() for s in args.only.split(",") if s.strip()]

    ok = 0
    for sec in sections:
        cand = Path(args.emit_candidate) / f"{sec}.md" if args.emit_candidate else None
        if apply_fix(sec, dry_run=args.dry_run, allow_unstaged=args.allow_unstaged,
                     emit_candidate=cand):
            ok += 1
    print(f"\n{ok}/{len(sections)} sections handled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
