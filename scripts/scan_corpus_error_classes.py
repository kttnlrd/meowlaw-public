#!/usr/bin/env python3
"""Corpus-wide scan against previously-identified error classes (bug register).

Each check maps to a CDN ticket class and scans the FULL corpus, not just the
reported instance. Emits machine-readable findings: class, act/path, detail.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

DATA = Path(os.path.join(os.environ["LEGISLATION_EXPLORER_DIR"], "data"))
findings: list[dict] = []

# ── C31 baseline ─────────────────────────────────────────────────────────────
# C31 is "publisher repeal gap OR items lost, verify against the source". Almost all of it is the
# former: only 534 of 1,422 findings sit in acts whose source PDF is held locally (2026-11-02), so
# proving the rest is a source-acquisition project, and repairing them would invent law. A billion
# advisory rows also make the nightly count unreadable. So the accepted set is frozen here and only
# a NEW gap - or a gap in a file that never had one - is reported from now on. Regenerate with
# `--write-c31-baseline`, and only after checking that the growth is not a real loss.
C31_BASELINE = Path(__file__).with_name("c31_baseline.json")
_c31_baseline: set[str] | None = None
_c31_ignore_baseline = False


def _load_c31_baseline() -> set[str]:
    global _c31_baseline
    if _c31_baseline is None:
        try:
            _c31_baseline = set(json.loads(C31_BASELINE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _c31_baseline = set()
    return _c31_baseline


def _c31_key(path: str, detail: str) -> str | None:
    m = re.search(r"line (\d+)", detail or "")
    return f"{path}:{m.group(1)}" if m else None


def add(cls: str, path: str, detail: str, count: int = 1):
    """Record one finding. `count` is the number of occurrences it stands for (character
    classes aggregate, so C22_ligature is 1,300 findings for 22,212 characters); it is only
    written when it differs from 1, so the shape of every existing finding is unchanged."""
    if cls == "C31_item_order_advisory" and not _c31_ignore_baseline:
        key = _c31_key(path, detail)
        if key and key in _load_c31_baseline():
            return
    f: dict = {"class": cls, "path": path, "detail": detail}
    if count != 1:
        f["count"] = count
    findings.append(f)


# ── C1 (CDN-0053/0070): compilation_no type consistency ─────────────────────
def scan_compilation_no(root=None):
    base = Path(root) if root else DATA
    for act in sorted(p.name for p in base.iterdir() if (p / "tree.json").exists()):
        try:
            t = json.loads((base / act / "tree.json").read_text())
        except Exception as e:
            add("C1_compilation_no", f"data/{act}/tree.json", f"unreadable: {e}")
            continue
        cn = t.get("compilation_no")
        if isinstance(cn, str):
            add("C1_compilation_no", f"data/{act}/tree.json",
                f"compilation_no is str ({cn!r}), others are int")
        if not t.get("compilation_date") and not t.get("compilation_note"):
            # An explicit note saying the source states no version date is a correct record of a
            # silent source; demanding a date there would only push the corpus to invent one.
            add("C1_compilation_no", f"data/{act}/tree.json", "missing compilation_date")


# ── C2 (CDN-0069): empty parts/divisions/leaves in trees ────────────────────
def scan_empty_tree_nodes(root=None):
    base = Path(root) if root else DATA
    for act in sorted(p.name for p in base.iterdir() if (p / "tree.json").exists()):
        try:
            t = json.loads((base / act / "tree.json").read_text())
        except Exception:
            continue
        for pi, p in enumerate(t.get("parts", [])):
            if not p.get("sections") and not p.get("divisions"):
                add("C2_empty_node", f"data/{act}/tree.json", f"empty part: {p.get('id')}")
            for di, d in enumerate(p.get("divisions", [])):
                if not d.get("sections") and not d.get("subdivisions"):
                    add("C2_empty_node", f"data/{act}/tree.json",
                        f"empty division: {p.get('id')}/{d.get('id')}")
                for si, s in enumerate(d.get("subdivisions", [])):
                    if not s.get("sections"):
                        add("C2_empty_node", f"data/{act}/tree.json",
                            f"empty subdivision: {p.get('id')}/{d.get('id')}/{s.get('id')}")


# ── C3 (CDN-0054): title truncation heuristics ──────────────────────────────
# A truncated title typically: ends mid-word with a hyphen, or ends '…'/'..',
# or ends with a standalone trailing word that indicates a cut (and/the/of/to
# as the LAST word, e.g. 'Rules applying to particular gifts of'). Words that
# merely CONTAIN those letters as suffixes ('demand', 'land') are NOT cuts.
def scan_tree_titles():
    suspicious_end = re.compile(r"[\w]-\s*$|\.\.\s*$|…\s*$|\b(?:and|the|of|to)\s*$")
    for act in sorted(p.name for p in DATA.iterdir() if (p / "tree.json").exists()):
        try:
            t = json.loads((DATA / act / "tree.json").read_text())
        except Exception:
            continue
        stack = []
        for p in t.get("parts", []):
            stack.append(p)
            for d in p.get("divisions", []):
                stack.append(d)
                for s in d.get("subdivisions", []):
                    stack.append(s)
        for node in stack:
            title = node.get("title", "")
            if not title:
                continue
            if suspicious_end.search(title.strip()) and len(title.strip()) > 3:
                add("C3_title_truncation", f"data/{act}/tree.json",
                    f"node {node.get('id')}: title ends suspiciously: {title!r}")


# ── C4 (CDN-0006/0049): asterisk footnote lines in section bodies ───────────
ASTERISK_LINE = re.compile(r"^\s*\*{2,}\s*$|^\s*\*[^*].*footnote|^\s*\*+ *[A-Za-z]+\s*\*+", re.M)
def scan_body_fragments():
    """Body text split into one-line fragments (treaties & any markdown).
    Fragment lines: bare (N), (a), a quote char, or a lone dash/em-dash.
    A file with more than 5 such lines is corrupted display-level text."""
    frag_re = re.compile(r"^\((?:\d+|[a-z])\)$|^\"$|^[-—]$")
    for base in [DATA / "treaties", DATA]:
        if base == DATA:
            dirs = [d for d in DATA.iterdir() if (d / "sections").is_dir()]
        else:
            dirs = [base]
        for d in dirs:
            for p in sorted((d / "sections").rglob("*.md")) if base == DATA else sorted(d.rglob("*.md")):
                if p.name.startswith("."):
                    continue
                try:
                    body = open(p, encoding="utf-8").read().split("---", 2)[-1]
                except Exception:
                    continue
                frags = [ln.strip() for ln in body.splitlines() if frag_re.match(ln.strip())]
                kinds = set()
                for f in frags:
                    if f == '"':
                        kinds.add("quote")
                    elif re.match(r"^[-—]$", f):
                        kinds.add("dash")
                    else:
                        kinds.add("mark")
                # Mono-pattern fragments are legitimate table cells (grid tables
                # use bare dashes; ATO guides use bare table-category codes).
                # Real fragmentation mixes quote/dash/marker kinds per file.
                if len(frags) > 5 and len(kinds) >= 2:
                    findings.append({"class": "C5_body_fragments", "path": str(p),
                                     "detail": f"{len(frags)} fragment lines ({', '.join(sorted(kinds))}) e.g. {frags[0]!r} {frags[1]!r}"})


# ── C4 (CDN-0006/0049): asterisk footnote lines in section bodies ───────────
# Two shapes are flagged, both of which only a broken extraction produces:
#   (a) a line that is nothing but asterisks (2 or more) — genuine noise in every case;
#   (b) a '*' line mentioning a footnote marker — a real PDF artifact shape that has no
#       hits in the corpus today; kept as a live check, not decoration.
#
# RETIRED 2026-09-21 (CDN-0199): the third alternative, `\*+ *[A-Za-z]+\s*\*+`
# (an emphasis-wrapped word). Measured over the live corpus: 18 findings, 18 false
# positives. Every one was legitimate content — the Act's own emphasis subheadings
# rendered as markdown emphasis on a line of their own (`**Defence**`, `*Exception*`,
# `*Partner*`/`*Beneficiary*`/`*Trustee*`, `**Object**`, `*Application*`/`**List**`/
# `*Objects*`), or defined-term asterisk markers INSIDE ```ingest-unclassified``` /
# ```ingest-formula``` fences, where the text is literal and never markdown emphasis.
# The shape it was written for — a stranded bold fragment left behind when a table cell
# was split (`**and**` / `**expenditure**` in itaa-1997, fixed by hand on 2026-08-30 in
# commit 620032e8b) — is STRUCTURALLY IDENTICAL to those subheadings and fence markers,
# so no pattern can tell them apart. Stranded-marker noise stays covered by
# C8_stray_bullet (`^[ \t]*[-*][ \t]*$` in ARTIFACT_PATS) and by (a) above.
#
# `^[ \t]*` rather than `^\s*` (both anchors): in re.M, `\s` matches the newline itself,
# so a match could BEGIN on the preceding blank line and every reported line number came
# out one too low (51-5.md reported "line 16" for text on line 17). These findings are
# acted on by hand, so the number has to be the asterisk line's own.
ASTERISK_LINE = re.compile(r"^[ \t]*\*{2,}\s*$|^[ \t]*\*[^*].*footnote", re.M)


def scan_asterisk_noise(root=None):
    """C4 asterisk noise in itaa-1997 section bodies.

    `root` defaults to the live corpus (DATA), mirroring scan_missing_heading; a scratch
    root is used by the pinned self-test. Finding paths are relative to whichever base was
    scanned, so the default run still reports 'itaa-1997/sections/...' as it always has.
    """
    base = Path(root) if root else DATA
    for p in sorted((base / "itaa-1997" / "sections").rglob("*.md")):
        text = p.read_text(errors="replace")
        for m in ASTERISK_LINE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            add("C4_asterisk_noise", str(p.relative_to(base)), f"line {line}: {m.group(0).strip()[:60]}")


# ── C5 (CDN-0045/0167): sentence fragments / mid-sentence cut at end of section ─
# Fragment signal: section body ends with a lowercase word right after content,
# or a lone short word on the final line (no period), i.e. truncated at cut point.
STRAY_TAIL = re.compile(r"^\s*[a-z]{2,10}\s*$")
def scan_section_fragments(root=None):
    base = Path(root) if root else DATA
    for act_dir in base.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue
            last = lines[-1]
            # section bodies normally end with a period/heading/anchor
            if STRAY_TAIL.match(last) and not last.endswith((".", ":", ")", "#")):
                try:
                    shown = str(p.relative_to(base))
                except ValueError:
                    shown = str(p)
                add("C5_fragment_tail", shown, f"ends with lone word: {last!r}")
            # fragment markers inside: '… of the' / 'the the' / double-space orphan
            for m in re.finditer(r"\b(the the|of the of|and the and)\b", text, re.I):
                # Column-seam guard (CDN-0196): a flattened table row pads its columns
                # with 2+ spaces, so the same function word ending one cell and opening
                # the next reads as a duplicate. Both words belong to their own cell and
                # deleting either one corrupts the row, so it is a table-wrap artefact —
                # not a fragment. Verified by inspection on 33 live hits (2026-10-26):
                # 29 are padded column seams in itaa-1997 tables, 1 was a real duplicate
                # in prose (fixed), 3 are single-space flattened rows in the NZ guides.
                if "  " in text[max(0, m.start() - 40):m.end() + 40]:
                    continue
                ln = text.count("\n", 0, m.start()) + 1
                try:
                    shown = str(p.relative_to(base))
                except ValueError:
                    shown = str(p)
                add("C5_fragment_inline", shown, f"line {ln}: {m.group(0)}")


# ── C11 (CDN-0186/0187): table cell coherence — mid-word splits, glyph junk ─
# Tables are the audit's blind spot: C5 skips anything inside '|' pipe rows,
# so words cut across cells, PDF formula glyphs, and truncated rows all pass.
# This class parses pipe-table cells:
#   (a) mid-word split: a cell ends with a short lowercase fragment (<=4 chars,
#       not itself a whole word preceded by a space) and the next cell starts
#       lowercase — e.g. '| ... ast | erisked terms ... |' (whole word cut)
#   (b) extraction glyphs: ç ÷ ´ ê ú æ û etc. inside any cell (formula junk)
#   (c) dangling end: a table row's final cell ends with a bare connector
#       ('the |', 'and |') — content truncated at the old page boundary
EXTRACT_GLYPH = re.compile(r"[ç÷´êëûüàáâãäåæôöòóõøèéíìîïñšžÿý]")

# A flagged char is NOT corruption when it is a normal diacritic inside an
# alphabetic word in natural-language text (e.g. "Fédération Internationale").
# Formula-font junk always shows up as a lone symbol, or as a prefix glued to
# the following word ("êTotal", "ç1", "´ fringe benefits") — never with letters
# on BOTH sides whose accent-folded form is a real word.
def _ASCII_FOLD(w: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c))


def _is_natural_word_accent(line: str, m: re.Match) -> bool:
    i = m.start()
    if i == 0 or not line[i - 1].isalpha() or i + 1 >= len(line) or not line[i + 1].isalpha():
        return False
    s = i
    while s > 0 and (line[s - 1].isalpha() or line[s - 1] in "'’"):
        s -= 1
    e = i + 1
    while e < len(line) and line[e].isalpha():
        e += 1
    word = line[s:e]
    if len(word) < 4:
        return False
    if _WORDS is None:
        return False
    return _ASCII_FOLD(word).lower() in _WORDS

# CDN-0193 Phase 1: the connector must TRAIL other text in the same cell.
# A cell whose entire content is one of these words is a complete value
# (gst-1999 s3-5 item 14 is the defined term "you"), not a truncation.
# Connector list widened: the nightly audit showed truncations ending on are/were/been/shall/
# may/from/into/than/its/their were invisible to this check.
DANGLE_CELL_END = re.compile(r"[^|\s]\s+(the|and|a|an|or|of|to|for|in|on|was|were|is|are|be|been|being|you|if|that|which|with|by|as|at|when|shall|may|might|must|from|into|than|its|their|this|these|those|any|each|such)\s*\|\s*$", re.I)

# /usr/share/dict/words (lowercase, ~104k) for the mid-word split test:
# flag cell boundary only if NEITHER side is a word but their concatenation is
# ('ast | erisked' -> 'asterisked'). Whole words on either side ('asset | when')
# are legitimate cell boundaries and are skipped.
try:
    _WORDS = set(open("/usr/share/dict/words", encoding="utf-8").read().split())
except Exception:
    _WORDS = None


def _tail_word(cell: str):
    """Last whitespace-separated token of a cell, stripped of leading markers."""
    t = cell.strip().split()[-1] if cell.strip().split() else ""
    return t.strip(",*'\"()") if t else ""


def _head_word(cell: str):
    """First whitespace token of a cell, stripped of leading markers."""
    parts = cell.strip().split()
    if not parts:
        return ""
    t = parts[0]
    return t.strip(",*'\"()") if t else ""


def _continuation_after(lines: list[str], ln_no: int, max_look: int = 4) -> tuple[str, str]:
    """Classify what follows a row that ends on a connector: 'wrap', 'header', or 'none'.

    A wrapped row ends on a connector by nature ("... to the" / "extent that ...") and the next row
    holds the rest. A column heading in a comparative table is also a split phrase - it is completed
    by the DATA rows below the '| --- |' rule, not by the line immediately after it, so the separator
    has to be looked through:

        | Item | Topic | These supplies are GST-free (except to the extent that |
        | --- | --- | --- |          <- the phrase continues in the cells below the rule

    What remains - a connector with no row, no heading rule, and no prose carrying it on - is the
    defect DANGLE_CELL_END was written for. Returns (kind, why), and the kind decides the class.
    """
    saw_separator = False
    for off in range(1, max_look + 1):
        j = ln_no - 1 + off
        if j >= len(lines):
            return "none", "end of file"
        nxt = lines[j]
        if not nxt.strip():
            continue
        if re.match(r"^\|\s*---", nxt.strip()):
            saw_separator = True
            continue
        if not nxt.startswith("|"):
            return "none", f"next non-empty line is not a table row ({nxt.strip()[:28]!r})"
        first = nxt.strip().strip("|").split("|")[0].strip()
        if saw_separator:
            return "header", "a heading rule follows, so the phrase continues in the rows below it"
        if not first:
            return "wrap", "next row opens with an empty first cell"
        if first[:1].islower():
            return "wrap", f"next row continues in lower case ({first[:24]!r})"
        return "none", f"next row starts a new item ({first[:24]!r})"
    return "none", "no following row"


def _header_fragmented_after(lines: list[str], ln_no: int, max_look: int = 6) -> bool:
    """Is the heading phrase actually FRAGMENTED over what became data rows?

    A comparative table legitimately ends a column heading on a connector and lets the data
    cells complete it - itaa-1997 118-300 heads a column "The *CGT event happens to this type
    of policy:" and "... and you are", then every data row starts with an item number and
    finishes the sentence ("1 | Any insurance policy ... | the insurer ..."). That is the
    publisher's design, not damage, and the 2026-11-02 scan counted 16 findings of which this
    shape is the bulk.

    The real defect is different: the heading itself was split over rows, so the row below the
    rule begins with a lower-case continuation instead of an item label - gst-1999 19-5:

        | You have an increasing | You have a decreasing | You have neither an |
        | --- | --- | --- |
        | adjustment, which will | adjustment, which will | increasing adjustment nor |

    So: emit only when the first non-empty row after the rule opens with a lower-case word.
    """
    saw_rule = False
    for off in range(1, max_look + 1):
        j = ln_no - 1 + off
        if j >= len(lines):
            return False
        s = lines[j].strip()
        if not s:
            continue
        if re.match(r"^\|\s*---", s):
            saw_rule = True
            continue
        if not s.startswith("|"):
            return False
        if not saw_rule:
            return False
        first = s.strip("|").split("|")[0].strip()
        return bool(first) and first[:1].islower()
    return False


def table_coherence_findings(lines: list[str], relpath: str) -> list[dict]:
    """The C11 detectors for ONE file. scan_table_coherence() walks the corpus
    and calls this; other tools (corpus_change_guard) reuse it per-file.

    S5 (2026-09-26) — are C11_table_midword and C11_table_truncated one class reported twice?
    No: they are two distinct defects. What made them look like one is that the per-line check
    stopped at the first match (glyph `continue`, truncated `continue`, midword tested last), so a
    row carrying both signatures was reported once and the second signature was invisible. The
    measured evidence: of 31 files with either class, 11 carry both, but 0 shared a LINE, and the
    identical "78 in 31 files" for both classes is a coincidence. Removing the first-match-wins
    `continue` makes 4 lines report both (itaa-1936 94.md:146, 121AS.md:57/203/205); the classes
    stay distinct and their counts rise to true incidence rather than being a floor.
    """
    out: list[dict] = []

    def add(cls, _path, detail):
        out.append({"class": cls, "path": relpath, "detail": detail})

    for ln_no, ln in enumerate(lines, 1):
        if not ln.startswith("|") or re.match(r"^\|\s*---", ln):
            continue
        # Every signature on the line is reported; the dedup key is (file, line, class), so one
        # row can name a glyph AND a truncation AND a mid-word split, but never the same class
        # twice (this is what keeps a repair worklist one row per row).
        emitted: set[str] = set()

        def emit(cls, detail):
            if cls in emitted:
                return
            emitted.add(cls)
            add(cls, relpath, detail)

        # (b) glyph junk (ignore natural-language accents, e.g. "Fédération")
        gm = next((m for m in EXTRACT_GLYPH.finditer(ln)
                   if not _is_natural_word_accent(ln, m)), None)
        if gm:
            emit("C11_table_glyph",
                 f"line {ln_no}: extraction glyph {gm.group(0)!r}: {ln.strip()[:90]}")
        # (c) dangling connector end - a defect ONLY when nothing continues the sentence.
        # A row that wraps ends on a connector by nature, so the old rule counted the wrap as damage
        # and named the class 'truncated'. Every one of the 138 findings was that shape, in corpora
        # the re-ingest never touched (the audit's own examples were gst-1999), which is how a class
        # can be 138 findings strong and still never have found the thing it was named for.
        if DANGLE_CELL_END.search(ln):
            kind, why = _continuation_after(lines, ln_no)
            if kind == "wrap":
                emit("C11_table_rowwrap",
                     f"line {ln_no}: row ends on bare connector, continued by the next row "
                     f"({why}): {ln.strip()[:80]}")
            elif kind == "header" and _header_fragmented_after(lines, ln_no):
                emit("C11_table_header_split",
                     f"line {ln_no}: column heading fragmented over the rows below the rule "
                     f"({why}): {ln.strip()[:80]}")
            # kind == "none": a connector at the end of a row proves nothing (the month "May", "entered
            # into", "passed on" are all complete). The 2026-11-01 audit measured C11_table_truncated at
            # 0 real truncations in 30 findings, so it is no longer emitted; the real defect has its own
            # signal below (C11_table_row_split).
        # (a) mid-word split across adjacent cells — wordlist test
        if _WORDS is None:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        for ci in range(len(cells) - 1):
            left, right = cells[ci], cells[ci + 1]
            tw = _tail_word(left).lower()
            hw = _head_word(right).lower()
            if len(tw) < 1 or len(hw) < 1:
                continue
            joined = tw + hw
            # A boundary is only legitimate when BOTH sides are whole
            # words ('asset | when'). If either half is a fragment, the
            # word was cut across cells ('d | efinitions' too — a
            # single-letter half is still a fragment, not a cell).
            if joined in _WORDS and not (tw in _WORDS and hw in _WORDS):
                emit("C11_table_midword",
                     f"line {ln_no}: '{tw}|{hw}' = '{joined}' cut across cells {ci+1}|{ci+2}: {ln.strip()[:100]}")
                break
    out.extend(_row_split_findings(lines, relpath))
    return out


# A footnote the publisher prints under a table is one spanning cell, not a split row.
_TABLE_FOOTNOTE = re.compile(r"^\|\s*\*(To find|For definition)", re.I)


def _cell_count(row: str) -> int:
    return len(re.split(r"(?<!\\)\|", row.strip().strip("|")))


def _row_split_findings(lines: list[str], relpath: str) -> list[dict]:
    if relpath.startswith("spec/"):
        return []  # spec/ is the app's own panel copy, not corpus data (same skip as C24)
    """C11_table_row_split: a data row that does not carry the header's cell count.

    One source cell the publisher wraps over several visual lines, emitted as several markdown ROWS,
    shifts the columns: the continuation rows have fewer cells than the header.
    """
    out: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and re.match(r"^\|\s*---", lines[i + 1]):
            want = _cell_count(lines[i])
            j = i + 2
            run = 0  # consecutive off-count rows are one split, reported once
            while j < len(lines) and lines[j].startswith("|"):
                n = _cell_count(lines[j])
                if n != want and not _TABLE_FOOTNOTE.match(lines[j]):
                    if not run:
                        out.append({"class": "C11_table_row_split", "path": relpath,
                                    "detail": f"line {j + 1}: row {j + 1} has {n} cells, header has "
                                              f"{want}: {lines[j].strip()[:80]}"})
                    run += 1
                else:
                    run = 0
                j += 1
            i = j
        else:
            i += 1
    return out


def scan_table_coherence():
    for act_dir in DATA.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            findings.extend(table_coherence_findings(lines, str(p.relative_to(DATA))))


# ── C6 (CDN-0081): stray trailing token at cut point (e.g. "payments") ──────
# A cut remnant duplicates the END of a heading: the title was severed at the cut point and the
# trailing token was stranded on the next line as its own two-word-at-most heading. It does NOT
# duplicate the START of the heading.
#
# Every finding this class produced (15, all in nz-master-tax-guide) was one legitimate shape: H1 is
# the heading as printed carrying its paragraph number ("Fishing gear and quotas ¶27-371") and H2 is
# the short topic heading the guide prints beneath it ("Fishing gear"). The H2 repeats the START of
# H1 - the topic - which is the act's own convention, so the check was reporting the guide's layout
# as damage while never seeing the shape it was written for.
#
# Fixed on 2026-09-19: a finding requires the repeated token to be the TRAILING token of H1. Acts
# whose headings carry paragraph references are reported by name and skipped, so the correction
# cannot silently become a check that cannot fire - the pinned self-test in
# scripts/test_c6_cut_token.py fails if the CDN-0081 shape stops being caught.
PARA_REF = re.compile(r"\s*¶\s*\d{1,3}-\d{1,4}\s*$")
_WORD_EDGE = "*_.,;:()[]'\""


def cut_token_shape(h1: str, h2: str) -> str:
    """Classify an H1/H2 pair: 'cut' (CDN-0081), 'convention' (legitimate), or 'none'.

    Split out from the scanner so the pinned self-test exercises the same code path the corpus
    scan uses, rather than a copy of it that can drift.
    """
    core = PARA_REF.sub("", h1)
    t1 = [w.lower().strip(_WORD_EDGE) for w in core.split() if w.strip()]
    t2 = [w.lower().strip(_WORD_EDGE) for w in h2.split() if w.strip()]
    if not t1 or not t2 or len(t2) > 2:
        return "none"
    shares = any(
        len(w1) >= 3 and len(w2) >= 3 and (w1.startswith(w2[:5]) or w2.startswith(w1[:5]))
        for w1 in t1 for w2 in t2
    )
    # The paragraph reference is what distinguishes the two conventions, not where the shared word
    # sits. A guide heading carries "¶NN-NNN" and is followed by a short topic heading restating
    # part of it - at the front ("Fishing gear and quotas" / "Fishing gear"), at the back
    # ("Introduction to tax credits" / "Credits"), or as a shared stem ("Tax rates for trusts" /
    # "Trustee income"). A cut remnant of the same shape is indistinguishable from it, so an act
    # using this convention is skipped by name rather than guessed either way.
    if PARA_REF.search(h1):
        return "convention" if shares else "none"
    trailing = len(t1[-1]) >= 3 and t1[-1].startswith(t2[0][:5]) and t2[0][:5] in t1[-1]
    leading = len(t1[0]) >= 3 and t1[0].startswith(t2[0][:5]) and t2[0][:5] in t1[0]
    if trailing and not leading:
        return "cut"                      # heading severed, trailing token stranded
    if leading and trailing and len(t1) == 1:
        return "cut"                      # the same single token repeated
    return "none"


def scan_stray_cut_tokens():
    for act_dir in DATA.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        convention = 0
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            # H1 then immediately a short H2 - the shape a cut point leaves behind
            m = re.search(r"^# (.+)\n\n## (.+)$", text, re.M)
            if not m:
                continue
            h1, h2 = m.group(1).strip(), m.group(2).strip()
            shape = cut_token_shape(h1, h2)
            if shape == "cut":
                add("C6_cut_token", str(p.relative_to(DATA)),
                    f"H1={h1!r} H2={h2!r} trailing token repeated - cut remnant")
            elif shape == "convention":
                convention += 1
        if convention:
            add("C6_cut_token:CONVENTION", f"data/{act_dir.name}/sections",
                f"{convention} H1+H2 pairs are the act's own heading/topic-heading convention "
                f"(H1 carries a ¶ reference, H2 repeats its opening words) - skipped, not a pass")


# ── C13 (CDN-0193 follow-up): duplicate anchors introduced by a re-ingest ──
# The gate verifies tokens, not markup, so a re-ingest can emit the same <a id="..."> hook twice for
# one printed subparagraph and pass every check. Duplicate anchors break deep links: a fragment URL
# resolves to the first match, so the second copy is unreachable. Found on 2026-09-19 by the
# structural integrity gate AFTER a 4,629-section apply that every per-block check had passed -
# byte-equality against the gate-verified output and the corruption counts are both blind to it.
def scan_duplicate_anchors():
    for act_dir in DATA.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            ids = re.findall(r'<a id="([^"]+)"', p.read_text(errors="replace"))
            dupes = sorted(k for k, v in Counter(ids).items() if v > 1)
            if dupes:
                add("C13_duplicate_anchor", str(p.relative_to(DATA)),
                    f"{len(dupes)} duplicated anchor id(s): {','.join(dupes[:6])}")


# ── C14 (CDN-0193): formula structure lost - every term present, nothing relating them ──
# Drawn operators (x, -, +, division) are set in subsetted Symbol fonts whose ToUnicode maps them to
# a plain space, so a re-ingested formula keeps the terms and loses the structure between them:
#
#     ```ingest-formula source=vol08.pdf page=160
#     Owned deductions + Acquired deductions        *Corporate tax rate     <- the x is gone
#     ```
#
# A fence whose lines carry a fraction rule is NOT a finding: the rule IS the structure (numerator
# over denominator) and it survives extraction. What is reported is a fence with no operator and no
# rule anywhere - the formula a reader cannot reconstruct. The fence marker names its own source page,
# so every finding is locatable, unlike the gate-derived operator worklist.
#
# Measured 2026-09-19: 963 suspect lines across 173 ITAA sections and 201 distinct source pages. This
# is the real size of the drawn-operator problem; the 67 positions in /tmp/operator-worklist.json were
# a small gate-derived subset whose characters the gate had wrong.
FORMULA_FENCE = re.compile(r"```ingest-formula source=(\S+) page=(\d+)")
# Operators that SURVIVE extraction, so their presence means the structure is not lost.
# The en dash (U+2013) is the printed subtraction in these formulas and it does come through:
# PDF-verified on the source pages named by the fences - 40-75 vol02 p49 prints "365 – 25" and
# its fence contains the en dash, 104-95 vol03 p228 prints "$5,000 – $4,500" likewise. Adding it
# (2026-09-22) removed 15 of 80 findings that were reporting a formula whose only missing glyph
# was the multiplication sign, i.e. they were counting a printed operator as an absent one.
# DELIBERATELY NOT operators, though they occur in fences:
#   '*'  the defined-term marker the Act prints on the first occurrence of a term (*All Groups,
#        *exempt income) - 40 of the 80 findings contain one and none of them is arithmetic;
#   '-'  an intra-word hyphen ("Write-off days in income year") - 23 of the 80;
#   'x'  a letter inside a word ("expenditure", "tax") - 0 standalone occurrences corpus-wide.
# This class is therefore a FLOOR on the drawn-operator problem, not a measurement: a fence that
# kept its en dash and lost its multiplication sign is no longer reported. The restore workstream
# reads operators off the page (scripts/apply_operator_proposal.py, OPERATORS = "×÷−–—+=-≤≥±<>,*")
# rather than trusting a fence's surviving characters.
_FORMULA_OPS = "+\u2212\u00d7\u00f7=\u2013\u2014"
_FRACTION_RULE = re.compile(r"_{4,}")


def scan_lost_formula_structure(root=None):
    """Formula fences with terms but nothing relating them.

    `root` defaults to the live corpus (DATA), mirroring scan_asterisk_noise; the pinned
    self-test points it at a scratch corpus so it can assert both directions.
    """
    base = Path(root) if root else DATA
    for act_dir in base.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            lines = p.read_text(errors="replace").splitlines()
            i = 0
            while i < len(lines):
                m = FORMULA_FENCE.match(lines[i])
                if not m:
                    i += 1
                    continue
                body, j = [], i + 1
                while j < len(lines) and not lines[j].startswith("```"):
                    body.append(lines[j])
                    j += 1
                text = " ".join(body)
                has_op = any(o in text for o in _FORMULA_OPS)
                has_rule = bool(_FRACTION_RULE.search(text))
                meaningful = [b for b in body if b.strip() and not b.strip().startswith("---")]
                if meaningful and not has_op and not has_rule:
                    add("C14_formula_structure", str(p.relative_to(base)),
                        f"line {i + 1}: {m.group(1)} p{m.group(2)} - {len(meaningful)} term line(s), "
                        f"no operator and no fraction rule: {meaningful[0].strip()[:70]}")
                elif meaningful and has_rule and not has_op:
                    add("C14_fraction_rule", str(p.relative_to(base)),
                        f"line {i + 1}: {m.group(1)} p{m.group(2)} - fraction rule present "
                        f"({len(meaningful)} line(s))")
                i = j


# ── C7 (CDN-0124): chapeau dropped — section body missing opening paragraph ──
# If the first body paragraph after frontmatter starts with a subsection marker
# or is empty → chapeau likely dropped.
def scan_chapeau():
    for act_dir in DATA.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            m = re.search(r"^---\n.*?\n---\n(.*)$", text, re.S)
            if not m:
                continue
            body = m.group(1).strip()
            if not body:
                add("C7_chapeau", str(p.relative_to(DATA)), "empty body")
                continue
            first_par = body.split("\n\n", 1)[0].strip()
            if re.match(r"^\(\d+\)", first_par) and len(first_par) < 80:
                add("C7_chapeau", str(p.relative_to(DATA)),
                    f"body starts with subsection ({first_par[:60]!r}) — chapeau may be missing")


# ── C8 (CDN-0095/0096/0097): formatting artifacts in bodies ─────────────────
ARTIFACT_PATS = {
    "header_slash": re.compile(r"/header/|/footer/|/content/"),
    "md_link_junk": re.compile(r"\]\(\s*\)|!\[\]\(\)"),
    "double_heading": re.compile(r"^##\s*$", re.M),
    "stray_bullet": re.compile(r"^\s*[-*]\s*$", re.M),
}
# ── C15: characters only a drawing produces, inside a formula fence ─────────
# The text layer of these volumes names each drawn glyph after the Latin-1 character at the same
# numeric code, so a fence can carry a bracket piece ('ç', 'ê', 'ø') or a multiplication sign ('´')
# where the page prints a delimiter or an operator.  Left in place it reads as content and defeats
# the formula.  The ones already adjudicated were repaired from the page, so a survivor is either a
# piece nobody has read yet or a repair that missed; either way the page decides, so this reports
# and never rewrites.  CDN-0211: '÷' (and '±') are legitimate drawn operators — the same set
# CDN-0202 adjudicated as real formula operators (OPERATORS = "×÷−–—+=-≤≥±<>,*") — so a fence
# carrying only '÷' is a *complete* formula ("=300% ÷ 4 = 75%"), not a lost-bracket finding.
# The superscripts ('²³') and degree sign stay: they are either legitimate units or drawn glyphs,
# and the page decides.  A hit on the accented vowels/spacing accents still means "look at the page".
DRAWING_CHARS = "´¯°¸æçèéêëöøùúûü²³"
OPEN_FENCE = re.compile(r"^```")


def scan_page_rule_artifacts(root=None):
    """C16: a page rule left in PROSE (CDN-0203).

    The source PDFs draw a horizontal rule at a page break; the extractor kept it as a run of
    underscores, which splits the sentence and swallows the Note that followed.  Runs INSIDE a
    formula fence are legitimate fraction bars (62 of them across 52 files) and are skipped -
    which is why this walks fences instead of grepping.  `root` lets the pinned self-test point
    the same code at a scratch corpus.
    """
    base = Path(root) if root else DATA
    for act_dir in base.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            in_fence = False
            for i, line in enumerate(text.split("\n"), 1):
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if not in_fence and "_" * 6 in line:
                    add("C16_page_rule", str(p.relative_to(base)),
                        f"line {i}: a page rule left in prose: {line.strip()[:60]!r}")


def scan_drawing_characters(root=None):
    base = Path(root) if root else DATA
    for act_dir in base.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            in_fence = False
            for i, line in enumerate(text.split("\n"), 1):
                if OPEN_FENCE.match(line):
                    in_fence = not in_fence
                    continue
                if not in_fence:
                    continue
                hit = sorted({c for c in line if c in DRAWING_CHARS})
                if hit:
                    add("C15_drawing_char", str(p.relative_to(base)),
                        f"line {i}: {hit} in {line.strip()[:60]!r}")


def scan_formatting_artifacts():
    for act_dir in DATA.iterdir():
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            text = p.read_text(errors="replace")
            for name, pat in ARTIFACT_PATS.items():
                m = pat.search(text)
                if m:
                    ln = text.count("\n", 0, m.start()) + 1
                    add(f"C8_{name}", str(p.relative_to(DATA)), f"line {ln}: {m.group(0)[:40]!r}")
                    break


# ── C9 (CDN-0007/0048/0071): definitions index quality ──────────────────────
def scan_definitions():
    try:
        store = json.loads((DATA / "definitions_all.json").read_text())
    except Exception as e:
        add("C9_definitions", "data/definitions_all.json", f"unreadable: {e}")
        return
    for act, act_data in store.items():
        terms = act_data.get("terms", {})
        # term keys that look truncated (end with 'the'/'of'/hyphen)
        for term in terms:
            if re.search(r"\b(the|of|and|to)\s*$", term, re.I) and len(term) > 8:
                add("C9_definitions", "data/definitions_all.json",
                    f"{act}: term looks truncated: {term!r}")
        # terms without anchor (index entry that can't resolve)
        for term, info in terms.items():
            if not info.get("anchor") and not info.get("section"):
                add("C9_definitions", "data/definitions_all.json",
                    f"{act}: term {term!r} missing anchor+section")


# ── C10 (CDN-0051/0073/0120): case citations year-collision / name issues ───
def scan_case_citations():
    # NOTE: FCAFC case numbers restart every year, so "[2018] FCAFC 122" and
    # "[2024] FCAFC 122" are DIFFERENT cases — normalizing the year away and
    # counting repeats is a false-positive factory. Only flag:
    #   (a) the exact same citation appearing more than once (true duplicate),
    #   (b) the same court+number with NO year at all (year-less citation —
    #       the CDN-0120 class that year-blind enrichment produced).
    for f in ["fcafc_tax_cases.json", "case_section_refs.json"]:
        p = DATA / f
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else list(data.values())
        seen_exact = Counter()
        seen_courtnum = Counter()
        for it in items:
            if isinstance(it, dict):
                c = it.get("citation", "")
                if not c:
                    continue
                # (a) exact duplicates
                seen_exact[c] += 1
                # (b) year-less court+number (only when the citation has a
                # court prefix like 'FCAFC 122' but no [year])
                if re.match(r"^FCAFC\s+\d+", c):
                    seen_courtnum[c] += 1
        for cit, cnt in seen_exact.items():
            if cnt > 1:
                add("C10_citation_collision", f"data/{f}",
                    f"{cit!r} appears {cnt} times — exact duplicate")
        for cit, cnt in seen_courtnum.items():
            if cnt > 1:
                add("C10_citation_collision", f"data/{f}",
                    f"{cit!r} appears {cnt} times — year-less citation (CDN-0120 class)")


def scan_missing_heading(check_root: str | None = None):
    """C12 (from the nightly mutation test, 2026-09-18): a section can lose its H1 and no class trips.

    The nightly investigator planted four defects in a copy of 30-315.md. Three were caught exactly
    (glyph, mid-word split, truncated row); deleting the '# ' H1 line was NOT caught by any class in
    this set, nor by the integrity gate's tail checks. A clean audit therefore said nothing about
    every section's title surviving.

    The rule is derived from the corpus's own convention rather than asserted: an act whose section
    files overwhelmingly carry an H1 is an act where a file without one is a defect. Acts that do not
    follow the convention are reported by name and skipped, so the check cannot become a
    false-positive factory.
    """
    root = Path(check_root) if check_root else DATA
    acts = sorted(p for p in root.glob("*/sections") if p.is_dir())
    for act_sections in acts:
        files = sorted(act_sections.rglob("*.md"))
        if not files:
            continue
        with_h1 = 0
        missing = []
        for p in files:
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            # an H1 is a '# ' line, ignoring YAML frontmatter
            body = text.split("---", 2)[-1] if text.startswith("---") else text
            if re.search(r"^#\s+\S", body, re.M):
                with_h1 += 1
            else:
                missing.append(p)
        share = with_h1 / len(files)
        act = act_sections.parent.name
        # The act's own tree.json is the authority for "this section has a title", not the
        # corpus's prevailing convention. When every copy loses its H1 the convention test
        # divides zero by everything and reported the whole-act loss as a SKIP (proved by the
        # nightly investigator on 2026-10-19). A section that tree.json titles and whose file
        # carries no H1 is damage whether or not other files kept theirs.
        try:
            tree = json.loads((act_sections.parent / "tree.json").read_text())
        except Exception:
            tree = {}
        titles: dict[str, str] = {}

        def _collect(node) -> None:
            if isinstance(node, dict):
                if node.get("id") and node.get("title"):
                    titles.setdefault(str(node["id"]), str(node["title"]))
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(tree)
        titled_losses = 0
        for p in missing:
            title = titles.get(p.stem)
            if not title:
                continue
            try:
                shown = p.relative_to(DATA.parent)
            except ValueError:
                shown = p          # scanning a sandbox outside the repo (how this is tested)
            add("C12_missing_heading", str(shown),
                f"tree.json titles this section '{title}' but the file carries no '# ' "
                f"heading - title lost from the document")
            titled_losses += 1
        if share < 0.8 and titled_losses == 0:
            add("C12_missing_heading:CONVENTION", f"data/{act}/sections",
                f"only {with_h1}/{len(files)} files ({share:.0%}) carry an H1 - convention not "
                f"established for this act, check skipped (not a pass)")
            continue
        # The untitled remainder is only asserted where the convention holds. Without this
        # guard one provable titled loss in a low-H1 act reported every other untitled file
        # in that act as damage - the false-positive factory the convention test exists to
        # prevent (caught by scripts/test_c12_missing_heading.py).
        if share < 0.8:
            continue
        for p in missing:
            if titles.get(p.stem):
                continue       # already reported as provable title loss above
            try:
                shown = p.relative_to(DATA.parent)
            except ValueError:
                shown = p          # scanning a sandbox outside the repo (how this is tested)
            add("C12_missing_heading", str(shown),
                f"no '# ' heading in a file of an act where {with_h1}/{len(files)} "
                f"({share:.0%}) have one - title lost or never written")


# ══ C17-C27: the classes the 2026-09-25 plan measured but no detector covered ══
# Every one of these was measured at HEAD on 2026-09-26 by RUNNING it; the count in each
# docstring is that measurement, next to the plan's number where they differ. Each has a
# self-test in scripts/test_c2X_*.py with one planted positive and one planted negative
# (phase 4 of scripts/final_data_audit.py runs them nightly), because a detector that
# cannot fail is decoration.

# ── C17 (CDN-0124): a corps section body that starts at (a)/(b) - the chapeau is gone ──
# ingest_corps_act.py's stray-heading fallback (:286-303) deletes any first line under 120
# characters unless it starts with '(', 'Note:', 'Example:', 'In this Act:', 'Where ' or
# 'If '. A chapeau ending "...provided by others If:" has no trailing space after 'If', so
# it does not match 'If ' and is deleted with the other ~403 real opening words; the file
# then begins at the first item marker. The fix is the ingest, not the corpus: this reports.
# Measured 2026-09-26: 313 findings - exactly the plan's 313, all in corporations-act-2001
# (itaa-1997/itaa-1936/gst-1999 have 0).
CHAPEAU_ITEM = re.compile(r"^\**\((?:a|b)\)\**")


def first_body_line(text: str) -> str:
    """First non-empty body line that is not a heading (frontmatter stripped)."""
    body = text.split("---", 2)[-1] if text.startswith("---") else text
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def scan_missing_chapeau(root=None):
    base = Path(root) if root else DATA
    for p in sorted((base / "corporations-act-2001" / "sections").rglob("*.md")):
        first = first_body_line(p.read_text(errors="replace"))
        if CHAPEAU_ITEM.match(first):
            add("C17_missing_chapeau", str(p.relative_to(base)),
                f"body starts at an item marker, no chapeau above it: {first[:70]!r}")


# ── C18 (CDN-0173): nz-it-2007 serves amendment-history sections as if they were law ──
# parse_nz_it.py:193 soup.find_all("div", class_="part") also matched the parts nested inside
# div.end / div.skeletons / div.skeleton-act (endnote copies of at least 11 amending Acts) and
# div.schedule-amendments / div.amend, so part ids '1', '2', '3' and '3B' sit in the tree
# beside the consolidated Parts A-Z and their files overwrite each other. This is a tree-level
# check: a consolidated Act's parts are letters.
# Measured 2026-09-26: 445 findings (part 1: 374, 2: 40, 3: 8, 3B: 23) - exactly the plan's
# 445. 87 of the titles also match the "(replaced)/(repealed)/New section" shape and every one
# of them is inside those four parts, so they add nothing to the count.
NZ_AMENDMENT_TITLE = re.compile(r"^(Section .* (?:replaced|repealed)|New section)")


def scan_nz_amendment_parts(root=None):
    base = Path(root) if root else DATA
    tree = base / "nz-it-2007" / "tree.json"
    if not tree.exists():
        return
    try:
        t = json.loads(tree.read_text())
    except Exception as e:
        add("C18_nz_amendment_part", str(tree.relative_to(base.parent)), f"unreadable: {e}")
        return
    tree_rel = str(tree.relative_to(base.parent))
    for part in t.get("parts", []):
        pid = str(part.get("id", ""))
        amendment_part = not re.match(r"^[A-Z]$", pid)
        nodes = [part]
        for d in part.get("divisions", []):
            nodes.append(d)
            nodes += d.get("subdivisions", [])
        for node in nodes:
            for s in node.get("sections", []):
                title = str(s.get("title", ""))
                if amendment_part:
                    add("C18_nz_amendment_part", tree_rel,
                        f"part {pid!r} is not a consolidated Part A-Z: section {s.get('id')} "
                        f"({title[:50]!r}) is amendment-history text served as law, path "
                        f"{s.get('path')}")
                elif NZ_AMENDMENT_TITLE.match(title):
                    add("C18_nz_amendment_part", tree_rel,
                        f"section {s.get('id')} in Part {pid}: title is an amendment-history "
                        f"heading ({title[:60]!r})")


# ── C19 (CDN-0204): a served ruling title that is a fragment ────────────────
# Titles come from backend/services/data_loader.load_rulings() - the list the API actually
# serves - so the check cannot drift from the served artefact. The three shapes are the ones
# the ticket measured: 219 titles started lower case, 17 with a digit or '(', 106 ended on a
# connector such as 'of'/'to'.
# Measured 2026-09-26: 26 findings, i.e. the plan's TARGET, not its 273: the 0204 extractor
# (_wrapped_title / _best_authoritative / _is_title_truncation) landed in commit 3b715b745,
# so the 273 the plan measured at its own HEAD is already repaired. The detector pins it.
TITLE_CONNECTOR_END = re.compile(
    r"\b(?:the|of|and|to|for|in|on|with|by|as|at|is|are|was|were|a|an|or|from|that|which)\s*$",
    re.I)


def title_fragment_reason(title: str | None) -> str | None:
    """Why a served ruling title reads as a fragment (CDN-0204), or None if it is whole.

    Split out from the scanner so the pinned self-test exercises the same decision the corpus
    scan uses, rather than a copy of it that can drift.
    """
    t = (title or "").strip()
    if not t:
        return "empty title"
    if t[:1].isalpha() and t[:1].islower():
        return "starts lower case - the extracted title is the middle of a wrapped title"
    if re.match(r"^[\(\d]", t):
        return "starts with a digit or '(' - the extracted title is a body fragment"
    if TITLE_CONNECTOR_END.search(t):
        return "ends on a connector word - the title was cut"
    return None


NZ_CODE_LINE = re.compile(r"^[A-Z]{2,6}\s+\d{4}/\d+")
NZ_HEAD_NOISE = re.compile(r"^(https?://|Inland Revenue|IRD|Page \d)", re.I)


def _nz_document_title(source: str | None, head_chars: int = 600) -> str:
    """The NZ ruling's own first non-code line, or "" when it cannot be read.

    The curated sidecar (`_best_authoritative`) resolves against an AU summary title and is
    empty for every NZ row, so CDN-0204's shape rule had nothing to compare the served title
    against and reported 65 correct year-led titles as fragments (DET and OS types are
    legitimately titled "<year> ..."). The document's own head is that missing evidence:
    measured 2026-10-13, the served title equals the source file's first non-code line for
    1,595 of 1,596 NZ rows. Agreement suppresses the finding as before; disagreement still
    reports it, so a genuinely cut NZ title is still caught.
    """
    if not source:
        return ""
    try:
        with open(source, encoding="utf-8", errors="replace") as fh:
            text = fh.read(head_chars)   # only the head is ever used
    except OSError:
        return ""
    for line in (ln.strip() for ln in text[:head_chars].splitlines()):
        if not line or NZ_CODE_LINE.match(line) or NZ_HEAD_NOISE.match(line):
            continue
        return line
    return ""


def _served_ruling_titles() -> list[tuple[str, str, str]]:
    """(citation, served full_title, curated title) for every ruling the API serves.

    The curated title is the sidecar/summary title the extractor treats as authoritative
    (`_best_authoritative`); it is empty when neither exists.
    """
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.services.data_loader import (          # deferred: needs the backend env
        load_rulings, _best_authoritative, _plausible_authoritative,
        _strip_leading_citation, _summary_title,
    )
    out: list[tuple[str, str, str]] = []
    for r in load_rulings():
        cit = str(r.get("citation", "?"))
        served = r.get("full_title") or ""
        curated = ""
        if str(r.get("jurisdiction", "")).upper() == "NZ":
            # Independent evidence: the ruling's own text, not the AU summary lookup.
            curated = _nz_document_title(r.get("source"))
        if not curated:
            try:
                curated = _best_authoritative(
                    None, _plausible_authoritative(_strip_leading_citation(_summary_title(cit)), cit)
                ) or ""
            except Exception:
                curated = ""
        out.append((cit, served, curated))
    return out


def scan_ruling_title_fragments(titles=None):
    """`titles` is the test seam: the pinned self-test passes planted (citation, title[, curated]).

    CDN-0216: a title that reads as a fragment is only a defect when the curated title does
    not agree with it. 'rhipe Limited - scheme of arrangement' and '1300 Smiles Limited - …'
    start lower case / with a digit but are the whole title, not a cut body line; the curated
    sidecar says so, and the shape rules cannot tell a brand from a fragment. When the two
    AGREE the extractor did not corrupt anything, so the finding is suppressed. When they
    DIFFER the served title really is a cut title and the finding stands.
    """
    if titles is None:
        try:
            titles = _served_ruling_titles()
        except Exception as e:
            add("C19_ruling_title_fragment:CONVENTION", "backend/services/data_loader.py",
                f"load_rulings() unavailable in this environment ({type(e).__name__}: {e}) - "
                f"check skipped, not a pass")
            return
    for row in titles:
        citation, title = row[0], row[1]
        curated = row[2] if len(row) > 2 else ""
        why = title_fragment_reason(title)
        if not why:
            continue
        if curated and _title_norm(title) == _title_norm(curated):
            # shape says 'fragment', the curated title says 'whole' - the shape rule is the
            # weaker evidence, so the curated agreement wins.
            continue
        add("C19_ruling_title_fragment", f"data/rulings/{citation}",
            f"{why}: {title[:90]!r}")


def _title_norm(s: str) -> str:
    """Lowercase + strip all punctuation/whitespace runs - for title comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ── C20-C26 (plan Part B / E-a..E-g): character and encoding classes ────────
# One detector, one class per defect, over every text file the corpus serves: section
# markdown, the tree/index JSON, rulings text and their derived summaries, the derived
# case-summary corpus the FTS is built from, maps, the Keays chapters and the regulatory
# guide texts. Raw AustLII HTML is deliberately NOT scanned: entities in .html are correct
# there (the browser renders them) and the defect is in the DERIVED text only.
NON_LATIN_SCRIPTS = {
    # Latin + combining marks (OECD and NZ Maori macrons: "Tāwhirimātea") are NOT in these
    # blocks, so macrons cannot fire. The Arabic block stops at U+FEFF: it is the BOM and is
    # reported by C23 as an invisible character, not as Arabic.
    #
    # Greek is DELIBERATELY ABSENT (removed 2026-10-05, CDN-0217). Every Greek occurrence the
    # corpus has ever produced is legitimate notation, not script corruption: Σ the summation
    # symbol in tax formulas (nz-it-2007 EC-17/EZ-4B/FP-24, the 122A/122-60 rollover maps),
    # π the constant in a ruling's frustum-volume formula (TR 95/36), and α/β in the protein
    # names β-lactoglobulin / α-lactalbumin in a derived case summary (2025_FCA_270). Cyrillic
    # was the real mojibake (the 1,370 CP866 "тАв" triplets) and stays; Greek-only "corruption"
    # has never been observed, and a detector that fires on summation signs would condemn every
    # formula in the corpus the way a macron detector would condemn the OECD guides.
    "cyrillic": re.compile(r"[\u0400-\u04FF\u0500-\u052F]"),
    "cjk": re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]"),
    "arabic": re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFE]"),
    "hebrew": re.compile(r"[\u0590-\u05FF\uFB1D-\uFB4F]"),
}
HTML_ENTITY = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});")
LIGATURE = re.compile(r"[\uFB00-\uFB06]")
INVISIBLE = re.compile(r"[\uFEFF\u200B-\u200D]")
NONSTANDARD_HYPHEN = re.compile(r"[\u2010\u2011]")
SOFT_HYPHEN = re.compile(r"\u00AD")
PUA_GLYPH = re.compile(r"[\uE000-\uF8FF]")

# (class, regex, what, skip inside a fence, skip on a table row, sample note)
_CHARACTER_CLASSES = (
    ("C21_html_entity", HTML_ENTITY, "raw HTML entity in derived text", False, False,
     "the derived-text cleaners unescape nothing; rebuild the row through html_to_text()"),
    ("C22_ligature", LIGATURE, "typographic ligature never NFKC-normalised", False, False,
     "MCP/embeddings/FTS read the raw text, so 'financial' does not match 'financial'"),
    ("C23_invisible_char", INVISIBLE, "zero-width/invisible character", False, True,
     "FEFF breaks quoted search and '(1)(a)' citation regexes"),
    ("C23_nonstandard_hyphen", NONSTANDARD_HYPHEN, "non-standard hyphen", False, True,
     "FTS5 treats U+2011 as a separator, so quoted '\"sub-fund\"' returns 0 rows"),
    ("C23_soft_hyphen", SOFT_HYPHEN, "soft hyphen", False, True,
     "renders as nothing, or as a missing hyphen where it stands in for one"),
    ("C26_pua_glyph", PUA_GLYPH, "private-use-area glyph (Symbol/Wingdings font)",
     False, False, "a drawn operator or bullet passed through as F0xx/E0xx"),
)


def character_corpus_files(base: Path) -> list[Path]:
    """Every served text file the character classes apply to (never .html).

    Deduped: `maps` is reached twice (once as a top-level directory with no sections/, once via
    the extras list below), and a duplicate entry double-counted every character in it.
    """
    files: list[Path] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if (d / "sections").is_dir():
            files += sorted((d / "sections").rglob("*.md"))
        elif d.name in ("maps", "spec"):
            files += sorted(p for p in d.rglob("*") if p.is_file() and p.suffix != ".html")
    for sub in (("regulatory-guides", "texts"), ("insolvency-keays", "chapters"),
                ("rulings", "summaries"), ("rulings",), ("maps",)):
        d = base.joinpath(*sub)
        if d.is_dir():
            files += sorted(p for p in d.glob("*") if p.is_file() and p.suffix != ".html")
    files += sorted(base.glob("*/tree.json")) + sorted(base.glob("*/section_index.json"))
    return sorted(set(files))


def scan_character_classes(root=None):
    """C20-C26 in one pass: each class counts characters (the unit the plan measured in).

    Findings are one per (file, class, kind) with the occurrence count in `count`, so a
    class that fires 22,000 times does not write 22,000 JSON rows. Measured 2026-09-26:
      C20 4,110 Cyrillic chars in 897 fields of 2 master-tax-guide JSON files (= the plan's
          1,370 'тАв' triplets) plus 16 Greek chars in 5 files;
      C21 52 entities in 50 scripts/cleaned/summaries files (the plan's 50 FTS rows) plus
          132 in 4 ruling-summary files and 9 in 6 maps;
      C22 22,212 ligatures (MTG 18,688 in 1,315 files - the plan's exact figure - Keays 3,107
          in 21, MTG section_index.json 404, maps 13); plan: 21,000 or more;
      C23_invisible_char 1,947 (the plan's exact 1,947, all U+FEFF in nz-it-2007);
      C23_nonstandard_hyphen 3,045 (plan: 3,034 in corps + 11 elsewhere);
      C23_soft_hyphen 36 (plan: 35);
      C26_pua_glyph 1,969 (plan: 46 in sections + 1,919 in regulatory guides).
    """
    base = Path(root) if root else DATA
    files = character_corpus_files(base)
    if root is None:
        # The derived case-summary corpus the case_summaries_fts table is built from lives
        # outside data/ (data_loader.CASE_SUMMARIES_DIR). Only the live run reaches it;
        # the pinned self-test plants its own derived files under the scratch root.
        d = Path(os.environ.get("CASE_SUMMARIES_DIR",
                                str(Path(__file__).resolve().parent.parent
                                    / "scripts" / "cleaned" / "summaries")))
        if d.is_dir():
            files += sorted(p for p in d.glob("*.json") if p.is_file())
    for p in files:
        if p.suffix == ".html":
            continue                      # entities in raw case HTML are correct, not a defect
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(p.relative_to(base))
        except ValueError:
            rel = str(p)                  # CASE_SUMMARIES_DIR sits outside the corpus root
        agg: dict[tuple[str, str], int] = {}
        samples: dict[tuple[str, str], tuple[str, str]] = {}
        in_fence = False
        for line in text.split("\n"):
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            table_row = line.lstrip().startswith("|")
            for script, rx in NON_LATIN_SCRIPTS.items():
                found = rx.findall(line)
                if found:
                    key = ("C20_non_latin_script", script)
                    agg[key] = agg.get(key, 0) + len(found)
                    samples.setdefault(key, (line.strip()[:70], "".join(found)))
            for cls, rx, _what, skip_fence, skip_table, _note in _CHARACTER_CLASSES:
                if (skip_fence and in_fence) or (skip_table and table_row):
                    continue
                found = rx.findall(line)
                if found:
                    key = (cls, "".join(sorted(set(found))))
                    agg[key] = agg.get(key, 0) + len(found)
                    samples.setdefault(key, (line.strip()[:70], "".join(found)))
        for (cls, kind), n in sorted(agg.items()):
            sample, matched = samples.get((cls, kind), ("", kind))
            # the matched characters are named by code point as well as by glyph: U+FEFF and
            # U+00AD print as nothing, so a report that shows only the character is unreadable.
            codes = " ".join(f"U+{ord(c):04X}" for c in dict.fromkeys(matched))[:40]
            add(cls, rel, f"{n} x {kind or '(invisible)'} [{codes}]: {sample!r}", count=n)


# ── C24 (E-d): a bold heading glued to the end of a paragraph ───────────────
# The ingester joined the heading to the paragraph it follows instead of starting a new line,
# so "…effect to this Act. **Penalties**" reads as body text and the heading is not a heading.
# Measured 2026-09-26: 176 findings in 89 files, all in aml-ctf-2006, and the scan was hard-wired
# to that one act - so the identical shape in itaa-1997, gst-1999 or corporations-act was
# invisible.  Generalised 2026-10-29 across all 54 acts: the same regex returns aml-ctf-2006 = 0
# (repaired since) and 2 hits in `spec`, both false positives ("**External links row:**",
# "**No panels below body**" - site chrome and a markdown link row).  SKIP_BOLD and the
# trailing-colon guard exclude exactly those.
GLUED_HEADING = re.compile(r"[.;:)] \*\*[A-Z][^*\n]{0,100}\*\*[ \t]*$", re.M)
SKIP_BOLD = re.compile(
    r"^(?:Note|Notes|Example|Examples|Item|Items|Step|Steps|Table|Diagram|Figure|"
    r"Section|Sections|Division|Divisions|Part|Parts|Subdivision|Schedule)\b"
)


def scan_glued_headings(root=None):
    base = Path(root) if root else DATA
    for p in sorted(base.glob("*/sections/**/*.md")):
        # `spec` is this project's own documentation, not legislation: its "**No panels below
        # body**" note is site chrome that no ingester glued.
        if p.relative_to(base).parts[:1] == ("spec",):
            continue
        text = p.read_text(errors="replace")
        for m in GLUED_HEADING.finditer(text):
            bold = m.group(0).split("**")[1].strip()
            if not bold or bold.endswith(":") or SKIP_BOLD.match(bold):
                continue
            ln = text.count("\n", 0, m.start()) + 1
            add("C24_glued_heading", str(p.relative_to(base)),
                f"line {ln}: heading glued to the paragraph: {m.group(0).strip()[-60:]!r}")


# ── C25 (E-f): endnote / table-of-contents text leaked into a section ───────
# parse_gst1999.py and parse_fbt_sis.py have no endnote cutoff, so the whole "Endnote 4 -
# Amendment history" block landed inside the act's last section as one flattened line; the
# OECD table of contents leaked into c31-32 as a spaced dot leader.
#
# The plan's first draft of this rule ("5+ dots in a line longer than 2,000 characters, or after
# an Endnote heading") fires 18 times in 11 files, and 15 of those are the legitimate population
# the same plan says must not fire: the itAA-1997 Div 10/11/12 checklists, the master-GST-guide
# checklists and glossary, and the MTG rate layouts are also single long flattened lines with dot
# leaders. What separates them is the CONTEXT, not the length: the artefact line is endnote text,
# so an Endnote heading or amendment-history wording sits in it or just above it. With that
# requirement the class fires on exactly the 3 files the plan named - gst-1999/195-1.md:2483,
# sis-1993/381.md:772 and fbt-1986/167.md:809 (measured 2026-09-26: 18 rows, 11 -> 3 files at
# first cut; see the commit) - and 381.md:775 still fires from the block context.
#
# The legitimate rows are not asserted clean by a list: the self-test plants the plan's own
# `child care subsidy ...... 52-150` negative and a long flattened checklist row.
DOT_RUN = re.compile(r"\.{5,}")
TOC_LEADER = re.compile(r"\. \. \. \.")
ENDNOTE_HEAD = re.compile(r"^[ \t]*#{0,6}[ \t]*(?:endnote|endnotes)\b", re.I)
# Amendment-history wording, which is what the leaked block carries.
ENDNOTE_MARK = re.compile(r"\bendnote\b|\bamendment history\b|\bamended by\b|\brepealed by\b"
                          r"|\bas amended\b|\bamending Act\b", re.I)


def _endnote_context(lines: list[str], i: int, window: int = 5) -> bool:
    """True when an Endnote heading or amendment-history wording sits in the 5 lines above i."""
    return any(ENDNOTE_HEAD.match(lines[j]) or ENDNOTE_MARK.search(lines[j])
               for j in range(max(0, i - window), i))


def scan_dot_leaders(root=None):
    base = Path(root) if root else DATA
    for d in sorted(x for x in base.iterdir() if (x / "sections").is_dir()):
        for p in sorted((d / "sections").rglob("*.md")):
            rel = str(p.relative_to(base))
            lines = p.read_text(errors="replace").split("\n")
            for i, ln in enumerate(lines):
                if DOT_RUN.search(ln):
                    # fires on the endnote CONTEXT, not on the length: a long flattened index row
                    # with no endnote wording is the legitimate shape this class must not report.
                    if ENDNOTE_MARK.search(ln) or _endnote_context(lines, i):
                        add("C25_dot_leader_endnote", rel,
                            f"line {i + 1}: dot leaders in endnote context on a {len(ln)}-character "
                            f"line - endnote text flattened into the section: {ln.strip()[:60]!r}")
                if TOC_LEADER.search(ln):
                    add("C25_toc_leak", rel,
                        f"line {i + 1}: spaced dot leader ('. . . .') from a leaked table of "
                        f"contents: {ln.strip()[:60]!r}")


# ── C25 (E-f, second shape): the endnote appendix hidden by a newline split ─
# CDN-0222's rule keys on the one-line marker "Endnotes Endnote 1—About the endnotes". Three aml
# files carry the same compilation appendix with the two headings on separate lines, so the
# one-line rule saw none of them. Measured 2026-11-02: exactly 3 files corpus-wide
# (aml-ctf-2006 sch-1-1, aml-ctf-rules-2025 12-5, aml-ctf-transitional-rules-2026 21), all three
# repaired by scripts/fix_endnote_leak.py (6 files, that script's own dry run names them).
# The block is only a defect when law text precedes it and the tail footer follows it: a page whose
# whole body is the appendix is a different shape and is not reported here.
ENDNOTES_ONLY = re.compile(r"^[ \t]*#{0,6}[ \t]*Endnotes[ \t]*$")
ENDNOTE_ONE = re.compile(r"^[ \t]*#{0,6}[ \t]*Endnote 1[—–-]")
FOOTER_LINE = re.compile(r"^[ \t]*-{3}[ \t]*$")


def scan_endnote_appendix(root=None):
    base = Path(root) if root else DATA
    for p in sorted(base.glob("*/sections/**/*.md")):
        text = p.read_text(errors="replace")
        if "Endnotes" not in text:
            continue
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if not ENDNOTES_ONLY.match(ln):
                continue
            window = lines[i + 1:i + 5]
            if not any(ENDNOTE_ONE.match(x) for x in window):
                continue
            body = "\n".join(lines[:i]).strip()
            if len(body) < 200 or not any(FOOTER_LINE.match(x) for x in lines[i:]):
                continue
            add("C25_endnote_appendix", str(p.relative_to(base)),
                f"line {i + 1}: compilation endnote appendix inside the section body, marker "
                f"split across lines: {ln.strip()[:40]!r} then "
                f"{next(x for x in window if ENDNOTE_ONE.match(x)).strip()[:40]!r}")
            break


# ── C32 (2026-11-02): a section's compilation_no disagrees with its own act ──
# Nothing in the nightly audit read the per-file number: a planted flip of one itaa-1997 file from
# 267 to 274 was caught by no check at all (2026-11-02 planted test). The build-time guard
# (scripts/check_guard_vs_corpus.py) sees it, but only during a rebuild. Every section of an act
# carries the same number — measured 2026-11-02 over 32 acts, 0 mixed — so the act's modal number
# is the reference and needs no external source. An act whose number is 0 means "unknown"
# (nz-*, corporations-act-2001: 9 acts) and is skipped rather than invented.
FRONT_COMP = re.compile(r"^compilation_no:\s*\"?(\d+)", re.M)


def scan_file_compilation_no(root=None):
    base = Path(root) if root else DATA
    for act_dir in sorted(p for p in base.iterdir() if (p / "sections").is_dir()):
        numbers: dict[Path, str] = {}
        for p in sorted((act_dir / "sections").rglob("*.md")):
            head = p.read_text(errors="replace")[:1500]
            fm = re.match(r"^---\n(.*?)\n---", head, re.S)
            m = FRONT_COMP.search(fm.group(1)) if fm else None
            if m:
                numbers[p] = m.group(1)
        if not numbers:
            continue
        modal, _ = Counter(numbers.values()).most_common(1)[0]
        if modal == "0":
            continue
        for p, n in numbers.items():
            if n != modal:
                add("C32_compilation_outlier", str(p.relative_to(base)),
                    f"compilation_no {n} but every other section of {act_dir.name} says {modal}")


# ── C27 (S4): a section file no tree/index references ──────────────────────
# master-tax-examples carries 23 slug-length twins whose bodies are identical to a registered
# section: two slug generations were written in one regeneration (19068816f) and only one was
# registered. The general check is what catches the next generation, so it runs on every act
# with a tree/index - triage the orphans it finds, never bulk-delete them.
# Measured 2026-09-26: 23 findings, all in master-tax-examples (310 registered + 23 unregistered
# = 333 files) - the plan's exact figure. No other act has an orphan.
def tree_section_refs(act_dir: Path) -> set[str]:
    """Every path, file name and id an act's tree.json / section_index.json references."""
    refs: set[str] = set()
    tree = act_dir / "tree.json"
    if tree.exists():
        try:
            t = json.loads(tree.read_text())
        except Exception:
            t = None

        def walk(node):
            if isinstance(node, dict):
                for s in node.get("sections", []):
                    if s.get("path"):
                        refs.add(str(s["path"]))
                    if s.get("id") is not None:
                        refs.add(str(s["id"]))
                for k in ("parts", "divisions", "subdivisions"):
                    for c in node.get(k, []):
                        walk(c)
        if t is not None:
            walk(t)
    idx = act_dir / "section_index.json"
    if idx.exists():
        try:
            entries = json.loads(idx.read_text())
        except Exception:
            entries = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                for k in ("path", "id"):
                    if e.get(k):
                        refs.add(str(e[k]))
    return refs


def scan_unregistered_sections(root=None):
    base = Path(root) if root else DATA
    for d in sorted(x for x in base.iterdir() if (x / "sections").is_dir()):
        refs = tree_section_refs(d)
        if not refs:
            add("C27_unregistered_section:CONVENTION", f"data/{d.name}/sections",
                "no tree.json / section_index.json references to check against - "
                "check skipped, not a pass")
            continue
        for p in sorted((d / "sections").rglob("*.md")):
            rel = str(p.relative_to(d / "sections"))
            if rel in refs or p.stem in refs or p.name in refs:
                continue
            add("C27_unregistered_section", f"{d.name}/sections/{rel}",
                "no tree.json / section_index.json entry references this file - orphan "
                "(triage: a duplicate of a registered twin, or a real section missing from "
                "the tree)")


# ── shared detector registry (S3, 2026-09-26) ───────────────────────────────
# ONE list. main() iterates it, and scripts/randomised_api_mcp_test.py (the nightly's content
# phase) imports THIS list instead of keeping its own copy. Before this the two had already
# drifted in both directions: the nightly's hand-kept 15 names included scan_body_fragments,
# which main() never called, and omitted scan_drawing_characters (C15) and
# scan_page_rule_artifacts (C16), both of which main() did call - so C15/C16 were dark at
# night and C5_body_fragments was dark in the standalone scanner. Every detector added below
# is registered here; nothing is called from main() that is not in this list.
# ── C28/C29 (nightly audit 2026-10-27): item identifiers ─────────────────────
# The rotation planted (b)->(q) and nothing saw it: none of the 24 detectors compared an item
# marker with its predecessor, and verify_data_integrity's artifact scan returned [] for the
# planted file. A publisher repeal gap and a corrupted label have the same signature when you
# only see the numbers (aml-ctf 245 (6)->(9) and bankruptcy 122 (5)->(7) are real gaps, traced
# to the source docx), so the two cases are split:
#   C28_item_order  a label that does not sit after its predecessor inside one run - cannot be
#                   a gap, additive or duplicate numbering; this is the corruption class.
#   C29_item_gap    a forward jump of more than one - ambiguous by nature (publisher repeal OR
#                   items lost in extraction), reported for source verification, not as damage.
# A run survives blank lines and body text (resetting on those made the audit's own first
# heuristic ~3x insensitive) and resets on a heading, which legitimately starts a new list.
# Roman numerals are skipped: (i)(v)(x) inside an alphabetical run are not sequence items.
ITEM_MARKER = re.compile(r"^\(([a-zA-Z]{1,2}|\d{1,3})\)")
# ITAA renders subsections and paragraphs bold (`**(2)**`, `**(a)**`, usually inside a blockquote)
# and the plain ITEM_MARKER missed every one of them: the run state then stayed parked on the last
# unwrapped label, so the next real (b) looked like a duplicate of a (b) from fifty lines earlier.
# Measured on itaa-1997 alone: 66 findings, this shape is most of them.
ITEM_MARKER_BOLD = re.compile(r"^\*\*\(([a-zA-Z]{1,2}|\d{1,3})\)")

# A paragraph that introduces the list which follows it: "…are the following:", "as follows."
# Trailing-colon chapeaus are matched separately; this catches the phrasal ones.
_CHAPEAU = re.compile(
    r"\b(?:the following|as follows|follows:?|are these|is as set out|"
    r"set out in this (?:section|subsection|table))\s*[:.]?$",
    re.I,
)
ITEM_HEADING = re.compile(r"^#{1,6}\s")
# A definition line ("R means—", "W means the amount of wages …") opens its own list and ends the
# one before it.  Qld Payroll Tax 1971 s 27A reads "the lesser of the following amounts— (a) the
# amount worked out using the following formula— where— R means— (a) …; (b) otherwise—4. T means …
# W means …; (b) the employer's … liability": the inner (a)/(b) belong to R, not to the outer
# rebate list, but the extractor has no indent to prove that, so the outer (b) was reported as a
# duplicate of R's (b).  Measured 2026-11-12: 3 files (qld-payrolltax 27A, 35A, 43A), all faithful
# to the source.  A definition line therefore resets the run exactly as a chapeau does.
DEFINITION_LINE = re.compile(
    r"^(?:>\s*)*(?:\*\*)?[A-Z][A-Za-z0-9 ]{0,24}(?:\*\*)?\s+means\b"
)
ROMAN_ITEMS = {"i", "v", "x", "ii", "iv", "vi", "ix", "xi", "xii"}


def _item_marker(line: str):
    """Item label of a prose line, or None.

    Blockquote markers are peeled first (`> **(a)** ...`), then a bold-wrapped label. Fenced blocks
    are handled by the caller: item letters inside a grid table are table cells, not a list.
    """
    s = line.strip()
    while s.startswith(">"):
        s = s[1:].strip()
    m = ITEM_MARKER.match(s) or ITEM_MARKER_BOLD.match(s)
    return m.group(1) if m else None



def _item_ordinal(marker: str):
    """Key that sorts an item label the way the Act prints it.

    Two-letter markers are sub-items of their first letter: (d) < (da) < (db) < (e). Treating
    (da) as the ordinal 105 made every (d)(da)(e) run look out of order.
    """
    marker = marker.lower()
    if marker.isdigit():
        return (int(marker), "")
    return (ord(marker[0]) - 96, marker[1:])


FIRST_LABEL = {0: "1", 1: "a", 2: "i", 3: "A"}


def _marker_depth(raw: str) -> int:
    """Nesting level of an item label, case preserved.

    Commonwealth drafting nests four deep: subsection (6) > paragraph (a) > subparagraph (i) >
    sub-subparagraph (A), and an inserted paragraph prints as (ba) / an inserted sub-subparagraph
    as (BAA). Folding case (which this detector used to do) merged all four levels into one run,
    so every return to an outer level read as a backward move: 571 C28 + 512 C29 findings on the
    live corpus, almost all false. Uppercase and digits are the outer kinds; a lowercase two-letter
    label is an inserted paragraph (depth 1) unless it is a roman numeral.
    """
    if raw.isdigit():
        return 0
    if raw.isupper():
        return 3
    if len(raw) == 1:
        return 1
    return 2 if raw in ROMAN_ITEMS else 1


def _is_gap(val, prev) -> bool:
    if val[0] > prev[0] + 1:
        return True
    if val[0] == prev[0] and val[1] and prev[1]:
        return (ord(val[1][0]) - 96) > (ord(prev[1][0]) - 96) + 1
    return False


def _consecutive(a, b) -> bool:
    """True when label b comes immediately after label a in the same run."""
    if a is None or b is None:
        return False
    return b > a and not _is_gap(b, a)


def _successor(raw: str) -> str:
    """The label that should have followed raw, for the finding text."""
    if raw.isdigit():
        return str(int(raw) + 1)
    if len(raw) == 1 and raw.isalpha():
        return chr(ord(raw) + 1)
    return raw + "?"


def _label(marker: str) -> str:
    return marker


def _inserted_variant(raw: str, prev_raw: str | None) -> bool:
    """Is `raw` an amendment insert on `prev_raw` rather than a new item?

    OPC adds a paragraph to an existing list by suffixing the label: (a) then (ia), (ib); a
    duplicated letter ((c) then (cc)) is the same move.  Both keep the run in place - the next
    plain label continues from the item that was inserted into, not from the insert.
    """
    if not prev_raw or not raw or raw == prev_raw:
        return False
    if len(raw) <= len(prev_raw):
        return False
    if raw.startswith(prev_raw):
        return len(raw) - len(prev_raw) <= 2      # (c) -> (cc), (ca)
    # (a) -> (ia): a single roman prefix in front of the label it extends
    return raw[0] == "i" and raw[1:] == prev_raw


def scan_item_sequence(check_root=None):
    """Track each nesting depth as its own run.

    A numbered item starts a new lettered list, so (1) (a) (b) (2) (a) is not out of order; every
    depth therefore keeps its own state, and a marker at depth k clears all deeper levels because
    those belonged to the item that just ended. A heading clears every level.

    Subparagraphs (roman) hold a slot but are never reported: (i) (v) inside a lettered run is not
    a sequence break, and roman ordinals would need their own converter to be compared at all.

    A roman sub-list sits INSIDE the item that contains it - (a) (i) (ii) (b) - but it wipes the
    lettered state, so a duplicate directly after one had no previous ordinal to compare with and
    was downgraded to advisory. `hist` keeps the recent NON-ROMAN ordinals per depth for exactly
    that comparison. Measured 2026-11-03: a planted (c)->(b) in adjr-act-1977 s11, sitting after a
    (i)/(ii) pair, scored advisory and looked like a detector that does not fire.
    """
    base = Path(check_root) if check_root else DATA
    for act_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        sec_dir = act_dir / "sections"
        if not sec_dir.is_dir():
            continue
        for p in sorted(sec_dir.rglob("*.md")):
            rel = str(p.relative_to(base))
            state: dict[int, tuple] = {}  # depth -> (ordinal, raw as printed, previous)
            pending: dict[int, tuple] = {}  # depth -> a forward jump awaiting the next label
            hist: dict[int, list] = {}  # depth -> recent non-roman (ordinal, raw), newest last
            in_fence = False
            for lineno, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if line.lstrip().startswith("```"):
                    in_fence = not in_fence
                    state.clear()  # a table is not a continuation of the prose list
                    hist.clear()
                    continue
                if in_fence:
                    continue
                if ITEM_HEADING.match(line):
                    state.clear()
                    hist.clear()
                    continue
                raw = _item_marker(line)
                if raw is None:
                    # A chapeau opens a new list.  Published Acts routinely put a second
                    # lettered list ("The following are exempt: (a) …") after a block of body
                    # text with no numbered parent between them, so keeping the old run state
                    # reported the new list's (a) as a restart and every gap after it as a lost
                    # item.  Nightly audit 2026-10-29: 1,303 C29 gaps and 706 C28 out-of-order
                    # rows, 12 of 12 sampled C28 and both sampled C29 refuted against the source.
                    # Only a paragraph that INTRODUCES a list clears the state, and only after a
                    # blank line, so an ordinary sentence between items leaves the run intact.
                    stripped = line.strip()
                    if stripped:
                        chapeau = stripped.endswith(":") or _CHAPEAU.search(stripped)
                        if chapeau:
                            state.clear()
                            hist.clear()
                        elif DEFINITION_LINE.match(stripped):
                            # "R means—" ends the list before it and starts a fresh one.
                            state.clear()
                            hist.clear()
                    continue
                if raw.lower() in ROMAN_ITEMS:
                    state[_marker_depth(raw)] = (None, None, None)
                    continue
                depth = _marker_depth(raw)
                val = _item_ordinal(raw)
                for d in [d for d in state if d > depth]:
                    del state[d]  # deeper levels belonged to the item that just ended
                prev, prev_raw, prev_prev = state.get(depth, (None, None, None))

                # A forward jump is only damage if the run then comes BACK to where it was:
                # (b) (q) (c) means the q is a corrupted label, because a real list never
                # returns.  A jump that simply carries on is either a publisher repeal gap or
                # lost items, which the corpus alone cannot distinguish - so the decision waits
                # for the next label instead of being guessed at the jump.
                pend = pending.pop(depth, None)
                if pend is not None:
                    p_ln, p_raw, p_val, p_prev_val, p_prev_raw = pend
                    # (ia)/(ib) are how OPC inserts a paragraph into a roman sub-list:
                    # (i) (ia) (ii) under one lettered item. Roman runs are not tracked
                    # (ROMAN_ITEMS), so the insert surfaces as a jump from the lettered
                    # item, and the next letter then looks like a return to it. Measured
                    # 2026-11-06: 4 such rows, all four verified against the corpus text
                    # as (i),(ia),(ii) drafting (fbt 9, fbt 24, corporations 946B,
                    # wa-duties 266F) - so the insert is carried, not filed.
                    if p_raw.lower() in ("ia", "ib"):
                        prev, prev_raw = p_prev_val, p_prev_raw
                    elif _consecutive(p_prev_val, val):
                        add("C28_item_order", rel,
                            f"line {p_ln}: ({p_raw}) does not belong in the run - the next item "
                            f"({raw}) continues from ({p_prev_raw}); expected "
                            f"({_successor(p_prev_raw)})")
                        prev, prev_raw = p_prev_val, p_prev_raw
                    else:
                        add("C31_item_order_advisory", rel,
                            f"line {p_ln}: ({p_raw}) jumps from ({p_prev_raw}) - publisher repeal "
                            f"gap OR items lost in extraction, verify against the source")
                        prev, prev_raw = p_val, p_raw

                if prev is not None and _inserted_variant(raw, prev_raw):
                    # OPC inserts a paragraph by amendment as a suffix on the existing label:
                    # (a) (ia) (b).  The insert belongs to the item it extends, so the run stays
                    # where it was and the following (b) is still consecutive with (a).
                    # Measured 2026-11-02: 7 of the 29 "does not belong in the run" rows were
                    # (ia) inserts - legitimate drafting reported as lost labels.
                    state[depth] = (prev, prev_raw, prev_prev)
                    continue

                if prev is not None:
                    # A fresh list restarting at the first label of its own depth is legal drafting,
                    # not damage: (b)(c) then a new unnumbered chapeau and (a) again.
                    restart = raw.lower() == FIRST_LABEL.get(depth, "").lower()
                    if val == prev:
                        # A repeat is only a defect when the run around it is otherwise
                        # consecutive: (e) (f) (f) (g) lost a label.  When the labels either
                        # side are not consecutive the file is a flattened multi-list page
                        # (aml-ctf-rules-2007 63.6 has three separate lettered lists merged into
                        # one section), which is a structure defect, not a label defect - and
                        # reporting it here buried the two genuine duplicates in 119 rows.
                        # Compare against the previous NON-ROMAN ordinal: a (i)/(ii) sub-list
                        # between (a) and the duplicate erases state[depth], and the old test
                        # then saw prev_prev as None and called a real duplicate advisory.
                        h = hist.get(depth) or []
                        ascending = (len(h) >= 2 and h[-2][0] is not None
                                     and h[-1][0] is not None
                                     and _consecutive(h[-2][0], h[-1][0]))
                        if ascending:
                            add("C28_item_order", rel,
                                f"line {lineno}: ({raw}) repeats ({prev_raw}) - duplicate item label")
                        else:
                            add("C31_item_order_advisory", rel,
                                f"line {lineno}: ({raw}) repeats ({prev_raw}) but the surrounding "
                                f"labels are not consecutive - flattened multi-list page, verify")
                    elif val < prev and not restart:
                        # Same leading label with a shorter suffix ((BAA) then (BA)) is an inserted
                        # sub-item printed out of lexical order, not a corrupted label.  Order is
                        # also advisory: the audit's own sample of 12 out-of-order rows was
                        # refuted 12/12 against the source, because list boundaries do not
                        # survive the extraction.  Only a repeat inside a consecutive run is a
                        # defect the corpus alone can prove.
                        if val[0] < prev[0]:
                            add("C31_item_order_advisory", rel,
                                f"line {lineno}: ({raw}) follows ({prev_raw}) - out of order; "
                                f"list boundaries do not survive extraction, verify against source")
                    elif val > prev and _is_gap(val, prev):
                        pending[depth] = (lineno, raw, val, prev, prev_raw)
                state[depth] = (val, raw, prev)
                hv = hist.setdefault(depth, [])
                hv.append((val, raw))
                del hv[:-3]
            # A jump that nothing follows cannot be resolved either way: report it as advice
            # rather than dropping it, because silence would hide a genuinely lost item.
            for p_ln, p_raw, _p_val, p_prev_val, p_prev_raw in pending.values():
                add("C31_item_order_advisory", rel,
                    f"line {p_ln}: ({p_raw}) jumps from ({p_prev_raw}) and no later label "
                    f"resumes the run - publisher repeal gap OR items lost, verify")


# ── C30 (E-g): a lettered marker OCR'd as a digit (l->1, o->0) ──────────────
# The nightly audit's silent class.  A lettered run (a)…(k)…(m) with a bare "(1)" in the middle
# is a corrupted "(l)"; the run state saw a restart at the first label of that depth, which the
# restart whitelist exists to allow, so it never reported the corruption.  Measured 2026-10-29:
# requiring (i) the digit to carry the same bold style as its neighbours and (ii) a consecutive
# lettered run of at least four to sit inside leaves 4 findings, all genuine and all in the two
# NZ Acts (nz-ita-1976 58/106/243, nz-ita-1994 DZ-2).  Without those two guards the rule fires
# 23 times and every extra hit is a legitimate "(3)" subsection starting after a lettered list.
CONFUSION_ITEM = re.compile(r"^(?P<b1>\*\*)?\((?P<lab>\d{1,2}|[a-z]{1,2})\)(?P<b2>\*\*)?")


def scan_letter_digit_markers(check_root=None):
    base = Path(check_root) if check_root else DATA
    for p in sorted(base.glob("*/sections/**/*.md")):
        try:
            lines = p.read_text(errors="replace").split("\n")
        except OSError:
            continue
        rows = []
        for i, line in enumerate(lines, 1):
            m = CONFUSION_ITEM.match(line)
            if m:
                # "(2) (f) of this Act" is a cross-reference that happens to start a wrapped
                # line, not a list label; a real marker is not immediately followed by another
                # parenthesised token.  Without this guard nz-ita-1976 s 106 line 95 - a
                # reference to s 65(2)(f) - was reported as a corrupted (l) forever.
                if re.match(r"\s*\([0-9a-zA-Z]{1,3}\)", line[m.end():]):
                    continue
                bold = bool(m.group("b1") and m.group("b2"))
                rows.append((i, m.group("lab"), bold, line.strip()[:70]))
        run: list[tuple[int, str, bool]] = []
        for idx, (ln, lab, bold, raw) in enumerate(rows):
            if lab.isalpha() and len(lab) == 1:
                run.append((idx, lab, bold))
                continue
            if not lab.isdigit():
                continue
            prev = run[-1] if run else None
            nxt = next(((rows[j][1], rows[j][2]) for j in range(idx + 1, len(rows))
                        if rows[j][1].isalpha()), None)
            if not prev or not nxt:
                continue
            if not (prev[2] == bold == nxt[1]) or len(nxt[0]) != 1:
                continue
            if ord(nxt[0]) - ord(prev[1]) != 2:
                continue
            run_len = 1
            for k in range(len(run) - 1, 0, -1):
                consecutive = ord(run[k][1]) - ord(run[k - 1][1]) == 1
                if consecutive and run[k][0] - run[k - 1][0] <= 6:
                    run_len += 1
                else:
                    break
            if run_len < 4:
                continue
            want = chr(ord(prev[1]) + 1)
            add("C30_letter_digit_marker", str(p.relative_to(base)),
                f"line {ln}: ({lab}) sits inside a lettered run ({prev[1]})-({lab})-({nxt[0]}); "
                f"expected ({want}): {raw!r}")


# Advisory = reported, never counted as a defect, because the corpus alone cannot show damage.
# C29/C31: a gap or jump in a published run is usually the publisher's own repeal.
# C14: measured 2026-11-02 - itaa-1997's own PDF text layer reproduces the flat three-term block
# ("Days held / Asset's cost / 365 / 100% / Asset's effective life"); the fraction bar is a DRAWN
# line, absent from the publisher's text, so the corpus is faithful and has lost nothing. A
# formula captured flat is a structured-formula gap, not corpus damage. C14_fraction_rule is the
# same fence seen from the other side (a rule survived) and is likewise informational.
ADVISORY_CLASSES = {"C29_item_gap", "C31_item_order_advisory",
                    "C14_formula_structure", "C14_fraction_rule"}

DETECTORS = [
    "scan_compilation_no",
    "scan_empty_tree_nodes",
    "scan_tree_titles",
    "scan_asterisk_noise",
    "scan_section_fragments",
    "scan_body_fragments",
    "scan_stray_cut_tokens",
    "scan_chapeau",
    "scan_missing_chapeau",
    "scan_nz_amendment_parts",
    "scan_ruling_title_fragments",
    "scan_formatting_artifacts",
    "scan_definitions",
    "scan_case_citations",
    "scan_table_coherence",
    "scan_missing_heading",
    "scan_duplicate_anchors",
    "scan_lost_formula_structure",
    "scan_drawing_characters",
    "scan_page_rule_artifacts",
    "scan_character_classes",
    "scan_glued_headings",
    "scan_dot_leaders",
    "scan_endnote_appendix",
    "scan_file_compilation_no",
    "scan_unregistered_sections",
    "scan_item_sequence",
    "scan_letter_digit_markers",
]


def main():
    # --write-c31-baseline: freeze the CURRENT advisory set (including everything already frozen,
    # so a regeneration never un-suppresses an old row). Run the detector with the baseline
    # ignored, then write every key it produces.
    if "--write-c31-baseline" in sys.argv:
        global _c31_ignore_baseline
        _c31_ignore_baseline = True
        findings.clear()
        scan_item_sequence()
        keys = sorted({k for f in findings
                       if f["class"] == "C31_item_order_advisory"
                       and (k := _c31_key(f["path"], f["detail"]))})
        C31_BASELINE.write_text(json.dumps(keys, indent=1) + "\n", encoding="utf-8")
        print(f"C31 baseline: {len(keys)} keys written to {C31_BASELINE}")
        return

    for name in DETECTORS:
        globals()[name]()

    # summarize: findings, plus the occurrence count for classes that aggregate characters
    by_class = Counter(f["class"] for f in findings)
    occurrences = Counter()
    for f in findings:
        occurrences[f["class"]] += int(f.get("count", 1))
    # C29 is advisory by design: a gap in a published lettered or numbered run is usually the
    # publisher's own repeal (gst-1999's missing (b) is absent from the printed volume, fbt-1986
    # 123B jumps 4->6 in the source's own anchors).  Counting it with the defects inflated the
    # nightly total by 1,303 rows that no fix could ever clear, so it is reported apart.
    advisory = sum(cnt for cls, cnt in by_class.items() if cls in ADVISORY_CLASSES)
    defects = len(findings) - advisory
    print(f"TOTAL FINDINGS: {len(findings)}  (defects {defects}, advisory {advisory})")
    for cls, cnt in sorted(by_class.items()):
        occ = occurrences[cls]
        extra = f" ({occ} occurrence(s))" if occ != cnt else ""
        tag = "  [advisory]" if cls in ADVISORY_CLASSES else ""
        print(f"  {cls}: {cnt}{extra}{tag}")
    print()
    # print a bounded sample per class for context - the full list goes to the JSON. A class
    # like C22_ligature has ~1,300 findings and printing them all buried the rest of the report;
    # the samples are grouped by class so the report reads class by class.
    PER_CLASS_SAMPLE = 20
    for cls in sorted(by_class):
        print(f"--- {cls} ---")
        for f in [x for x in findings if x["class"] == cls][:PER_CLASS_SAMPLE]:
            print(f"  {f['path']}: {f['detail'][:100]}")
        if by_class[cls] > PER_CLASS_SAMPLE:
            print(f"  ... {by_class[cls] - PER_CLASS_SAMPLE} more in the JSON below")

    # A fixed, undated filename: this used to be corpus_scan_20260826.json and was OVERWRITTEN on
    # every run, so a nightly reading the name concluded it held a 10-week-old scan and reported
    # suppressed-then-repaired findings as "cannot reproduce" (2026-11-03). Print the run time and
    # the count next to the path so freshness is readable, never inferred from the name.
    out = Path("/tmp/corpus_scan_latest.json")
    payload = json.dumps(findings, indent=2)
    out.write_text(payload)
    # legacy consumers (verify_v1_buckets, scan_truncation_scope) still read the old path
    Path("/tmp/corpus_scan_20260826.json").write_text(payload)
    print(f"\nfull JSON: {out}  ({len(findings)} findings, written "
          f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")


if __name__ == "__main__":
    main()
