"""CDN-0204 residual — /api/rulings-list tree titles lost their description.

Commit 3b715b745 fixed the ruling *detail* title extractor and recovered a
`full_title` that, for 32 rulings, is identical to `title` (e.g. GSTR 2000/17,
`full_title == title == 'Goods and services tax: tax invoices'`).
`backend/routes/rulings.py::_tree_title` only appended the description when
`full_title != title`, so those rulings rendered as a bare citation in the tree
sidebar — a regression the extractor introduced (332 → 364 bare titles).

The three checks below fail against that state. The invariant is: a ruling
whose description is *not* just the citation/stem must always render with the
description appended.
"""
from __future__ import annotations

import re

from backend.routes.rulings import _tree_title
from backend.services.data_loader import load_rulings


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _strip_withdrawn(tree_title: str) -> str:
    return tree_title.replace("  [WITHDRAWN]", "")


def _description(r: dict) -> str:
    return (r.get("full_title") or r.get("title") or "").strip()


def _renders_description(r: dict) -> bool:
    """True when _tree_title actually carries the ruling's description."""
    desc = _description(r)
    if not desc or "Legal database" in desc:
        return True  # nothing to append
    base = r.get("citation_display", r["citation"])
    # The description is appended unless it *is* the citation/stem.
    if _norm(desc) in {_norm(base), _norm(r.get("citation", ""))}:
        return True
    # _tree_title truncates to 120 chars on a word boundary; mirror that here.
    short = desc[:120].rsplit(" ", 1)[0] if len(desc) > 120 else desc
    return _norm(short) in _norm(_strip_withdrawn(_tree_title(r)))


def test_gstr_2000_17_tree_title_carries_the_description():
    """One of the 32 rulings the regression left bare."""
    r = next(x for x in load_rulings() if x["citation"] == "GSTR_2000_17")
    title = _tree_title(r)
    assert r["full_title"] == r["title"], "fixture moved: extractor output changed"
    assert "Goods and services tax: tax invoices" in title, (
        f"CDN-0204 regression: GSTR 2000/17 renders as a bare citation: {title!r}"
    )
    assert title.startswith("GSTR 2000/17"), title


def test_every_ruling_with_a_description_renders_it():
    """Invariant across the whole corpus — fails with 365 bare titles before the fix."""
    offenders = [x["citation"] for x in load_rulings() if not _renders_description(x)]
    assert not offenders, (
        f"{len(offenders)} rulings render a bare citation, e.g. {offenders[:10]}"
    )


def test_bare_citation_titles_stay_at_the_pre_regression_level():
    """332 rulings are legitimately bare (their description is the citation/stem)."""
    bare = [
        x["citation"]
        for x in load_rulings()
        if _norm(_strip_withdrawn(_tree_title(x))) == _norm(x.get("citation_display", x["citation"]))
    ]
    assert len(bare) <= 332, f"{len(bare)} bare tree titles (pre-regression level 332): {bare[:10]}"
