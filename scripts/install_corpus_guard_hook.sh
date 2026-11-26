#!/bin/sh
# Install the corpus change guard as this clone's pre-commit hook.
set -e
ROOT=$(git rev-parse --show-toplevel)
HOOK="$ROOT/.git/hooks/pre-commit"
chmod +x "$ROOT/scripts/pre_commit_corpus_guard.sh"
if [ -e "$HOOK" ] && [ ! -L "$HOOK" ]; then
    echo "refusing to overwrite existing $HOOK — move it aside first" >&2
    exit 1
fi
ln -sfn ../../scripts/pre_commit_corpus_guard.sh "$HOOK"
echo "installed $HOOK -> scripts/pre_commit_corpus_guard.sh"
echo "bypass for deliberate bulk work: CORPUS_GUARD_BYPASS=1 git commit ..."
