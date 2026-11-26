#!/usr/bin/env python3.12
"""Guard-of-the-guard for rebuild.sh's source-compilation pinning (S1 + S2, batch 5).

WHY THIS EXISTS
---------------
rebuild.sh stamps a compilation number into every section's frontmatter and into each
act's tree.json. The only thing tying that claim to the PDFs actually read is the 1b
guard. Three drift paths were live on 2026-09-26:

  1. the guard's expectation disagreed with the vendored source (itaa-1997 expected 266
     but source/itaa-1997 held the comp-263 set; itaa-1936 expected 192 and taa-1953
     expected 225 while the sources were 191 and 222),
  2. a missing source/<act>/ was a silent skip (gst-1999 had no directory, so its guard
     never ran at all),
  3. guard 1b ran *after* stage 1, which writes data/*/raw, so the guard could not be
     exercised without a write.

This test is the detector for all three. It compares three independent readings of the
same fact — what rebuild.sh claims (guard + stamps), what the source volume says about
itself (its own "Compilation No." footer, never the filename), and what the corpus
frontmatter records — and fails when they disagree. It also runs the guard for real,
via the write-free mode `./rebuild.sh --check-source`.

A detector that cannot fail is decoration: run it before the fix and it must fail.
"""
from __future__ import annotations

import collections
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REBUILD = ROOT / "rebuild.sh"
CHECK_SOURCE = ROOT / "scripts" / "check_source_compilation.py"
GUARD_OF_GUARD = ROOT / "scripts" / "check_guard_vs_corpus.py"
CORPUS = ROOT / "data"

GUARD_MARKER = "=== 1b. Source compilation guard ==="
STAGE1_MARKER = "=== 1. PDF extraction ==="
CHECK_SOURCE_FLAG = "--check-source"
# Option (b) — "change the stamp to what the source actually is" — must be ruled out for
# itaa-1997 in a comment, because acting on it would replace the comp-266 corpus with 263.
OPTION_B_MARKER = "REJECTED for itaa-1997"

GUARDED = {"itaa-1997", "itaa-1936", "gst-1999", "taa-1953"}
# Register ids pinned from the PDF's own "Authorised Version C… registered" footer for
# acts where the vendored set changed (S1). Not a filename check.
EXPECTED_REGISTER = {"itaa-1997": "C2026C00400"}  # bump when the source compilation is updated

COMP_RE = re.compile(r"Compilation No\.?\s*(\d+)", re.I)
AUTH_RE = re.compile(r"Authorised Version\s+(C\d{4}C\d+)", re.I)

failures: list[str] = []
notes: list[str] = []


def check(ok: bool, msg: str) -> bool:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)
    return ok


def read_rebuild() -> list[str]:
    return REBUILD.read_text(encoding="utf-8").splitlines()


# ── reading 1: what rebuild.sh claims ────────────────────────────────────────
def guard_expectations(lines: list[str]) -> dict[str, int]:
    """The `for spec in "<act> <n>" ...` list of guard 1b."""
    out: dict[str, int] = {}
    for ln in lines:
        if "for spec in" not in ln:
            continue
        for act, num in re.findall(r'"([a-z0-9-]+)\s+(\d+)"', ln):
            out[act] = int(num)
    return out


def stamp_expectations(lines: list[str]) -> dict[str, set[tuple[int, str | None]]]:
    """Every --compilation-no the script passes, keyed by the act its command reads.

    The act is read off the command's own path flag (--raw-dir/--out-dir/--sections-dir
    /--out-file/--pdf-dir), never off a comment or a nearby unrelated command.
    """
    out: dict[str, set[tuple[int, str | None]]] = {}
    path_flag = re.compile(
        r"--(?:raw-dir|out-dir|sections-dir|out-file|pdf-dir|tree-file)\s+"
        r"\"?\$(?:DATA|SOURCE)/([a-z0-9-]+)")
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#"):
            continue
        m = re.search(r"--compilation-no\s+(\d+)", ln)
        if not m:
            continue
        act = None
        for j in range(i, max(-1, i - 5), -1):        # the command's own act path
            m2 = path_flag.search(lines[j])
            if m2:
                act = m2.group(1)
                break
        date = None
        for j in range(i, min(len(lines), i + 4)):
            if lines[j].lstrip().startswith("#"):
                continue
            m3 = re.search(r"--compilation-date\s+([0-9]{4}-[0-9]{2}-[0-9]{2})", lines[j])
            if m3:
                date = m3.group(1)
                break
        if act in GUARDED:
            out.setdefault(act, set()).add((int(m.group(1)), date))
    return out


# ── reading 2: what the source PDFs say about themselves ─────────────────────
def pdf_front_matter(pdf: pathlib.Path, pages: int = 3) -> str:
    try:
        import fitz
    except ImportError:
        return ""
    try:
        doc = fitz.open(pdf)
    except Exception:
        return ""
    try:
        return "\n".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
    finally:
        doc.close()


def pdf_compilations(pdf: pathlib.Path) -> set[int]:
    return {int(n) for n in COMP_RE.findall(pdf_front_matter(pdf))}


def pdf_registers(pdf: pathlib.Path) -> set[str]:
    return set(AUTH_RE.findall(pdf_front_matter(pdf)))


# ── reading 3: what the corpus frontmatter records ───────────────────────────
def frontmatter(path: pathlib.Path) -> dict[str, str]:
    head = path.open(encoding="utf-8", errors="replace").read(4000)
    m = re.match(r"^---\n(.*?)\n---\s*\n", head, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def corpus_facts(act: str) -> tuple[collections.Counter, collections.Counter]:
    comps: collections.Counter = collections.Counter()
    dates: collections.Counter = collections.Counter()
    for f in (CORPUS / act / "sections").rglob("*.md"):
        fm = frontmatter(f)
        comps[fm.get("compilation_no")] += 1
        dates[(fm.get("compilation_date") or "")[:10]] += 1
    return comps, dates


def main() -> int:
    lines = read_rebuild()
    text = "\n".join(lines)

    print("== wiring: the guard runs first, and can be run without writing ==")
    guard_at = next((i for i, l in enumerate(lines) if GUARD_MARKER in l), None)
    stage1_at = next((i for i, l in enumerate(lines) if STAGE1_MARKER in l), None)
    check(guard_at is not None, "guard 1b block is present in rebuild.sh")
    check(stage1_at is not None, "stage 1 block is present in rebuild.sh")
    if guard_at is not None and stage1_at is not None:
        check(guard_at < stage1_at,
              f"guard 1b (line {guard_at + 1}) runs BEFORE stage 1 (line {stage1_at + 1}), "
              "so it aborts before any write to data/*/raw")
    check(CHECK_SOURCE_FLAG in text,
          f"rebuild.sh supports {CHECK_SOURCE_FLAG} (validation + guard, then exit before stage 1)")
    check(OPTION_B_MARKER in text,
          f"option (b) is explicitly ruled out for itaa-1997 (marker {OPTION_B_MARKER!r})")
    check("check_guard_vs_corpus.py" in text,
          "rebuild.sh calls scripts/check_guard_vs_corpus.py (guard of the guard)")
    check(GUARD_OF_GUARD.is_file(), f"{GUARD_OF_GUARD.relative_to(ROOT)} exists")

    # The missing-source-directory skip: a guarded act with no source/<act>/ must fail.
    if guard_at is not None:
        block = "\n".join(lines[guard_at:guard_at + 40])
        check(re.search(r"!\s*-d\s+\"\$act_dir\"", block) is not None,
              "guard 1b has an explicit `[ ! -d \"$act_dir\" ]` failure for a guarded act "
              "(no silent skip)")

    print("\n== the guard list parsed from rebuild.sh ==")
    expected = guard_expectations(lines)
    check(set(expected) == GUARDED,
          f"guard covers exactly {sorted(GUARDED)} (got {sorted(expected)})")
    for act in sorted(expected):
        print(f"    {act} -> {expected[act]}")

    print("\n== --check-source: the guard itself, run for real, no writes ==")
    if CHECK_SOURCE_FLAG in text and "--check-source" in (REBUILD.read_text(encoding="utf-8")):
        r = subprocess.run(["bash", str(REBUILD), CHECK_SOURCE_FLAG],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=600)
        tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-6:]
        check(r.returncode == 0,
              f"`rebuild.sh {CHECK_SOURCE_FLAG}` exits 0 (got {r.returncode}); tail:\n      "
              + "\n      ".join(tail))
        if r.returncode != 0 and r.stderr.strip():
            notes.append("check-source stderr: " + r.stderr.strip().splitlines()[-1])
    else:
        failures.append(f"NOT RUN: rebuild.sh does not implement {CHECK_SOURCE_FLAG}, and running "
                        "the script without it would write data/*/raw — refusing to execute it")
        print("  FAIL " + failures[-1])

    stamps = stamp_expectations(lines)

    for act in sorted(expected):
        want = expected[act]
        print(f"\n== {act}: expected {want} ==")
        src = ROOT / "source" / act
        if not check(src.is_dir(), f"source/{act}/ exists (S2: a missing dir must fail the guard, "
                                  "not skip it)"):
            continue
        pdfs = sorted(src.glob("*.pdf"))
        if not check(bool(pdfs), f"source/{act}/ holds at least one PDF"):
            continue

        # reading 2: the volume's own footer
        seen: dict[int, list[str]] = {}
        for p in pdfs:
            for n in pdf_compilations(p):
                seen.setdefault(n, []).append(p.name)
        check(sorted(seen) == [want],
              f"every source volume's internal 'Compilation No.' is {want} "
              f"(got {sorted(seen)}{' e.g. ' + str(seen[min(seen)][:2]) if seen and sorted(seen) != [want] else ''})")

        reg = EXPECTED_REGISTER.get(act)
        if reg:
            regs: set[str] = set()
            for p in pdfs:
                regs |= pdf_registers(p)
            check(regs and regs == {reg},
                  f"source volumes carry authorised version {reg} (got {sorted(regs)})")

        # reading 3: the corpus frontmatter
        comps, dates = corpus_facts(act)
        check(comps and set(comps) == {str(want)},
              f"corpus frontmatter compilation_no is {want} for every section "
              f"(got {dict(comps)})")
        check(len(dates) == 1,
              f"corpus frontmatter compilation_date is uniform (got {dict(dates)})")
        corpus_date = next(iter(dates)) if len(dates) == 1 else None

        # guard expectation vs the stamps rebuild.sh passes downstream
        acts_stamps = stamps.get(act)
        if check(acts_stamps is not None,
                 f"rebuild.sh passes --compilation-no for {act}"):
            check({c for c, _ in acts_stamps} == {want},
                  f"every --compilation-no stamp in rebuild.sh is {want} (got {sorted(acts_stamps)})")
            stamp_dates = {d for _, d in acts_stamps}
            check(stamp_dates == {corpus_date},
                  f"every --compilation-date stamp in rebuild.sh is {corpus_date} "
                  f"(got {sorted(str(d) for d in stamp_dates)}) — the guard must not downgrade "
                  "the corpus")

    print("\n== guard of the guard: older source than corpus must abort ==")
    if GUARD_OF_GUARD.is_file():
        # Derive the corpus's own compilation number so this check does not rot the
        # next time the corpus is bumped to a new compilation.
        want_itaa = None
        try:
            import json as _json
            _tree = _json.loads((ROOT / "data" / "itaa-1997" / "tree.json").read_text(encoding="utf-8"))
            want_itaa = str(_tree.get("compilation_no"))
        except Exception:
            pass
        if not (want_itaa or "").isdigit():
            print("  skip  could not read data/itaa-1997/tree.json compilation_no")
        want_itaa = want_itaa if (want_itaa or "").isdigit() else "266"
        older = str(int(want_itaa) - 3)
        for label, args, want_rc in (
            (f"corpus {want_itaa} vs expected {want_itaa} -> ok",
             (["--act", "itaa-1997", "--expected", want_itaa], 0), 0),
            (f"corpus {want_itaa} vs expected {older} -> abort",
             (["--act", "itaa-1997", "--expected", older], 1), 1),
        ):
            argv, rc = args
            r = subprocess.run([sys.executable, str(GUARD_OF_GUARD), *argv, "--quiet"],
                               capture_output=True, text=True, cwd=str(ROOT), timeout=300)
            check(r.returncode == want_rc,
                  f"{label} (exit {r.returncode}, wanted {want_rc})")
    else:
        check(False, "scripts/check_guard_vs_corpus.py exists")

    print("\n== summary ==")
    for n in notes:
        print("  note: " + n)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("PASSED: guard, sources, corpus frontmatter and stamps all agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
