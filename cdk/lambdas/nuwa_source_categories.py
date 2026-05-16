"""Tabla source_categories: Postgres directo o PostgREST."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from nuwa_config import is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_obs_log import log_phase
from nuwa_pg_dispatch import category_row_to_api
from nuwa_supabase import rest_json

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class SourceCategoryError(Exception):
    """Error de negocio al validar categorías (HTTP 400 normalmente)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug.strip()):
        raise SourceCategoryError(
            "INVALID_CATEGORY_SLUG",
            "slug debe ser snake_case: letras minúsculas, números y _; empezar con letra.",
        )


def get_category_by_id(category_id: int) -> dict[str, Any] | None:
    if is_database_mode():
        from nuwa_pg_dispatch import get_source_category_by_id_pg

        row = get_source_category_by_id_pg(category_id)
        return category_row_to_api(row) if row else None

    q = urlencode([("select", "*"), ("id", f"eq.{category_id}")])
    rows = rest_json("GET", "source_categories", query=q)
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    return category_row_to_api(dict(row))


def validate_assignable_category(category_id: int) -> None:
    """Existe y está activa (altas / cambios de fuente)."""
    cat = get_category_by_id(category_id)
    if cat is None:
        raise SourceCategoryError(
            "INVALID_SOURCE_CATEGORY",
            "La categoría no existe.",
        )
    if not cat.get("isActive", True):
        raise SourceCategoryError(
            "INACTIVE_SOURCE_CATEGORY",
            "La categoría existe pero está inactiva.",
        )


def category_meta_for_embed(cat: dict[str, Any]) -> dict[str, Any]:
    """Shape para `meta.categories` en sources/list (sin timestamps)."""
    return {
        "id": int(cat["id"]),
        "slug": cat["slug"],
        "nameEs": cat["nameEs"],
        "nameEn": cat.get("nameEn"),
        "isActive": bool(cat.get("isActive", True)),
    }


def list_categories_catalog(*, active_only: bool) -> list[dict[str, Any]]:
    """
    Catálogo completo para dropdowns (una carga por request).
    active_only=True → solo is_active; False → todas las filas (super_admin / tooling).
    """
    filter_active: bool | None = True if active_only else None
    if is_database_mode():
        from nuwa_pg_dispatch import list_source_categories_catalog_pg

        rows = list_source_categories_catalog_pg(filter_active)
        return [category_meta_for_embed(category_row_to_api(r)) for r in rows]

    parts: list[tuple[str, str]] = [("select", "*"), ("order", "id.asc")]
    if filter_active is not None:
        parts.append(("is_active", f"eq.{str(filter_active).lower()}"))
    # Sin paginación explícita en PostgREST: límite alto para evitar N+1 en cliente.
    parts.append(("limit", "5000"))
    parts.append(("offset", "0"))
    q = urlencode(parts)
    log_phase("source_categories_catalog", "PostgREST")
    rows = rest_json("GET", "source_categories", query=q)
    if not rows:
        return []
    if not isinstance(rows, list):
        rows = [rows]
    return [
        category_meta_for_embed(category_row_to_api(dict(r))) for r in rows
    ]


def list_categories(
    *,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> tuple[int | None, list[dict[str, Any]]]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    if is_database_mode():
        from nuwa_pg_dispatch import list_source_categories_pg

        total, rows = list_source_categories_pg(is_active, lim, off)
        return total, [category_row_to_api(r) for r in rows]

    parts: list[tuple[str, str]] = [("select", "*"), ("order", "id.asc")]
    if is_active is not None:
        parts.append(("is_active", f"eq.{str(is_active).lower()}"))
    parts.append(("limit", str(lim)))
    parts.append(("offset", str(off)))
    q = urlencode(parts)
    log_phase("source_categories_list", "PostgREST")
    rows = rest_json("GET", "source_categories", query=q)
    if not rows:
        return None, []
    if not isinstance(rows, list):
        rows = [rows]
    # Sin cabecera Prefer: count (PostgREST), total se omite como en sources/list.
    return None, [category_row_to_api(dict(r)) for r in rows]


def create_category(
    *,
    slug: str,
    name_es: str,
    name_en: str | None,
    is_active: bool,
) -> dict[str, Any]:
    validate_slug(slug)
    slug = slug.strip()
    if not isinstance(name_es, str) or not name_es.strip():
        raise SourceCategoryError("BAD_REQUEST", "nameEs es requerido (string no vacío).")

    body = {
        "slug": slug,
        "name_es": name_es.strip(),
        "name_en": name_en.strip() if isinstance(name_en, str) and name_en.strip() else None,
        "is_active": bool(is_active),
    }
    if is_database_mode():
        from nuwa_pg_dispatch import create_source_category_pg

        try:
            row = create_source_category_pg(
                slug=body["slug"],
                name_es=body["name_es"],
                name_en=body["name_en"],
                is_active=body["is_active"],
            )
        except SupabaseRestError as e:
            if e.status == 409:
                raise SourceCategoryError(
                    "DUPLICATE_CATEGORY_SLUG",
                    "Ya existe una categoría con ese slug.",
                ) from e
            raise
        return category_row_to_api(row)

    try:
        rows = rest_json("POST", "source_categories", body=body)
    except SupabaseRestError as e:
        if e.status == 409:
            raise SourceCategoryError(
                "DUPLICATE_CATEGORY_SLUG",
                "Ya existe una categoría con ese slug.",
            ) from e
        raise
    if isinstance(rows, list) and rows:
        row = rows[0]
    elif isinstance(rows, dict):
        row = rows
    else:
        raise SupabaseRestError(500, "PostgREST no devolvió la fila creada")
    return category_row_to_api(dict(row))
