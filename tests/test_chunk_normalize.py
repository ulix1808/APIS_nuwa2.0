"""Tests for chunk_normalize."""

from chunk_normalize import (
    CHUNK_TEXT_NORMALIZED_KEY,
    normalize_chunk_search_text,
    prepare_chunk_text_for_storage,
)


def test_normalize_chunk_search_text_folds_enye_and_accents() -> None:
    assert normalize_chunk_search_text("José Muñoz") == "jose munoz"
    assert normalize_chunk_search_text("María Peña") == "maria pena"


def test_prepare_chunk_text_for_storage_json() -> None:
    raw = '{"nombre":"José Peña","estado":"Jalisco"}'
    stored, norm = prepare_chunk_text_for_storage(raw)
    assert "José Peña" in stored
    assert CHUNK_TEXT_NORMALIZED_KEY in stored
    assert norm == "jose pena jalisco"
    import json

    obj = json.loads(stored)
    assert obj[CHUNK_TEXT_NORMALIZED_KEY] == norm


def test_prepare_chunk_text_replaces_existing_normalized_key() -> None:
    raw = '{"nombre":"Ana","chunk_text_normalized":"stale"}'
    stored, norm = prepare_chunk_text_for_storage(raw)
    import json

    obj = json.loads(stored)
    assert obj[CHUNK_TEXT_NORMALIZED_KEY] == "ana"
    assert norm == "ana"
