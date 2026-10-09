"""Person catalog hits with a different surname are dropped from /v1/search."""

import json

import pytest

from surname_conflict_filter import extract_hit_name, filter_surname_conflicts, has_surname_conflict

SUBJECT = "Joel Hernández Hernández"
LAST = "Hernández Hernández"


def _row(chunk_id: int, fields: dict) -> dict:
    return {"id": chunk_id, "chunk_text": json.dumps(fields, ensure_ascii=False)}


@pytest.mark.parametrize(
    "hit",
    [
        "JOEL HERNANDEZ GARCIA",
        "HERNANDEZ ELERIA JOEL",
        "JOEL HERNANDEZ PEREZ",
        "DERIAN JOEL HERNANDEZ RAMIREZ",
        "CHRISTIAN JOEL HERNANDEZ FERNANDEZ",
    ],
)
def test_complete_name_with_other_surname_conflicts(hit: str) -> None:
    assert has_surname_conflict(SUBJECT, hit, LAST)


@pytest.mark.parametrize(
    "hit",
    ["JOEL HERNANDEZ", "JOEL HERNANDEZ N", "HERNANDEZ HERNANDEZ JOEL", "Joel Hernandes Hernández", "JOEL HERNANDEZ HERNANDEZ"],
)
def test_incomplete_or_same_name_is_kept(hit: str) -> None:
    assert not has_surname_conflict(SUBJECT, hit, LAST)


def test_short_spellings_of_the_same_person_are_kept() -> None:
    assert not has_surname_conflict("Emilio Ricardo Lozoya Austin", "Emilio Lozoya")
    assert not has_surname_conflict("Emilio Lozoya Austin", "Emilio Ricardo Lozoya Austin")
    assert not has_surname_conflict("María de la Luz Hernández", "Maria Luz Hernandes")


def test_two_token_subject_is_never_judged() -> None:
    assert not has_surname_conflict("Joel Hernández", "Joel Hernández Torres")


def test_name_comes_from_structured_field_only() -> None:
    sat = json.dumps({"id": "HEHJ980529BDA", "nombre": "JOEL HERNANDEZ GARCIA", "SUPUESTO": "FIRMES"})
    assert extract_hit_name(sat) == "JOEL HERNANDEZ GARCIA"
    assert extract_hit_name("nombre: JOEL HERNANDEZ GARCIA · OIC Responsable: SFP") == "JOEL HERNANDEZ GARCIA"
    assert extract_hit_name(json.dumps({"OIC Responsable": "JOEL HERNANDEZ GARCIA"})) is None
    assert extract_hit_name("Nota sobre JOEL HERNANDEZ GARCIA en el municipio") is None


def test_filter_drops_conflicts_and_keeps_the_rest() -> None:
    rows = [
        _row(1, {"id": "HEHJ980529BDA", "nombre": "JOEL HERNANDEZ HERNANDEZ", "SUPUESTO": "FIRMES"}),
        _row(2, {"id": "HEGJ000000AAA", "nombre": "JOEL HERNANDEZ GARCIA", "SUPUESTO": "FIRMES"}),
        _row(3, {"estado": "Guanajuato", "nombre": "JOEL HERNANDEZ TORRES", "cargo": "Regidor"}),
        _row(4, {"id": "AEHJ560713D54", "nombre": "JOEL HERNANDEZ", "Situación del contribuyente": "Definitivo"}),
        {"id": 5, "chunk_text": "Texto libre que menciona a JOEL HERNANDEZ GARCIA"},
    ]
    kept, excluded = filter_surname_conflicts(rows, query=SUBJECT, last_name=LAST)
    assert excluded == 2
    assert [r["id"] for r in kept] == [1, 4, 5]


def test_filter_is_a_noop_for_short_queries() -> None:
    rows = [_row(1, {"nombre": "JOEL HERNANDEZ GARCIA"})]
    assert filter_surname_conflicts(rows, query="Joel Hernández") == (rows, 0)
