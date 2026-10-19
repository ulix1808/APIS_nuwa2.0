"""Unit tests for source_import_core (no network, no database)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "cdk" / "lambdas"
sys.path.insert(0, str(ROOT))

import source_import_core as core  # noqa: E402


def _csv(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def test_csv_keeps_day_month_dates_and_leading_zeros():
    data = _csv("RFC,FECHA,FOLIO\nA&A0401096P2,01/04/2017,000123\n")
    headers, rows = core.table_from_matrix(core.read_matrix(data, "sat.csv"), 0)
    assert headers == ["RFC", "FECHA", "FOLIO"]
    assert rows == [{"RFC": "A&A0401096P2", "FECHA": "01/04/2017", "FOLIO": "000123"}]


def test_windows_1252_csv_and_semicolons():
    data = _csv("RFC;NOMBRE\nAIPS681210IM4;SAUL ARIAS PEÑA\n", "cp1252")
    headers, rows = core.table_from_matrix(core.read_matrix(data, "x.csv"), 0)
    assert rows[0]["NOMBRE"] == "SAUL ARIAS PEÑA"


def test_xlsx_date_cells_as_day_month_year():
    import datetime as dt

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["RFC", "Fecha", "Monto"])
    ws.append(["AAA010528159", dt.datetime(2018, 5, 10), 773.0])
    buf = io.BytesIO()
    wb.save(buf)
    headers, rows = core.table_from_matrix(core.read_matrix(buf.getvalue(), "x.xlsx"), 0)
    assert rows == [{"RFC": "AAA010528159", "Fecha": "10/05/2018", "Monto": "773"}]


def test_header_row_index_and_duplicate_headers():
    m = [["Reporte SAT"], ["RFC", "Nombre", "Nombre", ""], ["X1", "A", "B", "C"]]
    headers, rows = core.table_from_matrix(m, 1)
    assert headers == ["RFC", "Nombre", "Nombre_2", "columna_4"]
    assert rows[0]["Nombre_2"] == "B"


def test_build_records_like_the_browser():
    headers = ["RFC", "Paterno", "Materno", "Nombres", "Monto", "Otra"]
    rows = [{"RFC": "PEJ850101", "Paterno": "Pérez", "Materno": "Núñez", "Nombres": "José", "Monto": "$1,000.00", "Otra": "x"}]
    mapping = {"RFC": "id", "Paterno": "nombre", "Materno": "nombre", "Nombres": "nombre", "Monto": "custom:Monto", "Otra": "ignorar"}
    out = core.build_records(headers, rows, mapping, "id")
    rec = out.records[0]
    obj = json.loads(rec.chunk_text)
    # same key order and text as the admin page + backend prepare_chunk_text_for_storage
    assert obj == {"id": "PEJ850101", "nombre": "Perez Nuñez Jose", "Monto": "$1,000.00", "chunk_text_normalized": rec.normalized}
    assert rec.key == "PEJ850101"


def test_same_record_in_another_format_has_the_same_hash():
    a = {"id": "AIPS681210IM4", "nombre": "SAUL ARIAS PEÑA", "MONTO": "773", "FECHA DE PUBLICACION": "01/02/2016", "X": ""}
    b = {"id": "AIPS681210IM4", "nombre": "Saul Arias Peña", "MONTO": "$773.00", "FECHA DE PUBLICACIÓN": "2016-02-01"}
    assert core.row_hash(a) == core.row_hash(b)
    c = dict(b, MONTO="$774.00")
    assert core.row_hash(c) != core.row_hash(b)


def test_folios_with_leading_zeros_are_not_numbers():
    assert core.row_hash({"folio": "0045"}) != core.row_hash({"folio": "45"})


def test_repairs_mac_roman_damage_in_headers_and_cells():
    headers = ["RFC", "Nombre", "PublicaciÛn"]
    rows = [
        {"RFC": "A1", "Nombre": "SAUL ARIAS PE—A", "PublicaciÛn": "10/05/2018"},
        {"RFC": "A2", "Nombre": "DISE„O DE M…XICO", "PublicaciÛn": ""},
    ]
    out = core.build_records(headers, rows, {"RFC": "id", "Nombre": "nombre", "Publicación": "custom:Publicación"}, "id")
    assert out.damage == "mac"
    names = [json.loads(r.chunk_text)["nombre"] for r in out.records]
    assert names == ["SAUL ARIAS PEÑA", "DISEÑO DE MEXICO"]
    assert json.loads(out.records[0].chunk_text)["Publicación"] == "10/05/2018"
    assert out.unmatched_mapping == []


def test_last_row_wins_for_a_repeated_key():
    headers = ["RFC", "Monto"]
    rows = [{"RFC": "a-1", "Monto": "1"}, {"RFC": "B2", "Monto": "2"}, {"RFC": "A1", "Monto": "3"}]
    out = core.build_records(headers, rows, {"RFC": "id", "Monto": "custom:Monto"}, "id")
    assert [(r.key, json.loads(r.chunk_text)["Monto"]) for r in out.records] == [("B2", "2"), ("A1", "3")]
    assert out.duplicates_in_file == 1


def test_without_key_identical_rows_collapse_and_different_ones_stay():
    headers = ["Nombre", "Expediente"]
    rows = [{"Nombre": "ACME", "Expediente": "1/2025"}, {"Nombre": "ACME", "Expediente": "1/2025"}, {"Nombre": "ACME", "Expediente": "3/2025"}]
    out = core.build_records(headers, rows, {"Nombre": "nombre", "Expediente": "custom:Expediente"}, None)
    assert len(out.records) == 2 and all(r.key.startswith("h:") for r in out.records)


def test_key_and_hash_of_stored_matches_new_records():
    headers = ["RFC", "Nombre"]
    out = core.build_records(headers, [{"RFC": "X1", "Nombre": "ACME"}], {"RFC": "id", "Nombre": "nombre"}, "id")
    rec = out.records[0]
    assert core.key_and_hash_of_stored(rec.chunk_text, "id") == (rec.key, rec.digest)


def test_json_records():
    data = json.dumps({"data": [{"nit": "123", "nombre": "A", "monto": 5}, {"nit": "", "nombre": ""}]}).encode()
    headers, rows = core.read_json_records(data)
    assert headers == ["nit", "nombre", "monto"]
    assert rows == [{"nit": "123", "nombre": "A", "monto": "5"}]


def test_strict_import_spots_new_columns_against_the_saved_mapping():
    import types

    sys.modules.setdefault("boto3", types.ModuleType("boto3"))  # provided by the Lambda runtime
    import handler_source_import_worker as w

    mapping = {"RFC": "id", "Nombre": "nombre", "Publicación DOF": "custom:Publicación DOF"}
    # Repaired / differently cased headers are the same column; "NUEVA" is not in the mapping.
    assert w._extra_columns(["rfc", "NOMBRE", "PublicaciÛn DOF", "NUEVA"], mapping) == ["NUEVA"]
    assert w._extra_columns(["RFC", "Nombre", "Publicación DOF"], mapping) == []


def test_repairs_u_umlaut_and_u_acute_misread_as_mac_roman():
    assert core.repair_text("T‹V S‹D", "mac") == "TÜV SÜD"
    assert core.repair_text("JES⁄S", "mac") == "JESÚS"
