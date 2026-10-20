"""OFAC Advanced XML reader. Fixtures are real records cut out of the 15 Oct 2026 publication."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

import watchlist_ofac as ofac  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ofac"


def _sdn():
    return ofac.parse_advanced_xml(str(FIXTURES / "SDN_SAMPLE.XML"))


def _by_id(records):
    return {r["extId"]: r for r in records}


def test_reads_the_publication_date_and_every_party():
    meta, records = _sdn()
    assert meta["dateOfIssue"] == "2026-10-15"
    assert meta["parties"] == 9
    assert [r["extId"] for r in records] == ["2679", "4238", "7156", "7163", "9647", "58017", "58020", "58021", "58022"]


def test_green_agropacific_company_record():
    rec = _by_id(_sdn()[1])["58020"]
    assert rec["name"] == "Green Agropacific S.P.R. de R.L. de C.V."
    assert rec["partyType"] == "organization"
    assert rec["lists"] == ["SDN List"]
    assert rec["programs"] == ["ILLICIT-DRUGS-EO14059", "SDGT"]
    assert rec["designatedOn"] == "2026-07-23"
    assert rec["ids"] == [
        {"type": "folio_mercantil", "label": "Folio Mercantil No.", "value": "2023064007", "raw": "2023064007", "country": "Mexico"}
    ]
    assert rec["features"]["Organization Established Date"] == ["2023-06-30"]
    linked = {(l["extId"], l["direction"]) for l in rec["linked"]}
    assert ("58017", "to") in linked  # Owned or Controlled By Miguel Angel Ayala Botello
    assert ("58021", "from") in linked and ("58022", "from") in linked


def test_person_with_curp_birth_date_and_nationality():
    rec = _by_id(_sdn()[1])["58021"]
    assert rec["name"] == "Liliana Cisneros Tapia"
    assert rec["partyType"] == "individual"
    assert rec["ids"][0]["type"] == "curp" and rec["ids"][0]["value"] == "CITL801109MMNSPL07"
    assert rec["birthDates"] == ["1980-11-09"]
    assert rec["nationalities"] == ["Mexico"]
    assert rec["features"]["Gender"] == ["Female"]
    assert rec["linked"] == [
        {"extId": "58020", "name": "Green Agropacific S.P.R. de R.L. de C.V.", "relation": "Acting for or on behalf of", "direction": "to"}
    ]


def test_company_with_rfc_and_aliases():
    rec = _by_id(_sdn()[1])["7156"]
    assert rec["name"] == "DISTRIBUIDORA IMPERIAL DE BAJA CALIFORNIA, S.A. DE C.V."
    assert {"type": "rfc", "value": "DIB771110HQ1"}.items() <= next(i for i in rec["ids"] if i["type"] == "rfc").items()
    names = [a["name"] for a in rec["aliases"]]
    assert "DISTRIBUIDORA IMPERIAL" in names


def test_person_rfc_without_homoclave_and_people_in_natural_order():
    rec = _by_id(_sdn()[1])["7163"]
    assert rec["name"] == "Miguel AGUILAR AMAO"
    assert any(i["type"] == "rfc" and i["value"] == "AUAM530929" for i in rec["ids"])


def test_vessels_and_birth_date_ranges():
    by = _by_id(_sdn()[1])
    assert by["4238"]["partyType"] == "vessel"
    assert "1938-01-01/1938-12-31" in by["2679"]["birthDates"]


def test_only_latin_names_and_weak_aliases_flagged():
    for rec in _sdn()[1]:
        for alias in rec["aliases"]:
            assert any("a" <= c.lower() <= "z" for c in alias["name"])
            assert alias["type"] in ("aka", "fka", "nka")
    zahhar = _by_id(_sdn()[1])["9647"]
    assert ("Mahmoud Khaled AL ZAHAR", False) in [(a["name"], a["lowQuality"]) for a in zahhar["aliases"]]
    assert ("Mahmoud Zahar", True) in [(a["name"], a["lowQuality"]) for a in zahhar["aliases"]]


def test_sdn_and_consolidated_merge_by_fixed_ref():
    _, sdn = _sdn()
    meta, cons = ofac.parse_advanced_xml(str(FIXTURES / "CONS_SAMPLE.XML"))
    assert meta["parties"] == 2
    merged = _by_id(ofac.merge_records(sdn, cons))
    assert len(merged) == 10
    assert {"SDN List", "Consolidated List"} <= set(merged["9647"]["lists"])
    assert merged["15268"]["lists"] and "SDN List" not in merged["15268"]["lists"]


def test_content_hash_changes_only_with_content():
    _, sdn = _sdn()
    _, again = _sdn()
    assert [r["contentHash"] for r in sdn] == [r["contentHash"] for r in again]
    rec = copy.deepcopy(_by_id(sdn)["58020"])
    rec["programs"].append("SDNTK")
    assert ofac.finalize_record(rec)["contentHash"] != _by_id(sdn)["58020"]["contentHash"]


def test_id_types_and_values():
    assert ofac.id_type_of("R.F.C.") == "rfc" and ofac.id_type_of("RFC") == "rfc"
    assert ofac.id_type_of("Cedula No.") == "cedula"
    assert ofac.id_type_of("NIT #") == "tax_id"
    assert ofac.id_type_of("Matricula Mercantil No") == "matricula_mercantil_no"
    assert ofac.normalize_id_value(" citl-801109 mmnspl07 ") == "CITL801109MMNSPL07"
