"""Autenticación de invocaciones API: JWT obligatorio (sin API Key en gateway)."""

from __future__ import annotations

from typing import Any

from nuwa_jwt import jwt_claims_from_event, jwt_int


def require_jwt(event: dict[str, Any]) -> dict[str, Any] | str:
    """
    Valida Authorization: Bearer. Devuelve claims o mensaje de error (para 401).
    Claims: sub (user id), cid (client_id), role (slug).
    """
    claims = jwt_claims_from_event(event)
    if not claims:
        return "Se requiere Authorization: Bearer <accessToken> (login en /v1/auth/login)."
    try:
        jwt_int(claims, "sub")
        jwt_int(claims, "cid")
    except (ValueError, TypeError):
        return "Token inválido."
    role = claims.get("role")
    if not role or not isinstance(role, str):
        return "Token inválido."
    return claims


def jwt_allows_client(claims: dict[str, Any], body_or_query_client_id: int) -> bool:
    """super_admin puede actuar sobre cualquier clientId; resto solo el suyo."""
    if claims.get("role") == "super_admin":
        return True
    try:
        return jwt_int(claims, "cid") == body_or_query_client_id
    except (ValueError, TypeError):
        return False


def effective_tenant_scope(claims: dict[str, Any]) -> int | None:
    """None si super_admin (sin filtro forzado por tenant); si no, client_id del token."""
    if claims.get("role") == "super_admin":
        return None
    try:
        return jwt_int(claims, "cid")
    except (ValueError, TypeError):
        return None


def jwt_matches_actor_body(claims: dict[str, Any], body: dict[str, Any]) -> bool:
    """Alinea JWT con clientId/userId del body (admin)."""
    try:
        uid = int(body.get("userId"))
        cid = int(body.get("clientId"))
    except (TypeError, ValueError):
        return False
    try:
        return jwt_int(claims, "sub") == uid and jwt_int(claims, "cid") == cid
    except (ValueError, TypeError):
        return False
