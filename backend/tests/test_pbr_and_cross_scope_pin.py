"""A bare PBR authorisation number resolves to its own ruling; an exact section
citation pins even when its Act is outside the active scope (real corpus)."""
import random
import sqlite3

import pytest
from fastapi import HTTPException

from backend.routes.search import _ANY_TAG, DOMAIN_ACTS, search, search_hybrid
from backend.services.vector_search_service import EMBEDDINGS_DB

ENDPOINTS = {
    "hybrid": lambda q, **kw: search_hybrid(None, q, **kw),
    "search": lambda q, **kw: search(None, q, **kw),
}


def _pbr_ids() -> set[str]:
    conn = sqlite3.connect(f"file:{EMBEDDINGS_DB}?mode=ro", uri=True)
    try:
        return {s for (s,) in conn.execute("SELECT DISTINCT section FROM embeddings WHERE act = 'private'")}
    finally:
        conn.close()


PBR_IDS = _pbr_ids()
_rng = random.Random(20261010)
REAL = ["1012101440404", "1051441442547"] + _rng.sample(sorted(PBR_IDS), 3)
FAKE = []
while len(FAKE) < 4:
    n = str(_rng.randrange(10**12, 10**13))
    if n not in PBR_IDS:
        FAKE.append(n)


@pytest.mark.parametrize("ep", ENDPOINTS)
@pytest.mark.parametrize("pbr", REAL)
def test_real_pbr_id_resolves_first_exact(ep, pbr):
    top = ENDPOINTS[ep](pbr)["results"][0]
    assert (top["act"], top["section"], top.get("exact_match")) == ("private-rulings", pbr, True)
    assert top["source_type"] == "private_ruling"
    assert top["title"].startswith(f"Private ruling {pbr}")  # the row identifies itself
    assert len(top["snippet"]) > 20 and not _ANY_TAG.search(top["snippet"]) and top["snippet"] != top["title"]


@pytest.mark.parametrize("pbr", REAL[:2])
def test_pbr_slicer_pins_the_requested_id(pbr):
    top = search_hybrid(None, pbr, type="private_ruling")["results"][0]
    assert (top["section"], top.get("exact_match")) == (pbr, True)


@pytest.mark.parametrize("ep", ENDPOINTS)
@pytest.mark.parametrize("fake", FAKE)
def test_fake_pbr_id_matches_nothing(ep, fake):
    assert ENDPOINTS[ep](fake)["results"] == []


CROSS_SCOPE = [
    ("s RA-12 NZ Income Tax Act 2007", ("nz-it-2007", "RA-12")),
    ("s HG-3 NZ Income Tax Act 2007", ("nz-it-2007", "HG-3")),
    ("s 738ZI Corporations Act 2001", ("corporations-act-2001", "738ZI")),
    ("s 194 AML/CTF Act 2006", ("aml-ctf-2006", "194")),
    ("s 5.2.3 AML/CTF Rules 2007", ("aml-ctf-rules-2007", "5.2.3")),
]


@pytest.mark.parametrize("ep", ENDPOINTS)
@pytest.mark.parametrize("q,key", CROSS_SCOPE)
def test_named_act_citation_pins_outside_default_scope(ep, q, key):
    top = ENDPOINTS[ep](q)["results"][0]
    assert (top["act"], top["section"], top.get("exact_match")) == (*key, True)


@pytest.mark.parametrize("ep", ENDPOINTS)
@pytest.mark.parametrize("q", ["anti-money laundering customer due diligence", "directors duties insolvent trading",
                               "New Zealand income tax residence"])
def test_topical_query_respects_default_scope(ep, q):
    rows = ENDPOINTS[ep](q)["results"]
    assert rows or ep == "search"  # /api/search is AND-only FTS: an empty page is in scope too
    assert {r["act"] for r in rows} <= DOMAIN_ACTS["au-tax"]


@pytest.mark.parametrize("ep", ENDPOINTS)
def test_out_of_scope_act_filter_still_422(ep):
    with pytest.raises(HTTPException) as e:
        ENDPOINTS[ep]("s 194 AML/CTF Act 2006", act="aml-ctf-2006")
    assert e.value.status_code == 422
