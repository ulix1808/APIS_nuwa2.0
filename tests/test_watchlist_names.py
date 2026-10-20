"""Name and ID rules of the early-warning match (same rules as the BFF entity matcher)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

import watchlist_names as wn  # noqa: E402


def test_company_tokens_drop_legal_forms_anywhere():
    assert wn.company_tokens("Green Agropacific S.P.R. de R.L. de C.V.") == ["green", "agropacific"]
    assert wn.company_tokens("GREEN AGROPACIFIC SPR DE RL DE CV") == ["green", "agropacific"]
    assert wn.company_tokens("CIBanco S.A., Institution de Banca Multiple (CIBanco)") == ["cibanco", "cibanco"]
    assert wn.company_tokens("DISTRIBUIDORA IMPERIAL DE BAJA CALIFORNIA, S.A. DE C.V.") == [
        "distribuidora",
        "imperial",
        "baja",
        "california",
    ]
    assert wn.company_tokens("Grupo Control") == ["grupo", "control"]


def test_every_token_of_the_searched_name_must_be_in_the_list_name():
    green = wn.company_tokens("GREEN AGROPACIFIC S.P.R. DE R.L. DE C.V.")
    assert wn.match_tokens(wn.company_tokens("Green Agropacific"), green) == "exact"
    assert wn.match_tokens(wn.company_tokens("Green Agro"), green) is None
    amateq = wn.company_tokens("AGROPECUARIA AMATEQ DEL VALLE S.A. DE C.V.")
    assert wn.match_tokens(wn.company_tokens("Agropecuaria Amateq"), amateq) == "exact"
    assert wn.match_tokens(wn.company_tokens("Agropecuaria Amateq del Valle Norte"), amateq) is None


def test_people_any_order_each_token_once():
    ofac = wn.person_tokens("CISNEROS TAPIA, Liliana")
    assert wn.match_tokens(wn.person_tokens("Liliana Cisneros Tapia"), ofac) == "exact"
    assert wn.match_tokens(wn.person_tokens("Liliana Cisneros Cisneros"), ofac) is None
    assert wn.match_tokens(wn.person_tokens("Ana Cisneros"), ofac) is None


def test_one_letter_off_is_probable():
    ofac = wn.person_tokens("Liliana Cisneros Tapia")
    assert wn.match_tokens(wn.person_tokens("Liliana Sisneros Tapia"), ofac) == "fuzzy"
    assert wn.match_tokens(wn.person_tokens("Lilliana Cisneros Tapia"), ofac) == "fuzzy"
    # Two tokens off, or a short token off, is not a match.
    assert wn.match_tokens(wn.person_tokens("Lilian Sisneros Tapia"), ofac) is None
    assert wn.match_tokens(wn.person_tokens("Juan Ruis"), wn.person_tokens("Juan Ruiz")) is None
    green = wn.company_tokens("Green Agropacific")
    assert wn.match_tokens(wn.company_tokens("Green Agropasific"), green) == "fuzzy"


def test_compact_names_are_exact():
    assert wn.match_tokens(wn.company_tokens("Mefra Fletes"), wn.company_tokens("MEFRAFLETES S.A. DE C.V.")) == "exact"


def test_searchable_names():
    assert not wn.is_searchable(["bajio"], "organization")
    assert wn.is_searchable(["cibanco"], "organization")
    assert not wn.is_searchable(["liliana"], "individual")
    assert wn.is_searchable(["liliana", "cisneros"], "individual")


def test_rfc_with_and_without_homoclave():
    assert wn.same_rfc("AUAM530929", "AUAM530929AB1")
    assert wn.same_rfc("DIB771110HQ1", "DIB771110HQ1")
    assert wn.same_rfc("DIB771110", "DIB771110HQ1")
    assert not wn.same_rfc("AUAM530929AB1", "AUAM530929XY2")
    assert not wn.same_rfc("DIB771110HQ1", "DIB771111HQ1")


def test_ids_confirm_or_rule_out():
    record = wn.IdSet.build(other=[("curp", "CITL801109MMNSPL07"), ("passport", "G123")])
    assert wn.compare_ids(wn.IdSet.build(curp=["citl801109mmnspl07"]), record).confirmed_by == "curp"
    other_curp = wn.compare_ids(wn.IdSet.build(curp=["CITL801109MMNSPL08"]), record)
    assert other_curp.confirmed_by is None and other_curp.conflict
    # RFC of the person vs CURP on the list: same 10 first characters is only a hint.
    hint = wn.compare_ids(wn.IdSet.build(rfc=["CITL801109AB1"]), record)
    assert hint.confirmed_by is None and hint.weak_rfc_curp and not hint.conflict
    other_person = wn.compare_ids(wn.IdSet.build(rfc=["CITL801110AB1"]), record)
    assert other_person.conflict
    # A Mexican "Tax ID No." on the list is an RFC.
    assert wn.compare_ids(wn.IdSet.build(rfc=["DIB771110HQ1"]), wn.IdSet.build(other=[("tax_id", "DIB771110HQ1")])).confirmed_by == "rfc"
    # Foreign IDs: same type and number confirm; same type, other number rules out.
    cedula = wn.IdSet.build(other=[("cedula", "79.876.543")])
    assert wn.compare_ids(wn.IdSet.build(other=[("cedula", "79876543")]), cedula).confirmed_by == "cedula"
    assert wn.compare_ids(wn.IdSet.build(other=[("cedula", "11111111")]), cedula).conflict
    # No IDs on either side: no verdict.
    empty = wn.compare_ids(wn.IdSet.build(), cedula)
    assert empty.confirmed_by is None and not empty.conflict


def test_generic_words_do_not_match_inside_a_long_name():
    long_name = wn.company_tokens("Resguardo De Valores Y Servicios Integrales RSVI, S.A. DE C.V.")
    assert wn.match_tokens(wn.company_tokens("Servicios Integrales"), long_name) is None
    assert wn.match_tokens(wn.company_tokens("Resguardo de Valores Servicios Integrales"), long_name) == "exact"
