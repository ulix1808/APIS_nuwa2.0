"""Token balance, ledger, and idempotent consume."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest import mock

import pytest

from nuwa_errors import SupabaseRestError
from nuwa_tokens_pg import resolve_token_client_id, tokens_balance, tokens_consume, tokens_ledger


ACTOR = {"id": 9, "client_id": 4, "role_slug": "admin"}
MASTER = {"id": 1, "client_id": 1, "role_slug": "super_admin"}


def test_resolve_token_client_uses_actor_company() -> None:
    assert resolve_token_client_id(ACTOR, {"clientId": 1}) == 4


def test_resolve_token_client_super_admin_target() -> None:
    assert resolve_token_client_id(MASTER, {"targetClientId": 8}) == 8


def test_resolve_token_client_denies_other_tenant() -> None:
    with pytest.raises(SupabaseRestError) as exc:
        resolve_token_client_id(ACTOR, {"targetClientId": 8})
    assert exc.value.status == 403


class _Conn:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.committed = False
        self.sql: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.sql.append(sql)
        return self.handler(sql, params)

    def commit(self) -> None:
        self.committed = True


def _cursor(row: Any) -> Any:
    class _Cur:
        def fetchone(self) -> Any:
            return row

        def fetchall(self) -> list[Any]:
            return row if isinstance(row, list) else []

    return _Cur()


@contextmanager
def _cm(conn: _Conn):
    yield conn


@mock.patch("nuwa_tokens_pg._conn")
def test_tokens_balance(mock_conn) -> None:
    conn = _Conn(lambda sql, params: _cursor({"token_limit": 100, "tokens_used": 40}))
    mock_conn.side_effect = lambda: _cm(conn)
    out = tokens_balance(ACTOR, {})
    assert out == {"success": True, "limit": 100, "used": 40, "remaining": 60}


@mock.patch("nuwa_tokens_pg._conn")
def test_tokens_ledger(mock_conn) -> None:
    created = datetime(2026, 9, 23, tzinfo=timezone.utc)
    conn = _Conn(
        lambda sql, params: _cursor(
            [
                {
                    "id": 3,
                    "cost": 1,
                    "action": "screening",
                    "label": "Screening: Ana",
                    "ref_folio": "SE-1",
                    "created_at": created,
                }
            ]
        )
    )
    mock_conn.side_effect = lambda: _cm(conn)
    out = tokens_ledger(ACTOR, {"limit": 10})
    assert out["items"][0]["refFolio"] == "SE-1"
    assert out["items"][0]["action"] == "screening"
    assert out["items"][0]["timestamp"] == int(created.timestamp() * 1000)


@mock.patch("nuwa_tokens_pg._conn")
def test_tokens_consume_debits_once(mock_conn) -> None:
    def handle(sql: str, params: Any) -> Any:
        if "SELECT id FROM token_ledger" in sql:
            return _cursor(None)
        if sql.strip().startswith("UPDATE clients"):
            return _cursor({"token_limit": 100, "tokens_used": 11})
        return _cursor(None)

    conn = _Conn(handle)
    mock_conn.side_effect = lambda: _cm(conn)
    status, body = tokens_consume(
        ACTOR,
        {"cost": 1, "action": "screening", "refFolio": "SE-1", "label": "Screening: Ana"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["alreadyCharged"] is False
    assert body["used"] == 11
    assert conn.committed is True
    assert any("INSERT INTO token_ledger" in sql for sql in conn.sql)
    assert any("client_token_usage" in sql for sql in conn.sql)


@mock.patch("nuwa_tokens_pg._conn")
def test_tokens_consume_is_idempotent(mock_conn) -> None:
    def handle(sql: str, params: Any) -> Any:
        if "SELECT id FROM token_ledger" in sql:
            return _cursor({"id": 3})
        if "FROM clients" in sql:
            return _cursor({"token_limit": 100, "tokens_used": 10})
        return _cursor(None)

    conn = _Conn(handle)
    mock_conn.side_effect = lambda: _cm(conn)
    status, body = tokens_consume(ACTOR, {"cost": 1, "action": "screening", "refFolio": "SE-1"})
    assert status == 200
    assert body["alreadyCharged"] is True
    assert body["used"] == 10
    assert conn.committed is False
    assert not any(sql.strip().startswith("UPDATE clients") for sql in conn.sql)


@mock.patch("nuwa_tokens_pg._conn")
def test_tokens_consume_insufficient(mock_conn) -> None:
    def handle(sql: str, params: Any) -> Any:
        if sql.strip().startswith("UPDATE clients"):
            return _cursor(None)
        if "FROM clients" in sql:
            return _cursor({"token_limit": 10, "tokens_used": 10})
        return _cursor(None)

    conn = _Conn(handle)
    mock_conn.side_effect = lambda: _cm(conn)
    status, body = tokens_consume(ACTOR, {"cost": 1, "action": "monitoring"})
    assert status == 409
    assert body["code"] == "insufficient_tokens"
    assert body["remaining"] == 0
    assert conn.committed is False


def test_tokens_consume_rejects_bad_cost() -> None:
    status, body = tokens_consume(ACTOR, {"cost": 0, "action": "screening"})
    assert status == 400
    assert body["code"] == "invalid_cost"
