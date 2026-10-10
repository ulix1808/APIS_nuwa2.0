"""Report list reads only the group fields of report_json unless the payload is requested."""

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cdk" / "lambdas"))

import nuwa_pg_dispatch  # noqa: E402


def _capture_sql(select: str) -> str:
    seen: dict[str, str] = {}

    class _Conn:
        def execute(self, sql, params):
            seen["sql"] = sql
            return mock.Mock(fetchall=lambda: [])

    @contextmanager
    def fake_conn():
        yield _Conn()

    with mock.patch.object(nuwa_pg_dispatch, "_conn", fake_conn):
        nuwa_pg_dispatch._reports_get({"select": select, "status": "active", "client_id": "1", "limit": "500"})
    return seen["sql"]


def test_group_only_select_never_reads_the_whole_report_json() -> None:
    sql = _capture_sql("id,folio,group_id,report_json_group")
    assert "jsonb_build_object" in sql
    assert "report_json->'metadatos'->'groupName'" in sql
    assert ") AS report_json" in sql
    select_part = sql.split("FROM public.reports")[0]
    plain_columns = select_part.split("jsonb_build_object")[0]
    assert "report_json" not in plain_columns


def test_full_select_keeps_report_json() -> None:
    sql = _capture_sql("id,folio,report_json")
    assert "SELECT id, folio, report_json FROM" in sql
    assert "jsonb_build_object" not in sql
