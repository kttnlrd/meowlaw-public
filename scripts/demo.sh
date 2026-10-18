#!/usr/bin/env sh
# Build MeowLaw, load the bundled Australian tax sources and start the server.
# Run from anywhere: it works inside the repository checkout it lives in.
#
#   scripts/demo.sh             # build, ingest examples/au-tax, serve on 127.0.0.1:8080
#   scripts/demo.sh --no-serve  # build and ingest only
#   scripts/demo.sh --fictional # load the small fictional parser samples instead
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found: install Rust 1.88 or later from https://rustup.rs" >&2
  exit 1
fi

echo "==> building (first build takes a few minutes)"
cargo build --release --locked
bin="$root/target/release/meowlaw"

if [ ! -f meowlaw.toml ]; then
  echo "==> writing meowlaw.toml"
  "$bin" init
fi

serve=1; set_=au-tax
for arg in "$@"; do
  case "$arg" in
    --no-serve) serve=0 ;;
    --fictional) set_=fictional ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$set_" = "fictional" ]; then
  echo "==> ingesting the fictional parser samples"
  "$bin" ingest examples/widget-act --no-rebuild
  "$bin" ingest examples/cases --kind case --no-rebuild
  "$bin" ingest examples/handbook/staff-handbook.md --kind secondary --no-rebuild
  "$bin" ingest examples/xml/model-code.xml --kind legislation --profile eu --no-rebuild
  "$bin" rebuild >/dev/null
else
  # ITAA 1997, ITAA 1936, TR 2018/5 and the Bendel summary: real sources,
  # licences recorded in examples/au-tax/sources/SOURCES.md. The two Acts are
  # about 6,700 sections, so extraction takes a minute or two.
  echo "==> ingesting the Australian tax sources (examples/au-tax)"
  "$bin" sync examples/au-tax/sources.toml --no-lint >/dev/null
fi
"$bin" corpora

if [ "$serve" = 0 ]; then
  echo "==> done. Try: $bin search \"general deductions\""
  exit 0
fi

echo "==> serving: open http://127.0.0.1:8080 (Ctrl-C to stop)"
exec "$bin" serve
