"""Unit tests — entity touch after monitoring run-finish and monitoring list fields."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest import mock

from datetime import datetime, timezone

from nuwa_entities_pg import (
    _entity_api,
    entities_alerts_create_pg,
    entities_monitoring_due_pg,
    entities_monitoring_list_pg,
    entities_monitoring_run_finish_pg,
)
from nuwa_errors import SupabaseRestError


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)
        self._idx = 0

    def fetchone(self) -> dict[str, Any] | None:
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self) -> list[dict[str, Any]]:
        if self._idx >= len(self._rows):
            return []
        row = self._rows[self._idx]
        self._idx += 1
        if isinstance(row, list):
            return row
        return [row]


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.committed = False

    def execute(self, _sql: str, _params: Any = None) -> _FakeCursor:
        item = self._script.pop(0)
        if isinstance(item, list):
            return _FakeCursor(item)
        return _FakeCursor([item])

    def commit(self) -> None:
        self.committed = True


@contextmanager
def _fake_conn(script: list[Any]):
    yield _FakeConn(script)


def test_entity_api_includes_last_report_folio() -> None:
    api = _entity_api(
        {
            "id": "e1",
            "name": "Ana López",
            "party_type": "individual",
            "party_type_label": "Persona Física",
            "legal_name": None,
            "first_name": "Ana",
            "last_name": "López",
            "full_name": "Ana López",
            "category": "screening",
            "rfc": None,
            "curp": None,
            "country": "MX",
            "risk_level": "medium",
            "status": "active",
            "relationship_role": None,
            "parent_entity_id": None,
            "last_screening_at": None,
            "last_report_folio": "RPT-2026-001",
            "created_at": None,
            "updated_at": None,
            "metadata": {},
        },
        report_count=2,
    )
    assert api["lastReportFolio"] == "RPT-2026-001"
    assert api["reportCount"] == 2


@mock.patch("nuwa_entities_pg._conn")
def test_monitoring_list_includes_last_error(mock_conn) -> None:
    row = {
        "id": "e1",
        "party_type": "individual",
        "party_type_label": "Persona Física",
        "legal_name": None,
        "first_name": "Ana",
        "last_name": "López",
        "full_name": "Ana López",
        "category": "screening",
        "rfc": None,
        "curp": None,
        "country": "MX",
        "risk_level": "low",
        "status": "active",
        "relationship_role": None,
        "parent_entity_id": None,
        "last_screening_at": None,
        "last_report_folio": None,
        "created_at": None,
        "updated_at": None,
        "metadata": {},
        "monitoring_id": "m1",
        "frequency": "weekly",
        "sources": ["sanctions"],
        "next_run_at": None,
        "last_run_at": None,
        "last_run_status": "error",
        "last_error": "timeout en búsqueda",
        "is_enabled": True,
        "report_count": 0,
        "name": "Ana López",
    }

    @contextmanager
    def _cm():
        yield _FakeConn([{"c": 1}, [row]])

    mock_conn.side_effect = _cm
    out = entities_monitoring_list_pg({"clientId": 1, "userId": 1})
    assert out["total"] == 1
    assert out["items"][0]["lastError"] == "timeout en búsqueda"
    assert out["items"][0]["lastRunStatus"] == "error"


@mock.patch("nuwa_entities_pg.touch_entity_after_report_pg")
@mock.patch("nuwa_entities_pg._conn")
def test_run_finish_ok_touches_entity(mock_conn, mock_touch) -> None:
    run = {
        "entity_id": "e1",
        "monitoring_id": "m1",
        "frequency": "weekly",
        "is_enabled": True,
    }

    @contextmanager
    def _cm():
        conn = _FakeConn([run, None, None])
        yield conn

    mock_conn.side_effect = _cm
    out = entities_monitoring_run_finish_pg(
        {
            "clientId": 1,
            "runId": "r1",
            "status": "ok",
            "reportFolio": "RPT-AUTO-99",
            "riskAfter": "2",
        }
    )
    assert out["lastRunStatus"] == "ok"
    mock_touch.assert_called_once_with(
        entity_id="e1",
        client_id=1,
        folio="RPT-AUTO-99",
        nivel_riesgo=None,
        nivel_numerico=2,
    )


@mock.patch("nuwa_entities_pg.touch_entity_after_report_pg")
@mock.patch("nuwa_entities_pg._conn")
def test_run_finish_error_due_immediately(mock_conn, mock_touch) -> None:
    run = {
        "entity_id": "e1",
        "monitoring_id": "m1",
        "frequency": "weekly",
        "is_enabled": True,
    }

    @contextmanager
    def _cm():
        yield _FakeConn([run, None, None])

    mock_conn.side_effect = _cm
    before = datetime.now(timezone.utc)
    out = entities_monitoring_run_finish_pg(
        {
            "clientId": 1,
            "runId": "r1",
            "status": "error",
            "error": "timeout",
        }
    )
    after = datetime.now(timezone.utc)
    assert out["lastRunStatus"] == "error"
    mock_touch.assert_not_called()
    nxt = datetime.fromisoformat(out["nextRunAt"].replace("Z", "+00:00"))
    assert abs((nxt - before.replace(microsecond=0)).total_seconds()) < 2
    assert nxt <= after


@mock.patch("nuwa_entities_pg._conn")
def test_due_allows_null_created_by_user(mock_conn) -> None:
    row = {
        "monitoring_id": "m1",
        "entity_id": "e1",
        "client_id": 1,
        "frequency": "weekly",
        "sources": ["sanctions"],
        "next_run_at": None,
        "created_by_user_id": None,
        "entity_name": "Ana",
        "party_type": "individual",
        "last_report_folio": None,
        "rfc": None,
        "curp": None,
    }

    @contextmanager
    def _cm():
        yield _FakeConn([[row]])

    mock_conn.side_effect = _cm
    out = entities_monitoring_due_pg({"limit": 10})
    assert out["total"] == 1
    assert out["items"][0]["createdByUserId"] is None


@mock.patch("nuwa_entities_pg._get_entity_row", return_value={"id": "e1"})
@mock.patch("nuwa_entities_pg._conn")
def test_alert_run_failed_allowed(mock_conn, _ent) -> None:
    @contextmanager
    def _cm():
        yield _FakeConn([{"id": "a1", "created_at": None, "status": "new"}])

    mock_conn.side_effect = _cm
    out = entities_alerts_create_pg(
        {
            "clientId": 1,
            "entityId": "e1",
            "alertType": "run_failed",
            "severity": "medium",
            "title": "Run falló",
        }
    )
    assert out["alertType"] == "run_failed"


def test_alert_unknown_type_rejected() -> None:
    try:
        entities_alerts_create_pg(
            {
                "clientId": 1,
                "entityId": "e1",
                "alertType": "unknown",
                "title": "x",
            }
        )
    except SupabaseRestError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("expected 400")
