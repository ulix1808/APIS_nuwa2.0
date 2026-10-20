"""Match decision of the early warning over real OFAC records (fixtures)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

import watchlist_index as wi  # noqa: E402
import watchlist_ofac as ofac  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ofac"


def _index() -> wi.ListIndex:
    _, sdn = ofac.parse_advanced_xml(str(FIXTURES / "SDN_SAMPLE.XML"))
    _, cons = ofac.parse_advanced_xml(str(FIXTURES / "CONS_SAMPLE.XML"))
    rows = [
        {
            "ext_id": r["extId"],
            "party_type": r["partyType"],
            "name": r["name"],
            "aliases": r["aliases"],
            "details": {"ids": r["ids"], "linked": r["linked"]},
            "lists": r["lists"],
            "programs": r["programs"],
            "designated_on": r["designatedOn"],
            "content_hash": r["contentHash"],
        }
        for r in ofac.merge_records(sdn, cons)
    ]
    return wi.ListIndex(wi.ofac_record_from_row(row) for row in rows)


IDX = _index()


def _match(name, party_type, rfc=None, curp=None, other=None, aliases=()):
    subject = wi.Subject("entity", name, party_type, wi.subject_ids(rfc, curp, other), list(aliases))
    return [(m.record.ref, m.matched_by, m.severity) for m in IDX.match(subject)]


def test_company_by_name_without_legal_form():
    assert _match("Green Agropacific", "organization") == [("58020", "name", "high")]
    assert _match("GREEN AGROPACIFIC SPR DE RL DE CV", "organization") == [("58020", "name", "high")]


def test_company_one_letter_off_is_medium():
    assert _match("Green Agropasific", "organization") == [("58020", "name_fuzzy", "medium")]


def test_company_by_alias_of_the_list():
    assert _match("Distribuidora Imperial", "organization") == [("7156", "name", "high")]


def test_company_rfc_decides():
    # Same RFC, whatever the name.
    assert _match("DIBC Comercial", "organization", rfc="DIB771110HQ1") == [("7156", "rfc", "high")]
    # Same name, other RFC: homonym.
    assert _match("Distribuidora Imperial de Baja California", "organization", rfc="XYZ990101AB1") == []


def test_person_needs_an_id():
    assert _match("Liliana Cisneros Tapia", "individual") == []
    assert _match("Liliana Cisneros Tapia", "individual", curp="CITL801109MMNSPL07") == [("58021", "curp", "high")]
    assert _match("Liliana Cisneros Tapia", "individual", curp="CITL801109MMNSPL99") == []
    # RFC whose first 10 characters are the CURP's, plus the full name: probable.
    assert _match("Liliana Cisneros Tapia", "individual", rfc="CITL801109AB1") == [("58021", "rfc_curp", "medium")]
    assert _match("Liliana Cisneros", "individual", rfc="CITL801109AB1") == [("58021", "rfc_curp", "medium")]
    assert _match("Ana Cisneros", "individual", rfc="CITL801109AB1") == []


def test_person_rfc_without_homoclave_on_the_list():
    assert _match("Miguel Aguilar Amao", "individual", rfc="AUAM530929KQ2") == [("7163", "rfc", "high")]


def test_foreign_id_from_the_other_identifier_field():
    rec = next(r for r in IDX.records if r.ref == "9647")
    if not rec.ids.other:
        return
    id_type, value = sorted(rec.ids.other)[0]
    assert _match("Cualquier Nombre", "individual", other=[{"type": id_type, "value": value}])[0][1] == id_type
    assert _match("Cualquier Nombre", "individual", other=[{"type": "other", "value": value}])[0][1] in (id_type, "id")


def test_people_and_companies_do_not_cross():
    assert _match("Green Agropacific", "individual", rfc="GRAG800101AB1") == []


def test_unknown_party_type_partners_match_companies_by_name():
    assert _match("Green Agropacific", "unknown") == [("58020", "name", "high")]
    assert _match("Liliana Cisneros Tapia", "unknown") == []


def test_weak_aliases_are_not_used():
    zahhar = next(r for r in IDX.records if r.ref == "9647")
    assert "Mahmoud Zahar" not in zahhar.names
    assert "Mahmoud Khaled AL ZAHAR" in zahhar.names


def test_vessels_are_not_matched():
    assert all(r.party_type in ("individual", "organization", "other") for r in IDX.records)
    assert _match("Mar Azul", "organization") == []
