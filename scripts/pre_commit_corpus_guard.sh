#!/bin/sh
# Versioned git pre-commit hook: run corpus_change_guard over the staged
# corpus section files AND any untracked (never-added) section file.
# Installed by scripts/install_corpus_guard_hook.sh.
#
# Escape hatch for deliberate bulk work (e.g. a reviewed compilation import):
#   CORPUS_GUARD_BYPASS=1 git commit ...
set -e
TOP=$(git rev-parse --show-toplevel)
PY=/usr/bin/python3.12
[ -x "$PY" ] || PY=python3

# Instance overlay + optional-data leak guard (plan §4.2). Runs BEFORE the corpus bypass
# below: CORPUS_GUARD_BYPASS is for bulk corpus work, not for shipping optional data.
overlay=$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^data_sources\.local\.yaml$|^artifacts/|^instance/' || true)
if [ -n "$overlay" ]; then
    echo "overlay guard: instance overlay files must never be committed:" >&2
    echo "$overlay" | sed 's/^/  /' >&2
    exit 1
fi
# Added lines outside content/ that carry a registry data_markers string. Only
# added lines: files that already mention a marker (docs, the guard itself) stay
# committable, a new mention does not.
git diff --cached -U0 --diff-filter=ACMR -- . ':(exclude)content/' | env -u PYTHONPATH "$PY" -c '
import glob, sys, yaml
top = sys.argv[1]
markers = {m for f in glob.glob(top + "/content/*/type.yaml")
           for m in (yaml.safe_load(open(f)) or {}).get("data_markers") or []}
path, hits = None, []
for line in sys.stdin:
    if line.startswith("+++ "):
        path = line[6:].rstrip("\n")
    elif line.startswith("+") and markers:
        hits += [f"{path}: {m}" for m in markers if m in line]
if hits:
    print("overlay guard: data_markers found in staged lines outside content/:", file=sys.stderr)
    print("\n".join("  " + h for h in sorted(set(hits))), file=sys.stderr)
    sys.exit(1)
' "$TOP"

[ -n "$CORPUS_GUARD_BYPASS" ] && { echo "corpus guard: bypassed (CORPUS_GUARD_BYPASS set)"; exit 0; }

# Nothing to do, and no python started, UNLESS a section file is either staged or untracked.
# X1 (2026-09-26): the early exit used to test only the staged diff, so a commit that staged
# code and nothing else walked straight past corpus_change_guard.py's untracked-file scan
# (:194) - a bulk dump into data/ never touches the index, so a bad-shape section file could
# sit unnoticed until some unrelated corpus commit. Both populations are now the guard's
# trigger; the guard itself decides what to do with them.
if ! git diff --cached --name-only | grep -Eq '^data/[^/]+/sections/.*\.md$' \
   && ! git ls-files --others --exclude-standard -- data | grep -Eq 'sections/.*\.md'; then
    exit 0
fi

exec "$PY" "$TOP/scripts/corpus_change_guard.py" --quiet-ok
