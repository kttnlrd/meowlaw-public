#!/usr/bin/env python3.12
"""X2: fix_page_rule_artifacts.py must leave a rollback tag and an in-repo provenance log.

The script wrote /tmp/cdn203-apply.json and created no tag. Both are the template every Part B
repair script would copy, so the smoke test runs --apply for real on a temporary copy of a
fixture in a throwaway git repo and asserts the rollback point and the log exist, then runs
--verify against the log it just wrote. Nothing outside the temp directory is touched.

Run: /usr/bin/python3.12 scripts/test_page_rule_provenance.py      (exit 1 on any failure)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "fix_page_rule_artifacts.py"
RUN = "_" * 37
FIXTURE = (
    '---\nact: "ITAA 1997"\nsection: "27-5"\n---\n\n# 27-5 What this Division is about\n\n'
    f"**(1)** For the effect of the GST, see Division 27. Note If you receive an amount {RUN}\n\n"
    "amount may be included in your assessible income: see Subdivision 20-A.\n"
)

failures = 0


def check(ok: bool, label: str, detail: str = ""):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" :: {detail}" if detail and not ok else ""))


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "scripts").mkdir()
    shutil.copyfile(SCRIPT, root / "scripts" / SCRIPT.name)
    sec = root / "data" / "itaa-1997" / "sections"
    sec.mkdir(parents=True)
    (sec / "27-5.md").write_text(FIXTURE, encoding="utf-8")

    def git(*args, **kw):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, **kw)

    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=test", "commit", "-q", "-m", "fixture")
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    p = subprocess.run([sys.executable, str(root / "scripts" / SCRIPT.name), "--apply", "--block-size", "1"],
                       cwd=root, capture_output=True, text=True)
    print("      " + (p.stdout or "").strip().replace("\n", "\n      "))
    check(p.returncode == 0, "--apply exits 0", p.stderr[-200:])

    logs = sorted((root / "docs" / "provenance").glob("cdn203-page-rule-artifacts-*.json"))
    check(bool(logs), f"provenance log written under docs/provenance/ -> {[l.name for l in logs]}")
    src = SCRIPT.read_text(encoding="utf-8")
    check('"docs" / "provenance"' in src and not re.search(r'^LOG\s*=\s*Path\(', src, re.M),
          "the script writes provenance under docs/provenance/ and has no /tmp LOG constant "
          "(the old path may still be named in the comment explaining the move)")

    tags = git("tag", "-l").stdout.split()
    expected_tag = f"page-rule-apply-rollback-{head}"
    check(expected_tag in tags, f"rollback tag {expected_tag} exists -> {tags}")

    if logs:
        rec = json.loads(logs[0].read_text())
        check(rec.get("rollback_tag") == expected_tag,
              f"the log names the tag ({rec.get('rollback_tag')})")
        check(rec.get("head") == head, f"the log names HEAD ({rec.get('head')})")
        check(rec.get("files") and rec["files"][0]["file"].startswith("data/"),
              "the log records the file it wrote, relative to the repo root")
        check(any(c["kind"] == "page_rule_removed" for c in rec["files"][0]["changes"]),
              "the recorded change is the page-rule removal")
        fixed = (sec / "27-5.md").read_text(encoding="utf-8")
        check(RUN not in fixed, "the page rule is gone from the fixture")

        v = subprocess.run([sys.executable, str(root / "scripts" / SCRIPT.name), "--verify"],
                           cwd=root, capture_output=True, text=True)
        check(v.returncode == 0, "--verify passes against the log it wrote", (v.stdout or "")[-200:])

    # a second apply must not silently reuse the first run's log
    p2 = subprocess.run([sys.executable, str(root / "scripts" / SCRIPT.name), "--apply"],
                        cwd=root, capture_output=True, text=True)
    check(p2.returncode == 0 and "nothing to do" in p2.stdout,
          f"a second --apply with nothing left to fix writes no new log -> {p2.stdout.strip()[:80]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
