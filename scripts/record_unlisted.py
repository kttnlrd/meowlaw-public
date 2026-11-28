#!/usr/bin/env python3.12
"""Record which route families are gated (and therefore unauditable without a session).

Coverage only earns its keep if the unlisted set is deliberate. This walks the families the app
actually mounts, asks the live host for each one anonymously, and records the gated ones in the
spec's ``known_unlisted``. A family that answers anonymously and is not otherwise named in the
spec stays out of the list, so the audit keeps failing until somebody writes a check for it.

Usage: record_unlisted.py <spec.json> <app-root> <base-url>
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    spec_path, app_root, base = sys.argv[1], sys.argv[2], sys.argv[3]
    spec = json.loads(open(spec_path).read())
    cwd = os.getcwd()
    sys.path.insert(0, app_root)
    os.chdir(app_root)
    try:
        from backend.main import app  # type: ignore
        families = set()
        for r in app.routes:
            parts = [p for p in (getattr(r, "path", "") or "").split("/") if p]
            if len(parts) >= 2 and parts[0] == "api":
                families.add("/" + "/".join(parts[:2]))
    finally:
        os.chdir(cwd)

    named = {("/" + "/".join([p for p in i["path"].split("/") if p][:2]))
             for i in spec.get("rest", []) + spec.get("downloads", [])}
    gated, absent, open_unlisted = [], [], []
    for fam in sorted(families - named):
        req = urllib.request.Request(base.rstrip("/") + fam, headers={"User-Agent": "audit/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0
        if code in (401, 403, 302, 303, 307):
            gated.append(fam)          # needs a session, unverifiable host-only
        elif code == 404:
            absent.append(fam)         # this build does not mount it
        else:
            open_unlisted.append(fam)  # answers anonymously and nobody checks it: a real gap

    spec["known_unlisted"] = gated
    spec["known_unlisted_note"] = ("Gated families, unverifiable without a session cookie. The "
                                   "coverage phase treats these as accounted for; any new family "
                                   "that answers anonymously still fails.")
    spec["not_on_this_host"] = absent
    spec["not_on_this_host_note"] = "Families this build does not mount (404 anonymously)."
    open(spec_path, "w").write(json.dumps(spec, indent=1) + "\n")
    print(f"  {os.path.basename(spec_path)}: {len(gated)} gated, {len(absent)} not on this host")
    if open_unlisted:
        print(f"  UNAUDITED, answers anonymously: {open_unlisted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
