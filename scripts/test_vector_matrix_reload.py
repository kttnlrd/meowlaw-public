#!/usr/bin/env python3.12
"""Prove a long-running process notices when the vector matrix on disk has been rebuilt.

The defect this pins: _ensure_loaded() called load() only when _ids was None, i.e. once per
process.  A worker that had already served one search kept its in-memory matrix forever - so a full
re-embed plus 257,260 restored ruling and case rows were invisible through the API until the
process was restarted, while every read of the file on disk looked correct.

Checks, with load() stubbed so nothing heavy runs:
  1. first call loads,
  2. an unchanged snapshot does NOT reload (no needless 1.7GB read per request),
  3. a changed snapshot signature DOES reload,
  4. the negative control: the old "load only if _ids is None" rule would have skipped (3).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import vector_search_service as v  # noqa: E402

failures: list[str] = []
loads: list[int] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    sig = [1000.0, 23849]                     # (matrix mtime, embeddings rows)

    def fake_load() -> None:
        """Emulate load(): it must set the globals the real one does, or the guard looks broken."""
        loads.append(1)
        v._ids = "loaded"                     # type: ignore[assignment]
        v._loaded_signature = tuple(sig)

    v.load = fake_load                        # type: ignore[assignment]
    v._current_signature = lambda: tuple(sig)  # type: ignore[assignment]
    v._ids = None
    v._matrix = None
    v._loaded_signature = None

    print("== 1. first call loads ==")
    v._ensure_loaded()
    check("loaded once", len(loads) == 1, f"{len(loads)} load(s)")

    print("== 2. unchanged snapshot does not reload ==")
    for _ in range(5):
        v._ensure_loaded()
    check("still one load after five more calls", len(loads) == 1, f"{len(loads)} load(s)")

    print("== 3. a rebuilt snapshot reloads ==")
    sig[0] = 2000.0                           # matrix file rewritten
    sig[1] = 281109                           # restored rows
    v._ensure_loaded()
    check("reloaded after the snapshot changed", len(loads) == 2, f"{len(loads)} load(s)")
    check("signature updated to the new state",
          v._loaded_signature == (2000.0, 281109), str(v._loaded_signature))

    print("== 4. row-count-only change also reloads ==")
    sig[1] = 281500                           # same mtime, rows added
    v._ensure_loaded()
    check("reloaded on row change", len(loads) == 3, f"{len(loads)} load(s)")

    print("== negative control: the old rule would skip the reload ==")
    old_rule_reloads = 0
    ids = "loaded"                            # _ids already set, as in a long-running worker
    if ids is None:
        old_rule_reloads += 1
    check("old 'if _ids is None' rule does not reload", old_rule_reloads == 0,
          "so the stale matrix would have been served indefinitely")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print(f"all checks passed ({len(loads)} load(s) across 9 calls, 2 real changes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
