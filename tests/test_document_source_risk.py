"""Riesgo 0 en fuentes/chunks del módulo de documentos internos."""

from unittest import mock

import pytest

from document_helpers import DOCUMENT_SOURCE_RISK_LEVEL, document_source_metadata


def test_document_source_category_slug() -> None:
    from document_helpers import DOCUMENT_SOURCE_CATEGORY_SLUG

    assert DOCUMENT_SOURCE_CATEGORY_SLUG == "documento_interno"


def test_document_source_risk_level_is_zero() -> None:
    assert DOCUMENT_SOURCE_RISK_LEVEL == 0


def test_document_source_metadata_source_kind() -> None:
    meta = document_source_metadata(
        document_id="uuid-1",
        client_id=1,
        primary_entity_id=None,
        document_type="Acta",
    )
    assert meta["sourceKind"] == "document"
    assert meta["sourceOrigin"] == "document"


def test_finalize_ingest_call_contract() -> None:
    """Contrato esperado en documents_finalize_pg → ingest_chunks (sin importar el módulo completo)."""
    kwargs = dict(
        source_id=99,
        viewer_client_id=1,
        is_super_admin=False,
        chunk_texts=['{"summary":"x"}'],
        replace_strategy="all",
        risk_level=DOCUMENT_SOURCE_RISK_LEVEL,
        visibility="private",
        entity_type="document",
    )
    assert kwargs["risk_level"] == 0
    assert kwargs["entity_type"] == "document"
    assert kwargs["replace_strategy"] == "all"


def test_ingest_chunks_pg_explicit_risk_zero() -> None:
    import nuwa_pg_dispatch as pg

    mock_conn = mock.MagicMock()
    mock_cursor = mock.MagicMock()
    mock_conn.__enter__ = mock.Mock(return_value=mock_conn)
    mock_conn.__exit__ = mock.Mock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = mock.Mock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = mock.Mock(return_value=False)
    mock_conn.execute.return_value.rowcount = 0

    source_row = {
        "id": 5,
        "client_id": 1,
        "risk_level": 1,
        "visibility": "private",
        "name": "doc:uuid:file.pdf",
        "metadata": {"documentId": "uuid"},
    }

    with mock.patch.object(pg, "_conn", return_value=mock_conn), mock.patch.object(
        pg, "fetch_source_by_id_pg", return_value=source_row
    ), mock.patch.object(pg, "can_mutate_source_row", return_value=True):
        out = pg.ingest_chunks_pg(
            5,
            viewer_client_id=1,
            is_super_admin=False,
            replace_strategy="all",
            chunk_texts=["chunk-a"],
            risk_level=0,
            visibility="private",
            entity_type="document",
        )

    assert out["insertedChunks"] == 1
    batch = mock_cursor.executemany.call_args[0][1]
    assert batch[0][1] == 0  # risk_level column
