"""Catálogo sources: Postgres directo (nuwa_pg_dispatch) o PostgREST (Supabase)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from nuwa_config import is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_obs_log import log_phase
from nuwa_pg_dispatch import can_mutate_source_row, source_row_to_api
from nuwa_supabase import rest_json


def _nuwa_platform_actor(client_id: int, user_id: int) -> bool:
    return client_id == 1 and user_id == 1


def resolve_create_visibility(client_id: int, user_id: int, requested: str) -> str:
    """Admin Nuwa (clientId=1, userId=1): siempre public (OpenAPI)."""
    if _nuwa_platform_actor(client_id, user_id):
        return "public"
    return requested


def fetch_source_row_rest(source_id: int) -> dict[str, Any] | None:
    """Fila `sources` por id vía PostgREST (modo sin PG directo)."""
    return _fetch_source_by_id_rest(source_id)


def _fetch_source_by_id_rest(source_id: int) -> dict[str, Any] | None:
    q = urlencode([("select", "*"), ("id", f"eq.{source_id}")])
    rows = rest_json("GET", "sources", query=q)
    if not rows:
        return None
    if isinstance(rows, list):
        return dict(rows[0]) if rows else None
    return dict(rows)


def list_sources(
    *,
    viewer_client_id: int,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """Devuelve (items en forma API, total global o None en modo PostgREST sin conteo)."""
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    if is_database_mode():
        from nuwa_pg_dispatch import list_sources_pg

        total, rows = list_sources_pg(viewer_client_id, lim, off)
        return [source_row_to_api(r) for r in rows], total

    q = urlencode(
        [
            ("select", "*"),
            ("or", f"(visibility.eq.public,client_id.eq.{viewer_client_id})"),
            ("order", "id.desc"),
            ("limit", str(lim)),
            ("offset", str(off)),
        ]
    )
    log_phase("sources_list", "PostgREST")
    rows = rest_json("GET", "sources", query=q)
    if not rows:
        return [], None
    if not isinstance(rows, list):
        rows = [rows]
    return [source_row_to_api(r) for r in rows], None


def get_source(
    *,
    source_id: int,
    viewer_client_id: int,
    is_super_admin: bool,
) -> dict[str, Any] | None:
    if is_database_mode():
        from nuwa_pg_dispatch import get_source_visible_pg

        row = get_source_visible_pg(source_id, viewer_client_id, is_super_admin)
        return source_row_to_api(row) if row else None

    if is_super_admin:
        q = urlencode([("select", "*"), ("id", f"eq.{source_id}")])
    else:
        q = urlencode(
            [
                ("select", "*"),
                ("and", f"(id.eq.{source_id},or(visibility.eq.public,client_id.eq.{viewer_client_id}))"),
            ]
        )
    rows = rest_json("GET", "sources", query=q)
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    return source_row_to_api(row)


def create_source(
    *,
    name: str,
    risk_level: int,
    visibility: str,
    client_id: int,
    created_by_user_id: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "name": name,
        "risk_level": risk_level,
        "visibility": visibility,
        "client_id": client_id,
        "created_by_user_id": created_by_user_id,
        "metadata": metadata,
    }
    if is_database_mode():
        from nuwa_pg_dispatch import create_source_pg

        row = create_source_pg(
            name=name,
            risk_level=risk_level,
            visibility=visibility,
            client_id=client_id,
            created_by_user_id=created_by_user_id,
            metadata=metadata,
        )
        return source_row_to_api(row)

    rows = rest_json("POST", "sources", body=body)
    if isinstance(rows, list) and rows:
        row = rows[0]
    elif isinstance(rows, dict):
        row = rows
    else:
        raise SupabaseRestError(500, "PostgREST no devolvió la fila creada")
    return source_row_to_api(row)


def update_source(
    *,
    source_id: int,
    viewer_client_id: int,
    is_super_admin: bool,
    name: str | None,
    risk_level: int | None,
    visibility: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None | str:
    """dict ok, None not_found, 'forbidden'."""
    if is_database_mode():
        from nuwa_pg_dispatch import update_source_pg

        try:
            row = update_source_pg(
                source_id,
                name=name,
                risk_level=risk_level,
                visibility=visibility,
                metadata=metadata,
                viewer_client_id=viewer_client_id,
                is_super_admin=is_super_admin,
            )
        except SupabaseRestError as e:
            if e.status == 403:
                return "forbidden"
            raise
        return source_row_to_api(row) if row else None

    row0 = _fetch_source_by_id_rest(source_id)
    if not row0:
        return None
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        return "forbidden"
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if risk_level is not None:
        patch["risk_level"] = risk_level
    if visibility is not None:
        patch["visibility"] = visibility
    if metadata is not None:
        patch["metadata"] = metadata
    if not patch:
        return source_row_to_api(row0)
    rows = rest_json("PATCH", "sources", query=f"id=eq.{source_id}", body=patch)
    if isinstance(rows, list) and rows:
        return source_row_to_api(rows[0])
    if isinstance(rows, dict):
        return source_row_to_api(rows)
    return None


def delete_source(
    *,
    source_id: int,
    viewer_client_id: int,
    is_super_admin: bool,
) -> str:
    """'ok' | 'not_found' | 'forbidden'"""
    if is_database_mode():
        from nuwa_pg_dispatch import delete_source_pg

        return delete_source_pg(source_id, viewer_client_id, is_super_admin)

    row0 = _fetch_source_by_id_rest(source_id)
    if not row0:
        return "not_found"
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        return "forbidden"
    rest_json("DELETE", "sources", query=f"id=eq.{source_id}")
    return "ok"
