#!/usr/bin/env python3.12
"""Do cases and rulings answer, or do they return an error?

Calls the tools that touch rulings and cases on a host and reports any error-shaped payload, then
shows the first line of each answer so a wrong-but-cheerful result cannot pass as healthy.

Usage: check_cases_rulings.py <base> <token>
"""

from __future__ import annotations
import os

import json
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from audit_features import Mcp  # noqa: E402

ERROR_MARKERS = ("traceback", "does not exist", "internal server error", "error\":", "\"error\"",
                 "not found", "no such", "exception", "failed")


def looks_like_error(text: str) -> str:
    low = text.lower()
    for m in ERROR_MARKERS:
        if m in low:
            return m
    return ""


def main() -> int:
    base, token = sys.argv[1], sys.argv[2]
    client = Mcp(base, token)
    tools, err = client.tools()
    if err:
        print(f"  handshake failed: {err}")
        return 1
    names = {t["name"] for t in tools}
    print(f"  {base}: {len(names)} tools")

    calls: list[tuple[str, dict]] = []
    if "list_rulings" in names:
        calls += [("list_rulings", {"limit": 3}), ("list_rulings", {"counts_only": True})]
    if "fetch" in names:
        calls += [("fetch", {"ref": "rul:TD 2001/14"}), ("fetch", {"ref": "case:[2025] FCAFC 116"}),
                  ("fetch", {"ref": "pr:1011261243735"})]
    if "related" in names:
        calls += [("related", {"id": "leg:itaa-1997:8-1"}),
                  ("related", {"id": "case:[2025] FCAFC 116"})]
    if "notice" in names:
        calls += [("notice", {"ref": "rul:TD 2001/14"})]
    if "graph_neighbourhood" in names:
        calls += [("graph_neighbourhood", {"key": "case:[2025] FCAFC 116", "depth": 1})]

    bad = 0
    for name, args in calls:
        if name not in names:
            continue
        ok, detail, text = client.call(name, args)
        marker = looks_like_error(text) if ok else "tool error"
        first = re.sub(r"\s+", " ", text or detail)[:96]
        flag = "ERROR" if (not ok or marker) else "ok"
        if flag == "ERROR":
            bad += 1
        print(f"  [{flag:5}] {name}({json.dumps(args)[:44]}) {detail:12} | {first}")
    print(f"  {bad} error-shaped answer(s) out of {len(calls)} calls")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
