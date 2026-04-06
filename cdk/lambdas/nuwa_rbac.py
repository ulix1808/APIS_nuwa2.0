"""Reglas de acceso a reportes y datos por rol."""

from __future__ import annotations

from typing import Any


def can_read_report(actor: dict[str, Any], report: dict[str, Any]) -> bool:
    slug = actor["role_slug"]
    if slug == "super_admin":
        return True
    if int(report["client_id"]) != actor["client_id"]:
        return False
    if slug == "admin":
        return True
    if slug == "user":
        return int(report["created_by_user_id"]) == actor["id"]
    return False


def reports_list_query_parts(
    actor: dict[str, Any],
    *,
    filter_client_id: int | None,
    filter_created_by_user_id: int | None,
) -> list[str]:
    """Fragmentos and para PostgREST; lista vacía si el actor no puede aplicar el filtro."""
    slug = actor["role_slug"]
    parts: list[str] = ["status=neq.deleted"]
    if slug == "super_admin":
        if filter_client_id is not None:
            parts.append(f"client_id=eq.{filter_client_id}")
        if filter_created_by_user_id is not None:
            parts.append(f"created_by_user_id=eq.{filter_created_by_user_id}")
    elif slug == "admin":
        parts.append(f"client_id=eq.{actor['client_id']}")
        if filter_created_by_user_id is not None:
            parts.append(f"created_by_user_id=eq.{filter_created_by_user_id}")
    else:
        parts.append(f"client_id=eq.{actor['client_id']}")
        parts.append(f"created_by_user_id=eq.{actor['id']}")
        if filter_created_by_user_id is not None and filter_created_by_user_id != actor["id"]:
            raise PermissionError("user cannot filter other creators")
    return parts


def can_manage_company(actor: dict[str, Any], target_client_id: int) -> bool:
    if actor["role_slug"] == "super_admin":
        return True
    return actor["role_slug"] == "admin" and actor["client_id"] == target_client_id


def can_manage_users(actor: dict[str, Any], target_client_id: int) -> bool:
    if actor["role_slug"] == "super_admin":
        return True
    return actor["role_slug"] == "admin" and actor["client_id"] == target_client_id
