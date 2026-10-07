"""Regression: /v1/search must not treat OIC Responsable-only matches as subject hits."""

from __future__ import annotations

import json

from oic_responsable_filter import (
    filter_oic_only_hits,
    should_exclude_oic_only_match,
)


def _sabg_chunk(
    *,
    nombre: str,
    oic: str = "PEMEX EXPLORACION Y PRODUCCION",
) -> str:
    return json.dumps(
        {
            "nombre": nombre,
            "OIC Responsable": oic,
            "Expediente": "0014/2009",
            "Ley infringida": "LAASSP",
            "Inicio de la sanción": "30-11-2010",
            "Monto de la Multa": "82,200.00",
            "Periodo de Inhabilitación": "3 meses",
        },
        ensure_ascii=False,
    )


def test_a_exclude_pemex_match_only_on_oic() -> None:
    exclude, payload = should_exclude_oic_only_match(
        query="PEMEX",
        chunk_text=_sabg_chunk(nombre="RIMEX DEL CENTRO, S.A. DE C.V."),
    )
    assert exclude is True
    assert payload is not None
    assert payload["reason"] == "match_only_on_oic_responsable"
    assert payload["query"] == "PEMEX"
    assert "RIMEX" in payload["subject_name"]
    assert "PEMEX" in payload["oic_responsable"]


def test_b_include_when_query_matches_subject_nombre() -> None:
    exclude, payload = should_exclude_oic_only_match(
        query="RIMEX DEL CENTRO",
        chunk_text=_sabg_chunk(nombre="RIMEX DEL CENTRO, S.A. DE C.V."),
    )
    assert exclude is False
    assert payload is None


def test_c_include_when_nombre_contains_query_phrase() -> None:
    nombre = (
        "BWATER COMPANY, S.A. DE C.V. Y GARNER SERVICIOS AMBIENTALES DE MEXICO, "
        "S.A. DE C.V. Y GARNER ENVIROM"
    )
    exclude, _ = should_exclude_oic_only_match(
        query="GARNER SERVICIOS AMBIENTALES DE MEXICO",
        chunk_text=_sabg_chunk(nombre=nombre),
    )
    assert exclude is False


def test_d_exclude_full_oic_name_query() -> None:
    exclude, payload = should_exclude_oic_only_match(
        query="PEMEX EXPLORACION Y PRODUCCION",
        chunk_text=_sabg_chunk(nombre="OTRA EMPRESA SANCIONADA SA DE CV"),
    )
    assert exclude is True
    assert payload is not None
    assert payload["reason"] == "match_only_on_oic_responsable"


def test_e_other_source_pemex_as_subject_not_excluded() -> None:
    """PEMEX as sanctioned subject (no OIC field / different schema) must stay."""
    chunk = json.dumps(
        {
            "nombre": "PEMEX EXPLORACION Y PRODUCCION",
            "rfc": "PEP830101XXX",
            "estado": "CDMX",
        },
        ensure_ascii=False,
    )
    exclude, _ = should_exclude_oic_only_match(query="PEMEX", chunk_text=chunk)
    assert exclude is False


def test_e_plain_text_pemex_subject_without_oic_field() -> None:
    exclude, _ = should_exclude_oic_only_match(
        query="PEMEX",
        chunk_text="PEMEX EXPLORACION Y PRODUCCION sancionado por LAASSP expediente 123",
    )
    assert exclude is False


def test_keep_when_query_matches_both_nombre_and_oic() -> None:
    exclude, _ = should_exclude_oic_only_match(
        query="PEMEX",
        chunk_text=_sabg_chunk(
            nombre="PEMEX LOGISTICA SA DE CV",
            oic="PEMEX EXPLORACION Y PRODUCCION",
        ),
    )
    assert exclude is False


def test_plain_delimited_chunk_format() -> None:
    chunk = (
        "nombre: RIMEX DEL CENTRO, S.A. DE C.V. · "
        "OIC Responsable: PEMEX EXPLORACION Y PRODUCCION · "
        "Expediente: 0004/2011"
    )
    exclude, payload = should_exclude_oic_only_match(query="PEMEX", chunk_text=chunk)
    assert exclude is True
    assert payload is not None


def test_filter_oic_only_hits_batch() -> None:
    rows = [
        {
            "id": "1",
            "chunk_text": _sabg_chunk(nombre="RIMEX DEL CENTRO, S.A. DE C.V."),
        },
        {
            "id": "2",
            "chunk_text": _sabg_chunk(nombre="RIMEX DEL CENTRO, S.A. DE C.V."),
        },
        {
            "id": "3",
            "chunk_text": json.dumps({"nombre": "PEMEX REFINACION"}, ensure_ascii=False),
        },
    ]
    # Query PEMEX: drop OIC-only rows, keep real PEMEX subject
    kept, n = filter_oic_only_hits(rows, query="PEMEX")
    assert n == 2
    assert len(kept) == 1
    assert kept[0]["id"] == "3"

    # Query RIMEX: keep SABG rows
    kept2, n2 = filter_oic_only_hits(rows[:2], query="RIMEX DEL CENTRO")
    assert n2 == 0
    assert len(kept2) == 2


def test_normalization_accents_and_case() -> None:
    chunk = json.dumps(
        {
            "nombre": "Empresa Áé",
            "OIC Responsable": "Pemex Exploración y Producción",
        },
        ensure_ascii=False,
    )
    exclude, _ = should_exclude_oic_only_match(query="pemex exploración", chunk_text=chunk)
    assert exclude is True
