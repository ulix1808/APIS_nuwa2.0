from report_helpers import extract_report_metadata, validate_report_for_save, validate_report_for_update


def test_extract_metadata_minimal() -> None:
    r = {
        "folio": "SE-1",
        "entidad": "cisco",
        "tipoConsulta": "Persona",
        "fecha": "2026-03-07",
        "hora": "03:00",
        "nivelRiesgo": "critical",
        "nivelRiesgoNumerico": 3,
        "metadatos": {
            "totalListasOriginal": 32,
            "totalListasActivas": 32,
            "totalDescartadas": 0,
            "esActualizacion": False,
        },
        "resumen": {"totalListas": 32, "totalMenciones": 10},
        "grokAnalisis": {
            "resumen": "ok",
            "falsosPositivos": 0,
            "confirmados": 10,
        },
    }
    m = extract_report_metadata(r)
    assert m["folio"] == "SE-1"
    assert m["entidad"] == "cisco"
    assert m["nivel_riesgo_numerico"] == 3
    assert m["grok_confirmados"] == 10


def test_validate_save() -> None:
    assert validate_report_for_save(1, 1, None) is not None
    assert validate_report_for_save(1, 1, {}) is not None
    assert validate_report_for_save(1, 1, {"folio": "x"}) is None


def test_validate_update() -> None:
    assert validate_report_for_update("a", {"folio": "b"}) is not None
    assert validate_report_for_update("x", {"folio": "x"}) is None
