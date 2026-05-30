from source_risk_level import (
    RISK_LEVEL_API_MESSAGE,
    parse_source_risk_level,
    validate_source_risk_levels_list,
)


def test_parse_valid_levels() -> None:
    for v in (0, 1, 2, 3):
        assert parse_source_risk_level(v) == v
        assert parse_source_risk_level(str(v)) == v


def test_parse_invalid() -> None:
    assert parse_source_risk_level(4) is None
    assert parse_source_risk_level(-1) is None
    assert parse_source_risk_level("x") is None
    assert parse_source_risk_level(None) is None


def test_validate_list() -> None:
    assert validate_source_risk_levels_list([0, 3]) is None
    assert validate_source_risk_levels_list([1, 2, 3]) is None
    assert validate_source_risk_levels_list([1, 9]) == RISK_LEVEL_API_MESSAGE
