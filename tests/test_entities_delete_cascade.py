"""Soft delete entidad: reportes, monitoreo y documents.primary_entity_id."""

import sys
from unittest import mock

import pytest

sys.modules.setdefault("boto3", mock.MagicMock())
botocore = mock.MagicMock()
sys.modules.setdefault("botocore", botocore)
sys.modules.setdefault("botocore.exceptions", botocore.exceptions)

from nuwa_errors import SupabaseRestError


@pytest.fixture
def mock_conn():
    conn = mock.MagicMock()
    entity_row = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "name": "Ingeniería de Bombas y Controles S.A. de C.V. (IDBC)",
        "legal_name": "IDBC",
        "full_name": "IDBC",
    }
    fetchone = mock.MagicMock(side_effect=[entity_row, None])
    conn.execute.return_value.fetchone = fetchone
    conn.execute.return_value.fetchall.return_value = [{"id": 1}, {"id": 2}]
    return conn


def test_entities_delete_marks_reports_and_returns_count(mock_conn) -> None:
    with mock.patch("nuwa_entities_pg._conn") as mconn:
        mconn.return_value.__enter__.return_value = mock_conn
        from nuwa_entities_pg import entities_delete_pg

        out = entities_delete_pg(
            {
                "clientId": 1,
                "userId": 10,
                "entityId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            }
        )

    assert out["status"] == "deleted"
    assert out["reportsDeleted"] == 2
    sql_calls = " ".join(str(c.args[0]) for c in mock_conn.execute.call_args_list)
    assert "UPDATE public.reports" in sql_calls
    assert "entity_monitoring" in sql_calls
    assert "primary_entity_id = NULL" in sql_calls


def test_entities_delete_not_found() -> None:
    conn = mock.MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    with mock.patch("nuwa_entities_pg._conn") as mconn:
        mconn.return_value.__enter__.return_value = conn
        from nuwa_entities_pg import entities_delete_pg

        with pytest.raises(SupabaseRestError) as exc:
            entities_delete_pg(
                {"clientId": 1, "userId": 10, "entityId": "00000000-0000-0000-0000-000000000001"}
            )
        assert exc.value.status == 404


def test_reports_get_active_excludes_deleted_entity_names() -> None:
    from nuwa_pg_dispatch import _reports_get

    with mock.patch("nuwa_pg_dispatch._conn") as mconn:
        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mconn.return_value.__enter__.return_value = conn
        _reports_get({"status": "eq.active", "client_id": "eq.1", "limit": "10", "offset": "0"})

    sql = conn.execute.call_args.args[0]
    assert "e.status = 'deleted'" in sql
    assert "legal_name" in sql
    assert "full_name" in sql
