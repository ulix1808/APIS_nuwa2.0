"""Niveles de riesgo del catálogo de fuentes y chunks (0–3, alineado con el front)."""

from __future__ import annotations

from typing import Any

# 0=bajo, 1=medio, 2=alto, 3=crítico (misma escala que severityToRiskLevel en el admin UI)
VALID_SOURCE_RISK_LEVELS = frozenset({0, 1, 2, 3})

RISK_LEVEL_API_MESSAGE = (
    "riskLevel debe ser 0, 1, 2 o 3 (0=bajo, 1=medio, 2=alto, 3=crítico)."
)


def is_valid_source_risk_level(value: int) -> bool:
    return value in VALID_SOURCE_RISK_LEVELS


def parse_source_risk_level(raw: Any) -> int | None:
    try:
        x = int(raw)
    except (TypeError, ValueError):
        return None
    return x if is_valid_source_risk_level(x) else None


def validate_source_risk_levels_list(values: list[int]) -> str | None:
    """None si todos son válidos; mensaje de error si no."""
    bad = [v for v in values if not is_valid_source_risk_level(v)]
    if bad:
        return RISK_LEVEL_API_MESSAGE
    return None
