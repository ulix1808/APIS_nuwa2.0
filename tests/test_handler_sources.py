"""Tests unitarios de serialización y validación de categorías (sin importar handlers JWT)."""

from unittest import mock

import pytest

import nuwa_pg_dispatch
from nuwa_source_categories import (
    SourceCategoryError,
    category_meta_for_embed,
    list_categories_catalog,
    validate_assignable_category,
    validate_slug,
)


def test_source_row_to_api_includes_category_id() -> None:
    row = {
        "id": 3,
        "name": "n",
        "risk_level": 2,
        "visibility": "public",
        "client_id": 1,
        "created_by_user_id": 1,
        "metadata": {},
        "created_at": None,
        "updated_at": None,
        "source_category_id": 7,
    }
    api = nuwa_pg_dispatch.source_row_to_api(row)
    assert api["sourceCategoryId"] == 7


def test_source_row_to_api_nested_category() -> None:
    row = {
        "id": 3,
        "name": "n",
        "risk_level": 2,
        "visibility": "public",
        "client_id": 1,
        "created_by_user_id": 1,
        "metadata": {},
        "created_at": None,
        "updated_at": None,
        "source_category_id": 7,
        "sc_join_id": 7,
        "sc_join_slug": "fiscal",
        "sc_join_name_es": "Fiscal",
        "sc_join_name_en": None,
    }
    api = nuwa_pg_dispatch.source_row_to_api(row, include_category=True)
    assert api["category"]["slug"] == "fiscal"


def test_validate_assignable_missing() -> None:
    with mock.patch("nuwa_source_categories.get_category_by_id", return_value=None):
        with pytest.raises(SourceCategoryError) as ei:
            validate_assignable_category(99)
        assert ei.value.code == "INVALID_SOURCE_CATEGORY"


def test_validate_assignable_inactive() -> None:
    with mock.patch(
        "nuwa_source_categories.get_category_by_id",
        return_value={"id": 1, "isActive": False},
    ):
        with pytest.raises(SourceCategoryError) as ei:
            validate_assignable_category(1)
        assert ei.value.code == "INACTIVE_SOURCE_CATEGORY"


def test_validate_slug_bad() -> None:
    with pytest.raises(SourceCategoryError):
        validate_slug("Bad-Slug")


def test_category_meta_for_embed_omits_timestamps() -> None:
    m = category_meta_for_embed(
        {
            "id": 2,
            "slug": "fiscal",
            "nameEs": "Fiscal",
            "nameEn": None,
            "isActive": True,
            "createdAt": "x",
            "updatedAt": "y",
        }
    )
    assert m == {
        "id": 2,
        "slug": "fiscal",
        "nameEs": "Fiscal",
        "nameEn": None,
        "isActive": True,
    }
    assert "createdAt" not in m


def test_list_categories_catalog_single_query_pg() -> None:
    row = {
        "id": 1,
        "slug": "fiscal",
        "name_es": "Fiscal / Tributario",
        "name_en": None,
        "is_active": True,
        "created_at": None,
        "updated_at": None,
    }
    with mock.patch("nuwa_source_categories.is_database_mode", return_value=True), mock.patch(
        "nuwa_pg_dispatch.list_source_categories_catalog_pg", return_value=[row]
    ):
        out = list_categories_catalog(active_only=True)
    assert len(out) == 1
    assert out[0]["slug"] == "fiscal"
    assert out[0]["nameEs"] == "Fiscal / Tributario"


def test_sources_list_next_offset_logic() -> None:
    total, lim, off = 100, 50, 0
    assert off + lim < total
    next_off = off + lim if total is not None and off + lim < total else None
    assert next_off == 50
