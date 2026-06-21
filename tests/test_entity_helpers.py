from entity_helpers import find_matches, normalize_name, parse_full_name_fields


def test_normalize_name_strips_accents() -> None:
    assert normalize_name("José María") == "jose maria"


def test_parse_full_name_individual() -> None:
    p = parse_full_name_fields("Juan Perez Garcia", "individual")
    assert p["first_name"] == "Juan"
    assert p["last_name"] == "Perez Garcia"


def test_match_rfc_only() -> None:
    candidates = [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": "ACME SA",
            "party_type": "organization",
            "party_type_label": "Persona moral",
            "legal_name": "ACME SA",
            "rfc": "ACM010101ABC",
            "curp": None,
            "report_count": 1,
        }
    ]
    m = find_matches(
        party_type="organization",
        legal_name=None,
        first_name=None,
        last_name=None,
        full_name=None,
        rfc="ACM010101ABC",
        curp=None,
        candidates=candidates,
        min_confidence=60,
    )
    assert len(m) == 1
    assert m[0]["matchType"] == "exact_identifier"


def test_match_organization_acronym_in_parentheses() -> None:
    candidates = [
        {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "name": "Ingeniería de Bombas y Controles S.A. de C.V. (IDBC)",
            "party_type": "organization",
            "party_type_label": "Persona moral",
            "legal_name": "INGENIERIA DE BOMBAS Y CONTROLES SA DE CV",
            "full_name": "Ingeniería de Bombas y Controles S.A. de C.V. (IDBC)",
            "rfc": None,
            "curp": None,
            "report_count": 2,
        }
    ]
    m = find_matches(
        party_type="organization",
        legal_name="IDBC",
        first_name=None,
        last_name=None,
        full_name="IDBC",
        rfc=None,
        curp=None,
        candidates=candidates,
        min_confidence=70,
    )
    assert len(m) == 1
    assert m[0]["matchType"] == "exact_name"
    assert m[0]["confidence"] >= 90
