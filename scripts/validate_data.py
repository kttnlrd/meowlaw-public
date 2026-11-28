"""Validate the data/ directory for integrity issues.

HARD FAILURES (exit code 1):
  - Tree paths that point to nonexistent markdown files.
  - definitions_all.json missing, unparseable, or structurally invalid.
  - Term entries in definitions_all.json whose section .md file cannot be found.

WARNINGS (printed; do not affect exit code):
  - Duplicate (act, section_id) pairs within a single tree.json.
  - Term anchors that do not appear in the matching section markdown.
  - Acts with a tree.json but no entry in definitions_all.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def collect_tree_paths(tree: dict) -> list[dict]:
    """Return list of {"id": ..., "path": ...} for every section in a tree."""
    entries: list[dict] = []
    for part in tree.get("parts", []):
        for sec in part.get("sections", []):
            entries.append(sec)
        for div in part.get("divisions", []):
            for sec in div.get("sections", []):
                entries.append(sec)
            for subdiv in div.get("subdivisions", []):
                for sec in subdiv.get("sections", []):
                    entries.append(sec)
    return entries


def build_stem_index(sections_dir: Path) -> dict[str, Path]:
    """Map lowercase filename stem -> first matching .md path under sections_dir."""
    index: dict[str, Path] = {}
    for md in sections_dir.rglob("*.md"):
        stem = md.stem.lower()
        if stem not in index:
            index[stem] = md
    return index


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_tree_paths(data_dir: Path, hard_failures: list[str], warnings: list[str]) -> None:
    """Check every tree.json section path resolves to a real file."""
    print("\n--- Tree path integrity ---")
    for act_dir in sorted(data_dir.iterdir()):
        tree_file = act_dir / "tree.json"
        if not act_dir.is_dir() or not tree_file.exists():
            continue
        act = act_dir.name
        sections_root = act_dir / "sections"

        try:
            tree = json.loads(tree_file.read_text(encoding="utf-8"))
        except Exception as e:
            hard_failures.append(f"{act}: tree.json parse error: {e}")
            continue

        entries = collect_tree_paths(tree)
        total = len(entries)
        missing: list[str] = []
        for entry in entries:
            path = entry.get("path", "")
            if not path:
                continue
            if not (sections_root / path).exists():
                missing.append(path)

        status = "OK" if not missing else f"FAIL — {len(missing)} of {total} paths missing"
        print(f"  {act}: {total} sections, {status}")
        if missing:
            for p in missing[:10]:
                print(f"    MISSING: {p}")
            if len(missing) > 10:
                print(f"    ... and {len(missing) - 10} more")
            hard_failures.append(
                f"{act}: {len(missing)}/{total} tree paths point to nonexistent files"
            )
        else:
            pass  # already printed OK


def check_definitions_all(data_dir: Path, hard_failures: list[str], warnings: list[str]) -> dict:
    """Validate definitions_all.json existence, parse, and per-term section file presence."""
    print("\n--- definitions_all.json ---")
    defs_file = data_dir / "definitions_all.json"

    if not defs_file.exists():
        hard_failures.append("definitions_all.json does not exist")
        print("  FAIL: file not found")
        return {}

    try:
        defs_all = json.loads(defs_file.read_text(encoding="utf-8"))
    except Exception as e:
        hard_failures.append(f"definitions_all.json parse error: {e}")
        print(f"  FAIL: parse error: {e}")
        return {}

    print(f"  Parsed OK — acts: {list(defs_all.keys())}")

    for act, act_data in defs_all.items():
        if not isinstance(act_data, dict):
            hard_failures.append(f"definitions_all.json[{act}]: expected dict, got {type(act_data).__name__}")
            continue

        terms = act_data.get("terms")
        if not terms:
            hard_failures.append(f"definitions_all.json[{act}]: 'terms' is missing or empty")
            print(f"  FAIL [{act}]: terms missing or empty")
            continue

        # Validate individual term entries
        bad_anchor = []
        bad_section = []
        for term, entry in terms.items():
            if not isinstance(entry, dict):
                bad_anchor.append(term)
                bad_section.append(term)
                continue
            if not entry.get("anchor"):
                bad_anchor.append(term)
            if not entry.get("section"):
                bad_section.append(term)

        if bad_anchor:
            hard_failures.append(
                f"definitions_all.json[{act}]: {len(bad_anchor)} terms missing 'anchor' field"
            )
            print(f"  FAIL [{act}]: {len(bad_anchor)} terms missing 'anchor'")
        if bad_section:
            hard_failures.append(
                f"definitions_all.json[{act}]: {len(bad_section)} terms missing 'section' field"
            )
            print(f"  FAIL [{act}]: {len(bad_section)} terms missing 'section'")

        if not bad_anchor and not bad_section:
            print(f"  OK   [{act}]: {len(terms)} terms, all entries have anchor+section")

    return defs_all


def check_definition_section_files(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """For every term, confirm its section .md file exists under data/{act}/sections/."""
    print("\n--- Definition section file presence ---")
    if not defs_all:
        print("  SKIP: no definitions to check")
        return

    for act, act_data in defs_all.items():
        if not isinstance(act_data, dict):
            continue
        terms = act_data.get("terms")
        if not terms:
            continue

        sections_dir = data_dir / act / "sections"
        if not sections_dir.exists():
            hard_failures.append(f"{act}: sections/ directory not found (cannot check definition files)")
            print(f"  FAIL [{act}]: sections/ directory missing")
            continue

        # Build stem->path index once per act
        stem_index = build_stem_index(sections_dir)

        missing_files: list[tuple[str, str]] = []
        for term, entry in terms.items():
            if not isinstance(entry, dict):
                continue
            section_id = entry.get("section", "")
            if not section_id:
                continue
            stem = section_id.lower()
            if stem not in stem_index:
                missing_files.append((term, section_id))

        if missing_files:
            print(f"  FAIL [{act}]: {len(missing_files)} terms reference nonexistent section files")
            for term, sec in missing_files[:10]:
                print(f"    MISSING: section={sec!r}  term={term!r}")
            if len(missing_files) > 10:
                print(f"    ... and {len(missing_files) - 10} more")
            hard_failures.append(
                f"{act}: {len(missing_files)} definition terms reference nonexistent section files"
            )
        else:
            print(f"  OK   [{act}]: all {len(terms)} terms' section files found")


def check_junk_keys(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """HARD FAILURE: flag definitions_all.json keys that are PDF/parse artefacts.

    Patterns:
      - key starts with '('
      - key length > 80
      - key ends with ' means', ' includes', or ' has'
      - key contains '. ' or '; '
      - key is one of a small set of bare stop-words
    """
    print("\n--- Junk-key lint (definitions_all.json terms) ---")
    if not defs_all:
        print("  SKIP: no definitions to check")
        return

    STOP_WORDS_EXACT = {"by", "is", "a", "an", "the", "of", "in", "to", "and", "or"}

    total_junk = 0
    for act, act_data in defs_all.items():
        if not isinstance(act_data, dict):
            continue
        terms = act_data.get("terms")
        if not terms:
            continue

        offenders: list[tuple[str, str]] = []
        for key in terms:
            reason: str | None = None
            if key.startswith("("):
                reason = "starts with '('"
            elif len(key) > 80:
                reason = f"length {len(key)} > 80"
            elif key.endswith(" means") or key.endswith(" includes") or key.endswith(" has"):
                reason = "ends with predicate word"
            elif ". " in key:
                reason = "contains '. '"
            elif "; " in key:
                reason = "contains '; '"
            elif key in STOP_WORDS_EXACT:
                reason = "bare stop-word"
            if reason:
                offenders.append((key, reason))

        if offenders:
            total_junk += len(offenders)
            print(
                f"  FAIL [{act}]: {len(offenders)} junk key(s) — "
                f"showing up to 10:"
            )
            for key, reason in offenders[:10]:
                print(f"    {reason}: {key!r}")
            hard_failures.append(
                f"{act}: {len(offenders)} junk term keys in definitions_all.json"
            )
        else:
            print(f"  OK   [{act}]: no junk keys ({len(terms)} terms)")

    if total_junk == 0:
        print("  All acts clean.")


def check_anchor_uniqueness(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """HARD FAILURE: anchor values must be unique within each act."""
    print("\n--- Anchor uniqueness within each act (definitions_all.json) ---")
    if not defs_all:
        print("  SKIP: no definitions to check")
        return

    for act, act_data in defs_all.items():
        if not isinstance(act_data, dict):
            continue
        terms = act_data.get("terms")
        if not terms:
            continue

        seen: dict[str, str] = {}          # anchor -> first term key
        duplicates: list[tuple[str, str, str]] = []  # (anchor, key1, key2)

        for key, entry in terms.items():
            if not isinstance(entry, dict):
                continue
            anchor = entry.get("anchor", "")
            if not anchor:
                continue
            if anchor in seen:
                duplicates.append((anchor, seen[anchor], key))
            else:
                seen[anchor] = key

        if duplicates:
            print(f"  FAIL [{act}]: {len(duplicates)} duplicate anchor(s) — showing up to 10:")
            for anchor, k1, k2 in duplicates[:10]:
                print(f"    {anchor!r}: shared by {k1!r} and {k2!r}")
            hard_failures.append(
                f"{act}: {len(duplicates)} duplicate anchors in definitions_all.json"
            )
        else:
            print(f"  OK   [{act}]: all {len(seen)} anchors unique")


def check_definition_findability(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """WARNING: term keys must be findable (case-insensitive substring) in the
    dictionary markdown file for each act.  Regression tripwire for parser run-on
    fix.  Warns if findability drops below 85%.

    Dictionary section file per act:
      itaa-1997  sections/part-6-5/division-995/995-1.md
      itaa-1936  sections/part-i/division-unknown/6.md
      gst-1999   sections/part-6-3/division-195/195-1.md
    """
    print("\n--- Definition-site findability in dictionary markdown (warnings if < 85%) ---")
    if not defs_all:
        print("  SKIP: no definitions to check")
        return

    DICT_FILES: dict[str, Path] = {
        "itaa-1997": data_dir / "itaa-1997" / "sections" / "part-6-5" / "division-995" / "995-1.md",
        "itaa-1936": data_dir / "itaa-1936" / "sections" / "part-i" / "division-unknown" / "6.md",
        "gst-1999":  data_dir / "gst-1999"  / "sections" / "part-6-3" / "division-195"  / "195-1.md",
    }

    THRESHOLD = 85.0

    for act, md_path in DICT_FILES.items():
        act_data = defs_all.get(act)
        if not act_data:
            print(f"  SKIP [{act}]: not in definitions_all.json")
            continue
        terms = act_data.get("terms")
        if not terms:
            print(f"  SKIP [{act}]: no terms")
            continue
        if not md_path.exists():
            warnings.append(f"{act}: dictionary markdown not found at {md_path.relative_to(data_dir)}")
            print(f"  WARNING [{act}]: dictionary markdown not found — {md_path}")
            continue

        # Strip '*' from file content before searching (terms may carry bold markers).
        content = md_path.read_text(encoding="utf-8").replace("*", "").lower()

        not_found: list[str] = []
        for key in terms:
            if key.lower() not in content:
                not_found.append(key)

        total = len(terms)
        found = total - len(not_found)
        pct = 100.0 * found / total if total else 0.0

        status = "OK" if pct >= THRESHOLD else "WARN"
        print(
            f"  {status} [{act}]: {found}/{total} keys found in dictionary markdown "
            f"({pct:.1f}%)"
        )
        if not_found:
            print(f"    Sample not found: {not_found[:5]}")
        if pct < THRESHOLD:
            warnings.append(
                f"{act}: only {pct:.1f}% of term keys found in dictionary markdown "
                f"(threshold {THRESHOLD}%) — parser run-on regression?"
            )


def check_duplicate_section_ids(
    data_dir: Path, hard_failures: list[str], warnings: list[str]
) -> None:
    """Warn on duplicate section IDs within a single tree.json."""
    print("\n--- Duplicate section IDs in tree.json (warnings) ---")
    for act_dir in sorted(data_dir.iterdir()):
        tree_file = act_dir / "tree.json"
        if not act_dir.is_dir() or not tree_file.exists():
            continue
        act = act_dir.name

        try:
            tree = json.loads(tree_file.read_text(encoding="utf-8"))
        except Exception:
            continue  # already reported above

        entries = collect_tree_paths(tree)
        counts: dict[str, int] = defaultdict(int)
        for entry in entries:
            sec_id = entry.get("id", "")
            if sec_id:
                counts[sec_id] += 1

        dupes = {sid: cnt for sid, cnt in counts.items() if cnt > 1}
        if dupes:
            total_dupes = sum(cnt - 1 for cnt in dupes.values())
            top = sorted(dupes.items(), key=lambda x: -x[1])[:10]
            warnings.append(
                f"{act}: {len(dupes)} duplicate section IDs ({total_dupes} extra occurrences)"
            )
            print(f"  WARNING [{act}]: {len(dupes)} duplicate IDs, top entries:")
            for sid, cnt in top:
                print(f"    {sid!r}: appears {cnt}x")
        else:
            print(f"  OK   [{act}]: no duplicates")


def check_anchor_in_markdown(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """Warn when a term anchor does not appear as '<a id="..."' in its section markdown."""
    print("\n--- Term anchor presence in markdown (warnings; known gap ~0%) ---")
    if not defs_all:
        print("  SKIP: no definitions to check")
        return

    ANCHOR_RE = re.compile(r'<a\s+id=["\']([^"\']+)["\']', re.IGNORECASE)

    for act, act_data in defs_all.items():
        if not isinstance(act_data, dict):
            continue
        terms = act_data.get("terms")
        if not terms:
            continue

        sections_dir = data_dir / act / "sections"
        if not sections_dir.exists():
            continue

        stem_index = build_stem_index(sections_dir)

        total = 0
        found = 0
        missing_anchor: list[tuple[str, str, str]] = []

        for term, entry in terms.items():
            if not isinstance(entry, dict):
                continue
            section_id = entry.get("section", "")
            anchor = entry.get("anchor", "")
            if not section_id or not anchor:
                continue
            stem = section_id.lower()
            md_path = stem_index.get(stem)
            if md_path is None:
                continue  # already reported in file-presence check
            total += 1
            try:
                content = md_path.read_text(encoding="utf-8")
                anchors_in_file = set(ANCHOR_RE.findall(content))
                if anchor in anchors_in_file:
                    found += 1
                else:
                    missing_anchor.append((term, anchor, str(md_path.relative_to(data_dir))))
            except Exception:
                pass

        if total == 0:
            print(f"  [{act}]: no terms to check")
            continue

        pct_found = 100.0 * found / total
        pct_missing = 100.0 - pct_found
        print(
            f"  [{act}]: {found}/{total} anchors found in markdown "
            f"({pct_found:.1f}% present, {pct_missing:.1f}% missing)"
        )
        if missing_anchor:
            warnings.append(
                f"{act}: {len(missing_anchor)}/{total} term anchors not found in markdown "
                f"(known gap — parsers do not yet emit per-term anchors)"
            )
            for term, anchor, path in missing_anchor[:3]:
                print(f"    e.g. term={term!r} anchor={anchor!r} in {path}")
            if len(missing_anchor) > 3:
                print(f"    ... and {len(missing_anchor) - 3} more")


def check_acts_without_definitions(
    data_dir: Path, defs_all: dict, hard_failures: list[str], warnings: list[str]
) -> None:
    """Warn for acts that have tree.json but no key in definitions_all.json."""
    print("\n--- Acts with tree.json but no definitions entry (warnings) ---")
    for act_dir in sorted(data_dir.iterdir()):
        if not act_dir.is_dir():
            continue
        act = act_dir.name
        if not (act_dir / "tree.json").exists():
            continue
        if act not in defs_all:
            warnings.append(f"{act}: has tree.json but no entry in definitions_all.json (expected for guides)")
            print(f"  WARNING [{act}]: no definitions_all.json entry (expected for guide acts)")
        else:
            print(f"  OK   [{act}]: present in definitions_all.json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the legislation data/ directory.")
    repo_root = Path(__file__).parent.parent
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=repo_root / "data",
        help="Path to the data/ directory (default: repo root / data)",
    )
    args = parser.parse_args()
    data_dir: Path = args.data_dir.resolve()

    if not data_dir.is_dir():
        print(f"ERROR: data directory not found: {data_dir}")
        return 1

    print(f"Validating data directory: {data_dir}")

    hard_failures: list[str] = []
    warnings: list[str] = []

    # --- Hard failure checks ---
    check_tree_paths(data_dir, hard_failures, warnings)
    defs_all = check_definitions_all(data_dir, hard_failures, warnings)
    check_definition_section_files(data_dir, defs_all, hard_failures, warnings)
    check_junk_keys(data_dir, defs_all, hard_failures, warnings)
    check_anchor_uniqueness(data_dir, defs_all, hard_failures, warnings)

    # --- Warning-only checks ---
    check_duplicate_section_ids(data_dir, hard_failures, warnings)
    check_anchor_in_markdown(data_dir, defs_all, hard_failures, warnings)
    check_acts_without_definitions(data_dir, defs_all, hard_failures, warnings)
    check_definition_findability(data_dir, defs_all, hard_failures, warnings)

    # --- Summary ---
    print("\n" + "=" * 60)
    if warnings:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")

    print()
    if hard_failures:
        print(f"HARD FAILURES ({len(hard_failures)}):")
        for f in hard_failures:
            print(f"  - {f}")
        print()
        print(f"VALIDATION FAILED ({len(hard_failures)} hard failures)")
        return 1
    else:
        print("VALIDATION PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
