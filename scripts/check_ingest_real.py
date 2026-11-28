"""Ingest every real source with its adapter into a scratch folder, lint each, report counts.

    python scripts/check_ingest_real.py [--data DATA_DIR] [--private-rulings DIR] [--only NAME ...]

Reads sources only; writes nothing outside a temporary folder (deleted afterwards). Each row
fails when the adapter errors, the lint finds errors, or fewer documents than the floor came
through (the floors are set below today's counts, so a shrinking source shows up).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ingest_collection as ing  # noqa: E402
from lint_collection import lint, log_path, log_run  # noqa: E402

# name, adapter, source (relative to --data unless absolute / a {placeholder}), collection id, floor
SOURCES = [
    ("Keays", "keays", "insolvency-keays", "insolvency-keays", 21),
    ("Tax treaties", "treaties", "treaties", "tax-treaties", 1200),
    ("ASIC regulatory guides", "guides", "regulatory-guides", "asic-regulatory-guides", 170),
    ("AFSA practice guidance", "guides", "afsa", "afsa-practice-guidance", 45),
    ("ATO public rulings", "rulings", "rulings", "public-rulings", 11000),
    ("NZ IRD rulings", "rulings", "nz_rulings", "nz-ird-rulings", 1500),
    ("ATO private rulings", "private_rulings", "{private_rulings}", "ato-private-rulings", 57000),
    ("Case law", "cases", ".", "case-law", 70000),
    ("CCH Master Tax Guide", "built_tree", "master-tax-guide", "master-tax-guide", 1393),
    ("CCH Master GST Guide", "built_tree", "master-gst-guide", "master-gst-guide", 425),
    ("CCH Master Tax Examples", "built_tree", "master-tax-examples", "master-tax-examples", 310),
    ("NZ Master Tax Guide", "built_tree", "nz-master-tax-guide", "nz-master-tax-guide", 1654),
]
# Instance-only sources (private firm material) are listed in scripts/ingest_sources.local.json,
# which is not committed: [["Name", "adapter", "source", "collection-id", floor], ...]
_LOCAL = ROOT / "scripts" / "ingest_sources.local.json"
if _LOCAL.is_file():
    SOURCES += [tuple(r) for r in json.loads(_LOCAL.read_text(encoding="utf-8"))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--private-rulings", default=os.environ.get("PRIVATE_RULINGS_DIR")
                    or str(ROOT / "data" / "private-rulings" / "json"))
    ap.add_argument("--only", nargs="*", help="adapter or collection ids to run")
    ap.add_argument("--report", action="store_true", help="summarise the run log instead of running")
    a = ap.parse_args()
    if a.report:
        return report()
    data = Path(a.data)
    bad = 0
    with tempfile.TemporaryDirectory(prefix="ingest-check-") as tmp:
        for name, adapter, src, cid, floor in SOURCES:
            if a.only and adapter not in a.only and cid not in a.only:
                continue
            path = Path(a.private_rulings) if src == "{private_rulings}" else data / src
            t0 = time.time()
            if not path.exists():
                print(f"SKIP {name:26} source not here: {path}")
                log_run(run="real-data", source=str(path), collection=cid, adapter=adapter, docs=None,
                        errors=[], warnings=[], status="skip", note="source not here")
                continue
            try:
                mod = ing.adapter(adapter)
                with contextlib.redirect_stdout(io.StringIO()):
                    parts = mod.parts(path)
                    nparts, ndocs = ing.write(cid, parts, getattr(mod, "TITLE", None) or name,
                                              getattr(mod, "GROUP", None), "restricted", Path(tmp))
                errs, warns = lint(Path(tmp) / cid)
                warns = list(getattr(mod, "ISSUES", [])) + warns  # what the adapter had to drop
            except (Exception, SystemExit) as e:  # noqa: BLE001 - report and carry on
                print(f"FAIL {name:26} adapter error: {e!r}"[:300])
                log_run(run="real-data", source=str(path), collection=cid, adapter=adapter, docs=None,
                        errors=[f"adapter error: {e!r}"[:500]], warnings=[], status="fail")
                bad += 1
                continue
            if ndocs < floor:
                errs = errs + [f"{ndocs} documents, expected at least {floor}"]
            ok = not errs
            bad += not ok
            log_run(run="real-data", source=str(path), collection=cid, adapter=adapter, docs=ndocs,
                    errors=errs, warnings=warns, status="ok" if ok else "fail")
            print(f"{'ok  ' if ok else 'FAIL'} {name:26} {ndocs:7} docs / {nparts:4} parts  "
                  f"lint {len(errs)}E {len(warns)}W  floor {floor}  {time.time() - t0:5.1f}s")
            for e in errs[:5]:
                print("       error:", e)
            # keep the scratch folder small: drop each collection once checked
            shutil.rmtree(Path(tmp) / cid, ignore_errors=True)
    print("ALL OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def report(last: int = 10) -> int:
    """Per collection: the last runs' document counts and status, and every issue kind seen
    with how many runs it appeared in, most persistent first."""
    p = log_path()
    if not p.is_file():
        print(f"no log yet at {p}")
        return 0
    runs: dict[str, list[dict]] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        runs.setdefault(r["collection"], []).append(r)
    for cid, rs in sorted(runs.items()):
        rs = rs[-last:]
        trend = " ".join(f"{r['docs'] if r['docs'] is not None else '-'}{'' if r['status'] == 'ok' else '!'}"
                         for r in rs)
        print(f"{cid}  (last {len(rs)} runs, ! = not ok): {trend}")
        seen: dict[str, list[int]] = {}
        for r in rs:
            for k, n in (r.get("kinds") or {}).items():
                seen.setdefault(k, []).append(n)
        for k, ns in sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:8]:
            print(f"    {len(ns):2}/{len(rs)} runs  x{ns[-1]:<6} {k}"[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
