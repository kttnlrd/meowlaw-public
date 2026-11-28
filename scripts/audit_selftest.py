#!/usr/bin/env python3.12
"""Prove the audit can fail.

A suite that passes on every host is either testing a healthy system or testing nothing. This
injects one real fault at a time into a host, runs audit_features.py, and requires the matching
check to fail. Any mutation the audit does not catch is a hole in the audit, not a healthy host.

Each mutation is reverted in a ``finally`` block and the revert is verified, so a crash mid-run
leaves the host as it was.

Usage
  audit_selftest.py --spec specs/audit-lawkitty.json --base https://lawkitty.app \
      --mcp-token "$TOKEN" --app-root /path/to/app \
      --data-root /path/to/app --dist /path/to/app/frontend/dist

Exit 0 only when every injected fault was caught.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "audit_features.py")


def run_audit(args, extra: list[str]) -> tuple[list[dict], str]:
    out = tempfile.mktemp(suffix=".json")
    cmd = [sys.executable, AUDIT, "--spec", args.spec, "--base", args.base,
           "--app-root", args.app_root, "--json", out] + extra
    if args.mcp_token and "--mcp-token" not in extra:
        cmd += ["--mcp-token", args.mcp_token]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    try:
        rows = json.load(open(out))["rows"]
    except Exception:
        rows = []
    finally:
        if os.path.exists(out):
            os.remove(out)
    return rows, proc.stdout + proc.stderr


def failed_checks(rows: list[dict]) -> set[str]:
    return {f"{r['phase']}/{r['check']}" for r in rows if not r["ok"]}


class Mutation:
    """One injected fault: what to break, what the audit must then report."""

    def __init__(self, name: str, expect: str, apply, revert, extra: list[str] | None = None):
        self.name = name
        self.expect = expect
        self.apply = apply
        self.revert = revert
        self.extra = extra or []


def mutations(args) -> list[Mutation]:
    out: list[Mutation] = []

    # 1. a document file disappears: the download phase must notice missing bytes
    rg = os.path.join(args.data_root, "data", "regulatory-guides", "pdfs", "RG_001.pdf")

    def hide_pdf():
        if os.path.exists(rg):
            shutil.move(rg, rg + ".selftest-hidden")

    def show_pdf():
        if os.path.exists(rg + ".selftest-hidden"):
            shutil.move(rg + ".selftest-hidden", rg)

    out.append(Mutation("regulatory guide PDF removed", "downloads/regulatory guide",
                        hide_pdf, show_pdf))

    # 2. the built bundle stops being served: the ui phase must not accept an SPA fallback
    assets = os.path.join(args.dist, "assets")
    moved: list[tuple[str, str]] = []

    def hide_bundle():
        moved.clear()
        for name in sorted(os.listdir(assets)):
            if name.endswith(".js") and name.startswith("index-"):
                src = os.path.join(assets, name)
                dst = src + ".selftest-hidden"
                shutil.move(src, dst)
                moved.append((src, dst))

    def show_bundle():
        for src, dst in moved:
            if os.path.exists(dst):
                shutil.move(dst, src)

    out.append(Mutation("built JS bundle removed", "ui/deployed bundle", hide_bundle, show_bundle))

    # 3. a wrong MCP token: the handshake must fail rather than skip
    out.append(Mutation("bad MCP token", "mcp/handshake", lambda: None, lambda: None,
                        extra=["--mcp-token", "not-a-real-token"]))

    # 4. a gated route asserted public: the rest phase must catch the 401
    spec = json.loads(open(args.spec).read())
    broken = json.loads(json.dumps(spec))
    for item in broken.get("rest", []):
        if item.get("expect") == 401:
            item["expect"] = 200
            break
    tmp_spec = tempfile.mktemp(suffix=".json")
    json.dump(broken, open(tmp_spec, "w"))
    out.append(Mutation("gated route asserted public", "rest/" + next(
        (i["name"] for i in broken["rest"] if i.get("expect") == 200 and "gated" in i["name"]), "acts"),
        lambda: None, lambda: None, extra=["--spec", tmp_spec]))

    # 5. a bogus download id: the phase must fail on a 404, not pass on any response
    broken2 = json.loads(json.dumps(spec))
    for item in broken2.get("downloads", []):
        if item["name"] == "public ruling":
            item["id"] = "ZZ 9999/999"
            break
    tmp_spec2 = tempfile.mktemp(suffix=".json")
    json.dump(broken2, open(tmp_spec2, "w"))
    out.append(Mutation("download id that does not exist", "downloads/public ruling",
                        lambda: None, lambda: None, extra=["--spec", tmp_spec2]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--mcp-token", default="")
    ap.add_argument("--app-root", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--dist", required=True)
    args = ap.parse_args()

    results = []
    for m in mutations(args):
        applied = False
        try:
            m.apply()
            applied = True
            rows, log = run_audit(args, m.extra)
            caught = m.expect in failed_checks(rows)
            detail = ""
            if not caught:
                detail = (f"expected {m.expect}; failures were "
                          f"{sorted(failed_checks(rows))[:5] or 'none'}")
        finally:
            m.revert()
            if applied and m.name != "bad MCP token":
                pass  # revert function verified below by the caller's own run
        results.append((m.name, m.expect, caught, detail))
        print(f"  [{'CAUGHT' if caught else 'MISSED'}] {m.name:34} -> {m.expect}  {detail[:90]}")

    missed = [r for r in results if not r[2]]
    print(f"\n  {len(results) - len(missed)}/{len(results)} injected faults caught")
    if missed:
        print("  the audit does not bite on: " + ", ".join(r[0] for r in missed))
    # prove the host is clean again
    rows, _log = run_audit(args, [])
    left = failed_checks(rows)
    print(f"  after revert: {len(failed_checks(rows))} failing checks"
          + (f" {sorted(left)[:4]}" if left else ""))
    return 0 if not missed and not left else 1


if __name__ == "__main__":
    sys.exit(main())
