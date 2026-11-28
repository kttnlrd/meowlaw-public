"""A query that is nothing but a ruling citation resolves to that document (real corpus)."""
import re

import pytest

from backend.routes.search import _cited_ruling, _withdrawal_index

SERIES = ["AID", "CR", "PR", "TR", "TD", "IT", "TA", "MT", "GSTR", "PS_LA", "LCG", "PCG", "SGR"]


def _forms(series):
    """Every form of one real document of the series: corpus ref, own citation, each ± 'W'."""
    rec = next(v for k, v in _withdrawal_index().items()
               if re.match(rf"{series}_\d", k))
    base = {rec["derived_ref"], re.sub(r"W$", "", rec["citation_alt"] or rec["derived_ref"])}
    return rec, sorted(base | {f + "W" for f in base})


@pytest.mark.parametrize("series", SERIES)
def test_every_series_resolves_to_itself(series):
    rec, forms = _forms(series)
    for q in forms:
        hit = _cited_ruling(q)
        assert hit and hit[0] is rec, q
        assert hit[1]["section"] == rec["derived_ref"] and hit[1]["act"] == "rulings", q


@pytest.mark.parametrize("q, ref", [
    ("ATO ID 2001/1", "AID 2001/1"), ("PSLA 2003/10", "PS LA 2003/10"),
    ("TD 93/12", "TD 1993/12"), ("td 93/12w", "TD 1993/12"),
])
def test_known_forms(q, ref):
    assert _cited_ruling(q)[1]["section"] == ref


@pytest.mark.parametrize("q", ["ATO ID 2001", "CR 2002", "TR 98", "2001", "26AH", "118-110",
                               "s 160AAB", "TR 2004/1 deductions", "W", "",
                               "capital gains small business concessions"])
def test_lookalikes_do_not_fire(q):
    assert _cited_ruling(q) is None
