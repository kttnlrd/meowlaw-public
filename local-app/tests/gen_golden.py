#!/usr/bin/env python3
"""Write what the Python app serves, for the Rust contract test to compare byte for byte.

Calls the real route functions in backend/routes/acts.py (no HTTP, no sign-in, no database)
against ../data, and writes one file per request plus manifest.json:

    python3 local-app/tests/gen_golden.py                 # the committed set, local-app/tests/golden
    python3 local-app/tests/gen_golden.py --sweep 40 --out /tmp/lk-sweep
                                                      # + every shallow tree and 40 sections per source
    LAWKITTY_GOLDEN=/tmp/lk-sweep cargo test --release --test contract

Also rewrites local-app/src/info.json (the /api/info body) from backend/routes/api.py.

The local build serves tree-backed sources only, so the Python output is taken as a user who
has every tree-backed source and none of the browsers the local build leaves out (rulings,
tax cases, private rulings, guides, treaties, maps).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routes import acts, api  # noqa: E402
from backend.services import collections, content_access as ca  # noqa: E402
from backend.services.data_loader import load_acts_meta, load_tree  # noqa: E402

NOT_SERVED_KEYS = {"ruling", "nz-ruling", "case", "ato-private-ruling", "regulatory-guide",
                   "afsa-guide", "treaty", "maps", "workflows", "chains"}
NOT_SERVED_IDS = {"commentary", "insolvency", "nz-mtg"}  # licensed browsers, not trees
# Restricted collections are private material: never written to a golden file.
PRIVATE = {cid for cid, c in collections.all_collections().items() if c["access"] == "restricted"}

AVAILABLE = set(ca.all_type_keys()) - NOT_SERVED_KEYS
acts.available_keys = lambda user: AVAILABLE
acts.user_from = lambda request: None
acts.require_act = lambda request, act: None


def compact(obj) -> bytes:
    """FastAPI's JSONResponse.render."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode("utf-8")


def call(fn):
    """(status, body bytes, is a tree response) for one route call."""
    try:
        out = fn()
    except HTTPException as e:
        return e.status_code, compact({"detail": e.detail}), False
    if hasattr(out, "body"):  # get_tree returns a Response with X-Uncompressed-Length
        assert out.headers["X-Uncompressed-Length"] == str(len(out.body))
        return 200, bytes(out.body), True
    return 200, compact(out), False


def list_acts(jurisdiction=None):
    return [a for a in acts.list_acts(None, jurisdiction)
            if a["id"] not in NOT_SERVED_IDS and a["id"] not in PRIVATE]


def section_ids(act: str) -> list[str]:
    tree = load_tree(act)
    return [str(s["id"]) for p in tree.get("parts", []) for s in acts._sections_in(p)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "golden")
    ap.add_argument("--sweep", type=int, default=0, help="also write N random sections per source and every shallow tree")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    info = compact(api.api_info())
    (ROOT / "local-app" / "src" / "info.json").write_bytes(info)

    tree = lambda act, **kw: (lambda: acts.get_tree(act, None, **kw))  # noqa: E731
    part = lambda act, **kw: (lambda: acts.get_tree_part(act, None, **kw))  # noqa: E731
    section = lambda act, sec: (lambda: acts.get_section(act, sec, None))  # noqa: E731

    cases = [
        ("/api/info", lambda: api.api_info()),
        ("/api/acts", lambda: list_acts()),
        ("/api/acts?jurisdiction=nz", lambda: list_acts("nz")),
        ("/api/tree/itaa-1997?shallow=1", tree("itaa-1997", shallow=True)),
        ("/api/tree/itaa-1997?shallow=1&open=8-1", tree("itaa-1997", shallow=True, open="8-1")),
        ("/api/tree/case-law?shallow=1", tree("case-law", shallow=True)),
        ("/api/tree/constitution?shallow=1", tree("constitution", shallow=True)),
        ("/api/tree/judiciary-act-1903", tree("judiciary-act-1903")),
        ("/api/tree/no-such-act?shallow=1", tree("no-such-act", shallow=True)),
        ("/api/tree/itaa-1997/part?id=2-1", part("itaa-1997", id="2-1")),
        ("/api/tree/itaa-1997/part?section=8-1", part("itaa-1997", section="8-1")),
        ("/api/tree/itaa-1997/part?id=no-such-part", part("itaa-1997", id="no-such-part")),
        ("/api/tree/case-law/part?id=1.1", part("case-law", id="1.1")),
        ("/api/tree/case-law/part?section=2026-hca-18", part("case-law", section="2026-hca-18")),
        ("/api/section/itaa-1997/8-1", section("itaa-1997", "8-1")),
        ("/api/section/itaa-1997/s6-5", section("itaa-1997", "s6-5")),
        ("/api/section/itaa-1936/6", section("itaa-1936", "6")),
        ("/api/section/gst-1999/9-5", section("gst-1999", "9-5")),
        ("/api/section/nz-it-2007/CB-1", section("nz-it-2007", "CB-1")),
        ("/api/section/itaa-1997/no-such-section", section("itaa-1997", "no-such-section")),
    ]
    if args.sweep:
        rng = random.Random(args.seed)
        # The dictionary sections take the definition-formatting pass; they are large.
        for act, sec in (("itaa-1997", "995-1"), ("gst-1999", "195-1"), ("fbt-1986", "136"), ("taa-1953", "2"),
                         ("sis-1993", "10"), ("corporations-act-2001", "9"), ("aml-ctf-2006", "5"),
                         ("nz-it-2007", "YA-1"), ("case-law", "2026-hca-18")):
            cases.append((f"/api/section/{act}/{sec}", section(act, sec)))
        for a in load_acts_meta():
            act = a["id"]
            if act in PRIVATE:
                continue
            cases.append((f"/api/tree/{act}?shallow=1", tree(act, shallow=True)))
            ids = section_ids(act)
            for sec in rng.sample(ids, min(args.sweep, len(ids))):
                cases.append((f"/api/section/{act}/{quote(sec, safe='')}", section(act, sec)))
                if rng.random() < 0.1:
                    cases.append((f"/api/tree/{act}/part?section={quote(sec, safe='')}", part(act, section=sec)))
                    cases.append((f"/api/tree/{act}?shallow=1&open={quote(sec, safe='')}", tree(act, shallow=True, open=sec)))

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.json"):
        old.unlink()
    manifest = []
    seen = set()
    for i, (path, fn) in enumerate(cases):
        if path in seen:
            continue
        seen.add(path)
        status, body, is_tree = call(fn)
        name = f"{i:04d}.json"
        (args.out / name).write_bytes(body)
        manifest.append({"path": path, "file": name, "status": status, "tree": is_tree})
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"{len(manifest)} responses -> {args.out}")


if __name__ == "__main__":
    main()
