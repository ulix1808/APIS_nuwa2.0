"""Resolución de tenant a partir de la API Key de API Gateway (x-api-key)."""

from __future__ import annotations

import os
from typing import Any

from nuwa_supabase import rest_json


def request_api_key_id(event: dict[str, Any]) -> str | None:
    ident = (event.get("requestContext") or {}).get("identity") or {}
    kid = ident.get("apiKeyId")
    return kid if kid else None


def get_bound_client_id(event: dict[str, Any]) -> int | None:
    """
    Si la petición usa una API key de compañía (registrada en companies.apigw_key_id),
    devuelve ese client_id. Si es la key de plataforma o una key no mapeada, devuelve None
    (comportamiento previo: se confía en clientId del body/query salvo otras validaciones).
    """
    kid = request_api_key_id(event)
    if not kid:
        return None
    platform = (os.environ.get("NUWA_PLATFORM_API_KEY_ID") or "").strip()
    if platform and kid == platform:
        return None
    rows = rest_json(
        "GET",
        "companies",
        query=f"apigw_key_id=eq.{kid}&select=client_id&limit=1",
    )
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    return int(row["client_id"])


def tenant_client_mismatch_message() -> str:
    return "El clientId no corresponde a la API key (tenant)."


def assert_body_client_matches_key(event: dict[str, Any], client_id: int) -> str | None:
    """Devuelve mensaje de error si hay conflicto; None si OK."""
    bound = get_bound_client_id(event)
    if bound is None:
        return None
    if bound != client_id:
        return tenant_client_mismatch_message()
    return None
