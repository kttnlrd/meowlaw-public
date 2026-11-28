#!/usr/bin/env python3
"""FINAL DATA AUDIT — one entry point, one verdict.

Phases:
  1. INTEGRITY      — verify_data_integrity.py (structural gate: 0 critical)
  2. LAYER+CONTENT  — randomised_api_mcp_test.py (API -> MCP -> full-corpus
                      content scan; new date seed each run)
  3. PROD SUITE     — test_prod_v270.py (52 functional API checks)
  4. DETECTOR TESTS — scripts/test_*.py pinned self-tests for the corpus checks

Exit 0 only if ALL four phases pass. Findings sync to CDN tickets via phase 2.
"""
import os, random, re, subprocess, sys
from datetime import date

ROOT = str(Path(__file__).resolve().parent.parent)

# Audit the app with the interpreter the APP runs under (systemd ExecStart uses
# /usr/bin/python3.12). Phases 2-3 import backend modules; the cron wrapper is
# launched by the agent venv python, which has no psycopg2/psycopg2.extras —
# running the prod suite under sys.executable crashed with ModuleNotFoundError
# and reported a meaningless "0/1 passed" (CDN-0191-era misdiagnosis, 2026-09-10).
PY = "/usr/bin/python3.12" if os.path.exists("/usr/bin/python3.12") else sys.executable


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)


def why(p):
    """Last meaningful line of a crashed phase, so the verdict names the cause
    instead of a bare 0/1 (a traceback used to read as 'PROD SUITE: 0/1')."""
    lines = [l.strip() for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if l.strip()]
    return lines[-1][:160] if lines else f"no output (exit {p.returncode})"


ok = True
results = []

# ── Phase 1: integrity gate ──
p1 = run([PY, "scripts/verify_data_integrity.py"])
pass1 = "RESULT: PASS" in p1.stdout
ok = ok and pass1
results.append(f"[1/4] INTEGRITY     : {'PASS' if pass1 else f'FAIL ({why(p1)})'}")

# ── Phase 2: layer + content (random seed each run; reproducible via --seed N) ──
seed = random.SystemRandom().randrange(10_000_000, 99_999_999)
p2 = run([PY, "scripts/randomised_api_mcp_test.py", "--seed", str(seed)])
m2 = re.search(r"TOTAL: (\d+) checks \| FAIL (\d+) \| FIND (\d+)", p2.stdout)
if m2 and p2.returncode == 0:
    checks, fails, finds = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    phase2 = f"(seed={seed}) {checks} checks | FAIL {fails} | FIND {finds}"
    ok = ok and fails == 0
else:
    phase2 = f"(seed={seed}) RUN FAILED (exit {p2.returncode}: {why(p2)})"
    ok = False
results.append(f"[2/4] LAYER+CONTENT : {phase2}")

# ── Phase 3: prod suite ──
p3 = run([PY, "backend/tests/test_prod_v270.py"])
m3 = re.search(r"✅ (\d+) passed", p3.stdout)
m3f = re.search(r"❌ (\d+) failed", p3.stdout)
if m3:
    passed = int(m3.group(1))
    failed = int(m3f.group(1)) if m3f else 0
    ok = ok and failed == 0
    results.append(f"[3/4] PROD SUITE    : {passed}/{passed + failed} passed")
else:
    ok = False
    results.append(f"[3/4] PROD SUITE    : RUN FAILED (exit {p3.returncode}: {why(p3)})")

# ── Phase 4: pinned detector self-tests ──
# A corpus check that was corrected to skip an act's convention can stop firing on the very damage
# it was written for: C6 reported 15 legitimate guide headings as cut remnants and never caught a
# real one. Each such correction ships a self-test asserting BOTH directions, and running them here
# is what stops them rotting into decoration. A detector that no longer fires must fail the audit.
#
# Convention: a corpus-detector self-test is named test_c<CLASS>_<what>.py (mirroring the C1..C12
# class names in scan_corpus_error_classes.py). Do NOT widen this to scripts/test_*.py - that swept
# in unrelated, pre-existing scripts whose dependencies live in other projects (test_scrape_10.py
# died on a missing case-pipeline data file) and reported a detector failure that was not one.
import pathlib as _pl
det_tests = sorted(_pl.Path(ROOT, "scripts").glob("test_c[0-9]*_*.py"))
det_fail = []
for t in det_tests:
    rt = run([PY, str(t.relative_to(ROOT))])
    if rt.returncode != 0:
        det_fail.append(f"{t.name} ({why(rt)})")
ok = ok and not det_fail
# S3 (2026-09-26): every class the CONTENT PHASE emitted must have a self-test, otherwise a
# corrected detector can rot into decoration with nothing noticing. The class list is read from
# phase 2's own output (no extra scan). A class is covered when a test file test_c<N>_*.py exists
# for its prefix, or when a test file names the class itself - the character family (C20-C26)
# deliberately shares one self-test (scripts/test_c20_character_classes.py) that names each class
# it pins. Landed as a REPORT first, as the plan says: the classes still without one are the ones
# no batch has covered yet, so a fatal check here would fail the audit for unplanned work. It
# becomes an assertion once the list is empty.
det_classes = sorted({m.group(1) for m in re.finditer(r"corpus (C\d+[A-Za-z_]*)", p2.stdout or "")})
det_text = "\n".join(t.read_text(errors="replace") for t in det_tests)
untested = [c for c in det_classes
            if not any(t.name.startswith(f"test_{c.split('_')[0].lower()}_") for t in det_tests)
            and not re.search(rf"\b{re.escape(c)}\b", det_text)]
results.append(f"[4/4] DETECTOR TESTS: {len(det_tests) - len(det_fail)}/{len(det_tests)} passed"
               + (f" | FAILED: {', '.join(det_fail)}" if det_fail else "")
               + (f" | {len(untested)} emitted class(es) with no self-test: {' '.join(untested)}"
                  if untested else " | every emitted class has a self-test"))

print("═══ FINAL DATA AUDIT ═══")
for r in results:
    print(r)
print(f"═══ VERDICT: {'PASS' if ok else 'FAIL'} ═══")
sys.exit(0 if ok else 1)
