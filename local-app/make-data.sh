#!/usr/bin/env bash
# Package a LawKitty data folder as the separate download the local app reads.
#
#   local-app/make-data.sh <corpus-data-dir> [out-dir]
#
# Copies only what the local app serves (every source folder with a tree.json, its sections,
# licence and collection files, the definitions index and the search index if present) into
# <out-dir>/lawkitty-data/, stamps it with DATA_VERSION, and archives it as
# <out-dir>/lawkitty-data-<version>.tar.gz. The corpus folder is only read, never changed.
#
# Left out: collections whose collection.yaml says access: restricted or admin (private or
# licensed material), anything listed in EXCLUDE below, and the server-only databases
# (embeddings, users, graph), which the local app does not use.
set -euo pipefail

SRC="${1:?usage: make-data.sh <corpus-data-dir> [out-dir]}"
OUT="${2:-.}"
SRC="$(cd "$SRC" && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
case "$OUT/" in "$SRC"/*) echo "out-dir must be outside the corpus folder" >&2; exit 1 ;; esac

# Extra folders never to package, space-separated (e.g. MAKE_DATA_EXCLUDE="firm-notes drafts").
# Private collections are normally caught by their access: restricted line instead.
read -r -a EXCLUDE <<< "${MAKE_DATA_EXCLUDE:-}"
VERSION="$(date -u +%Y%m%d)"
DEST="$OUT/lawkitty-data"
rm -rf "$DEST"
mkdir -p "$DEST"

n=0
for dir in "$SRC"/*/; do
  id="$(basename "$dir")"
  [ -f "$dir/tree.json" ] || continue
  for x in "${EXCLUDE[@]+"${EXCLUDE[@]}"}"; do [ "$id" = "$x" ] && continue 2; done
  if [ -f "$dir/collection.yaml" ] && grep -qE '^access:[[:space:]]*(restricted|admin)' "$dir/collection.yaml"; then
    echo "skip $id (restricted collection)"
    continue
  fi
  mkdir -p "$DEST/$id"
  for f in tree.json licence.json collection.yaml aliases.json; do
    [ -f "$dir/$f" ] && cp -p "$dir/$f" "$DEST/$id/"
  done
  [ -d "$dir/sections" ] && cp -rp "$dir/sections" "$DEST/$id/"
  n=$((n + 1))
done

# Shared files the local app reads: definitions (definition links in sections), search index.
for f in definitions_all.json search_index.db; do
  [ -f "$SRC/$f" ] && cp -p "$SRC/$f" "$DEST/"
done

printf '%s\n' "$VERSION" > "$DEST/DATA_VERSION"
echo "$n sources -> $DEST ($(du -sh "$DEST" | cut -f1)), version $VERSION"

tar -C "$OUT" -czf "$OUT/lawkitty-data-$VERSION.tar.gz" lawkitty-data
echo "archive: $OUT/lawkitty-data-$VERSION.tar.gz ($(du -sh "$OUT/lawkitty-data-$VERSION.tar.gz" | cut -f1))"
