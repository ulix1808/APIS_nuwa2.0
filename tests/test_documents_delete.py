"""Eliminación en cascada: documento + JSON + chunks + fuente indexada."""

import sys
from unittest import mock

import pytest

sys.modules.setdefault("boto3", mock.MagicMock())
botocore = mock.MagicMock()
sys.modules.setdefault("botocore", botocore)
sys.modules.setdefault("botocore.exceptions", botocore.exceptions)

import nuwa_documents_pg as docs


def test_purge_document_search_index_deletes_source_and_chunks() -> None:
    mock_conn = mock.MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {
        "id": 42,
        "name": "doc:uuid-1:contrato.pdf",
        "metadata": {"documentId": "uuid-1", "sourceKind": "document"},
    }
    mock_conn.execute.return_value.rowcount = 3

    out = docs._purge_document_search_index(
        mock_conn,
        source_id=42,
        document_id="uuid-1",
        client_id=1,
    )

    assert out["removedSourceId"] == 42
    assert out["removedChunks"] == 3
    assert mock_conn.execute.call_count >= 3


def test_purge_skips_non_document_source() -> None:
    mock_conn = mock.MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {
        "id": 9,
        "name": "OFAC SDN",
        "metadata": {"list type": "sanctions"},
    }

    out = docs._purge_document_search_index(
        mock_conn,
        source_id=9,
        document_id="uuid-1",
        client_id=1,
    )

    assert out["removedSourceId"] is None
    assert out["removedChunks"] == 0
    assert mock_conn.execute.call_count == 1
