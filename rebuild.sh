#!/bin/bash
# Legislation Explorer — Reproducible full rebuild from source PDFs.
# 
# WARNING: This wipes and regenerates data/sections/* for all acts.
# Run backup first: ./scripts/backup.sh
#
#   ./rebuild.sh                 full rebuild
#   ./rebuild.sh --check-source  validation + the 1b source guard, then exit before stage 1.
#                                Write-free (stages 0 and 1b only read source/ and the corpus
#                                frontmatter), so it is safe to run as a test.
#
# Pipeline order is documented inline. Do not reorder stages without
# understanding dependencies.
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"
PROJECT="$(pwd)"
DATA="$PROJECT/data"
SOURCE="$PROJECT/source"

export PYTHONPATH="${PROJECT}/backend:${PYTHONPATH:-}"

CHECK_SOURCE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check-source) CHECK_SOURCE_ONLY=1 ;;
        *) echo "ERROR: unknown argument: $arg"; echo "usage: rebuild.sh [--check-source]"; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# 0. Validation
# ---------------------------------------------------------------------------
echo "=== 0. Validation ==="
for cmd in pdftotext python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found"
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 1b. Source compilation guard — refuse to stamp a compilation the source is not.
#
# This block runs BEFORE stage 1 on purpose. Stage 1 writes data/*/raw, so a guard placed
# after it has no write-free mode and cannot be exercised by a test. This block only reads
# source/ and the corpus frontmatter, which is what makes `rebuild.sh --check-source` safe.
#
# The in-repo ITAA 1997 PDFs were Compilation No. 263 (C2026C00122) while stage 2 stamped
# --compilation-no 266. Without this check the build produces comp-263 content labelled 266:
# older text claiming to be current, and comp-266-only provisions (e.g. 40-291A) silently absent.
# The guard reads the compilation number off the PDF itself, never the filename. The itaa-1997
# source is now the vendored comp-266 set (C2026C00324VOL01-12.pdf, batch 5).
#
# Expected numbers match the source AND the corpus frontmatter (checked by
# scripts/test_rebuild_guards.py):
#   itaa-1997 266 (2026-07-01)   itaa-1936 191 (2026-04-01)
#   gst-1999   96 (2026-01-01)   taa-1953  222 (2026-04-01)
# ---------------------------------------------------------------------------
echo "=== 1b. Source compilation guard ==="
guard_failed=0
for spec in "itaa-1997 266" "itaa-1936 191" "gst-1999 96" "taa-1953 222"; do
    set -- $spec
    act_dir="$SOURCE/$1"; want="$2"
    # A guarded act with no source directory must FAIL, not skip. It used to be an
    # `if [ -d "$act_dir" ]` wrapper: gst-1999 had no directory, so its guard never ran and
    # stage 5 re-parsed whatever data/gst-1999/raw happened to hold.
    if [ ! -d "$act_dir" ]; then
        echo "  STOP $act_dir is missing. This act is guarded, so the build must not"
        echo "       fall through to a stale data/$1/raw — vendor source/$1/*.pdf first."
        guard_failed=1
        continue
    fi
    if ! /usr/bin/python3.12 scripts/check_source_compilation.py \
            --pdf-dir "$act_dir" --expected "$want"; then
        guard_failed=1
    fi
    # Guard of the guard: the source must not be OLDER than the corpus it would replace,
    # or remedy (b) below could downgrade published law text with nothing to catch it.
    if ! /usr/bin/python3.12 scripts/check_guard_vs_corpus.py \
            --act "$1" --expected "$want" --corpus-dir "$DATA"; then
        guard_failed=1
    fi
done
if [ "$guard_failed" -ne 0 ]; then
    cat <<'EOF'

ABORT: the source volumes are not the compilation this build would stamp, or they are
       older than the corpus already on disk.

Fix one of these, deliberately — do not bypass:
  (a) vendor the correct compilation's PDFs into source/<act>/ so the source matches the stamp, or
  (b) change the --compilation-no in this script to the compilation the source actually is,
      and accept that the corpus becomes that older compilation.
  (c) if the source is merely MISSING (no source/<act>/), vendoring it is the only option —
      (b) does not apply, because there would be no source to rebuild from at all.

(b) is REJECTED for itaa-1997. It was the remedy that looked safe and is not: the comp-263
PDFs would rebuild the real comp-266 corpus (commit 091945207) as 263 and delete at least 26
comp-266-only sections (40-291A, Div 119, 112-155..185). For itaa-1997 only (a) or (c) apply.

A rebuild that proceeds here would replace current content with older content, or with
undefined content, under a number nothing downstream re-checks.
EOF
    exit 1
fi

if [ "$CHECK_SOURCE_ONLY" -eq 1 ]; then
    echo "--- --check-source: guard passed; exiting before stage 1. Nothing written. ---"
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Extract PDFs to raw text (idempotent — only if raw/ missing or stale)
# ---------------------------------------------------------------------------
echo "=== 1. PDF extraction ==="

extract_pdfs() {
    local act_dir="$1"
    local raw_dir="$2"
    mkdir -p "$raw_dir"
    local extracted=0
    for pdf in "$act_dir"/*.pdf; do
        [ -e "$pdf" ] || continue
        local basename
        basename=$(basename "$pdf" .pdf | tr '[:upper:]' '[:lower:]')
        # Map volume names: C2026C00324VOL01.pdf -> vol01.txt (the register prefix is ignored,
        # so any <register>VOLnn.pdf in source/<act>/ maps to the same raw file name)
        local volname
        volname=$(echo "$basename" | grep -oP 'vol\d+' || echo "$basename")
        local txtout="$raw_dir/${volname}.txt"
        if [ ! -f "$txtout" ] || [ "$pdf" -nt "$txtout" ]; then
            echo "  Extracting $pdf -> $txtout"
            pdftotext -layout "$pdf" "$txtout"
            extracted=$((extracted + 1))
        fi
    done
    echo "  $extracted volumes extracted"
}

extract_pdfs "$SOURCE/itaa-1997"   "$DATA/itaa-1997/raw"
extract_pdfs "$SOURCE/itaa-1936"   "$DATA/itaa-1936/raw"
extract_pdfs "$SOURCE/gst-1999"    "$DATA/gst-1999/raw"
extract_pdfs "$SOURCE/taa-1953"    "$DATA/taa-1953/raw"

# ---------------------------------------------------------------------------
# 2. Parse primary legislation
# ---------------------------------------------------------------------------
echo "=== 2. Parse ITAA 1997 ==="
rm -rf "$DATA/itaa-1997/sections"
python3 pipeline/parse_itaa97.py \
    --raw-dir "$DATA/itaa-1997/raw" \
    --out-dir "$DATA/itaa-1997/sections" \
    --compilation-no 266 \
    --compilation-date 2026-07-01

echo "=== 3. Parse ITAA 1936 (vols 1-4) ==="
rm -rf "$DATA/itaa-1936/sections"
python3 pipeline/parse_itaa36.py \
    --raw-dir "$DATA/itaa-1936/raw" \
    --out-dir "$DATA/itaa-1936/sections" \
    --compilation-no 191 \
    --compilation-date 2026-04-01

echo "=== 4. Parse ITAA 1936 schedules (vol 5) ==="
python3 pipeline/parse_itaa36_schedules.py \
    --raw-file "$DATA/itaa-1936/raw/vol05.txt" \
    --out-dir "$DATA/itaa-1936/sections" \
    --tree-file "$DATA/itaa-1936/tree.json"

echo "=== 5. Parse GST 1999 ==="
rm -rf "$DATA/gst-1999/sections"
python3 pipeline/parse_gst1999.py \
    --raw-dir "$DATA/gst-1999/raw" \
    --out-dir "$DATA/gst-1999/sections" \
    --compilation-no 96 \
    --compilation-date 2026-01-01

echo "=== 6. Parse TAA 1953 ==="
rm -rf "$DATA/taa-1953/sections"
python3 pipeline/parse_taa53.py \
    --raw-dir "$DATA/taa-1953/raw" \
    --out-dir "$DATA/taa-1953/sections" \
    --compilation-no 222 \
    --compilation-date 2026-04-01

# ---------------------------------------------------------------------------
# 3. Build navigation trees
# ---------------------------------------------------------------------------
echo "=== 7. Build trees ==="
python3 pipeline/build_tree.py \
    --sections-dir "$DATA/itaa-1997/sections" \
    --out-file "$DATA/itaa-1997/tree.json" \
    --act "ITAA 1997" \
    --compilation-no 266 \
    --compilation-date 2026-07-01

python3 pipeline/build_tree.py \
    --sections-dir "$DATA/itaa-1936/sections" \
    --out-file "$DATA/itaa-1936/tree.json" \
    --act "ITAA 1936" \
    --compilation-no 191 \
    --compilation-date 2026-04-01

python3 pipeline/build_tree.py \
    --sections-dir "$DATA/gst-1999/sections" \
    --out-file "$DATA/gst-1999/tree.json" \
    --act "GST Act 1999" \
    --compilation-no 96 \
    --compilation-date 2026-01-01

python3 pipeline/build_tree.py \
    --sections-dir "$DATA/taa-1953/sections" \
    --out-file "$DATA/taa-1953/tree.json" \
    --act "TAA 1953" \
    --compilation-no 222 \
    --compilation-date 2026-04-01 \
    --flat-divisions

# ---------------------------------------------------------------------------
# 4. CCH commentary (depends on external MCP pipeline output)
# ---------------------------------------------------------------------------
echo "=== 8. Build CCH commentary ==="
python3 pipeline/build_cch_explorer.py

# ---------------------------------------------------------------------------
# 5. Definitions
# ---------------------------------------------------------------------------
# Flow: extract_definitions.py  -> data/{act}/definitions.json  (per-act term catalog)
#       extract_all_definitions.py -> data/definitions_all.json  (merged; served by backend)
# Definition linking is done at serve time in backend/processors/markdown.py.
# pipeline/link_definitions.py (static in-place linker) has been retired and moved
# to scripts/legacy/link_definitions.py.
echo "=== 9. Extract definitions ==="
python3 pipeline/extract_definitions.py
python3 pipeline/extract_all_definitions.py

# ---------------------------------------------------------------------------
# 6. Citation / ruling / smartlink indices
# ---------------------------------------------------------------------------
echo "=== 10. Build citation index ==="
python3 pipeline/build_citation_index.py

echo "=== 11. Build ruling index ==="
python3 scripts/build_ruling_index.py

echo "=== 12. Build smartlink index ==="
python3 scripts/build_smartlink_index.py

# ---------------------------------------------------------------------------
# 7. Search index
# ---------------------------------------------------------------------------
echo "=== 13. Rebuild search index ==="
rm -f "$PROJECT/search_index.db"
python3 scripts/rebuild_search_index.py

# ---------------------------------------------------------------------------
# 8. Verification
# ---------------------------------------------------------------------------
echo "=== 14. Verification ==="
TOTAL_MD=$(find "$DATA" -name '*.md' | wc -l)
echo "  Total .md files: $TOTAL_MD"
for act in itaa-1997 itaa-1936 gst-1999 taa-1953; do
    tree="$DATA/$act/tree.json"
    if [ -f "$tree" ]; then
        PARTS=$(python3 -c "import json; t=json.load(open('$tree')); print(len(t.get('parts',[])))" 2>/dev/null || echo 0)
        echo "  $act: $PARTS parts"
    else
        echo "  $act: NO tree.json"
    fi
done

if [ -f "$PROJECT/search_index.db" ]; then
    SIZE=$(du -h "$PROJECT/search_index.db" | cut -f1)
    echo "  search_index.db: $SIZE"
else
    echo "  WARNING: search_index.db missing"
fi

echo ""
echo "Rebuild complete."

# ---------------------------------------------------------------------------
# 15. Data validation gate
# ---------------------------------------------------------------------------
echo "=== 15. Data validation ==="
VALIDATE_EXIT=0
python3 "$PROJECT/scripts/validate_data.py" --data-dir "$DATA" || VALIDATE_EXIT=$?
if [ $VALIDATE_EXIT -ne 0 ]; then
    echo "ERROR: Data validation failed — see above for details."
fi
exit $VALIDATE_EXIT
