"""Unit tests — nuwa_admin_platform_pg (panel admin plataforma)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest

from nuwa_admin_platform_pg import (
    _countries_from_row,
    _map_app_role_to_id,
    _map_role_slug,
    _user_api,
    admin_users_delete_platform,
    admin_users_invite,
    admin_users_list_platform,
    admin_users_update_platform,
    clients_create,
    clients_list,
    parse_operating_countries,
    require_super_admin,
)
from nuwa_errors import SupabaseRestError


def test_require_super_admin_allows() -> None:
    require_super_admin({"role_slug": "super_admin"})


def test_require_super_admin_denies() -> None:
    with pytest.raises(SupabaseRestError) as exc:
        require_super_admin({"role_slug": "admin"})
    assert exc.value.status == 403


def test_role_mapping() -> None:
    assert _map_role_slug("super_admin") == "master"
    assert _map_role_slug("user") == "analyst"
    assert _map_role_slug("compliance_officer") == "compliance_officer"

    class _Conn:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def execute(self, sql: str, params: Any = None) -> Any:
            self.last_sql = sql
            self.last_params = params
            parent = self

            class _Res:
                def fetchall(self_inner) -> list[dict[str, Any]]:
                    return list(parent._rows)

                def fetchone(self_inner) -> dict[str, Any] | None:
                    return parent._rows[0] if parent._rows else None

            return _Res()

    assert _map_app_role_to_id(_Conn([{"id": 1, "slug": "super_admin"}]), "master") == 1
    assert _map_app_role_to_id(_Conn([{"id": 2, "slug": "admin"}]), "admin") == 2
    assert _map_app_role_to_id(_Conn([{"id": 3, "slug": "user"}]), "analyst") == 3
    assert (
        _map_app_role_to_id(
            _Conn([{"id": 9, "slug": "compliance_officer"}]),
            "compliance_officer",
        )
        == 9
    )


def test_user_api_shape() -> None:
    u = _user_api(
        {
            "id": 5,
            "email": "a@b.com",
            "full_name": "Ana",
            "role_slug": "super_admin",
            "is_active": True,
            "client_id": 1,
        },
        company_name="Nuwa",
    )
    assert u["role"] == "master"
    assert u["status"] == "active"
    assert u["companyName"] == "Nuwa"


class _FakeCursor:
    def __init__(self, script: list[tuple[Any, ...]]) -> None:
        self._script = list(script)
        self._idx = 0

    def fetchone(self) -> dict[str, Any] | None:
        if self._idx >= len(self._script):
            return None
        row = self._script[self._idx]
        self._idx += 1
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        raise TypeError(row)

    def fetchall(self) -> list[dict[str, Any]]:
        if self._idx >= len(self._script):
            return []
        row = self._script[self._idx]
        self._idx += 1
        if isinstance(row, list):
            return row
        if isinstance(row, dict):
            return [row]
        raise TypeError(row)


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.committed = False

    def execute(self, _sql: str, _params: Any = None) -> _FakeCursor:
        item = self._script.pop(0)
        if callable(item):
            return item(_sql, _params)
        return _FakeCursor([item] if not isinstance(item, list) else item)

    def commit(self) -> None:
        self.committed = True


@contextmanager
def _fake_conn(script: list[Any]):
    conn = _FakeConn(script)
    yield conn


@mock.patch("nuwa_admin_platform_pg._conn")
def test_clients_list_returns_stats(mock_conn) -> None:
    rows = [
        {
            "id": 1,
            "name": "Nuwa",
            "rfc": "RFC1",
            "legal_rep": None,
            "address": None,
            "sector": None,
            "plan": "professional",
            "status": "active",
            "token_limit": 2000,
            "tokens_used": 100,
            "billing_contact": None,
            "billing_email": None,
            "payment_method": None,
            "next_invoice": None,
            "compliance_officer_user_id": None,
            "operating_countries": ["colombia", "costarica"],
            "created_at": "2026-01-01",
            "company_name": "Nuwa",
        }
    ]
    user_row = {
        "id": 1,
        "email": "nuwa@nuwa.space",
        "full_name": "Admin",
        "role_slug": "super_admin",
        "is_active": True,
        "client_id": 1,
        "company_name": "Nuwa",
    }
    usage_row = {
        "screenings": 10,
        "background_checks": 0,
        "monitoring": 0,
        "targeted": 0,
    }

    mock_conn.side_effect = lambda: _fake_conn(
        [
            rows,
            [user_row],
            usage_row,
        ]
    )

    out = clients_list({"limit": 10})
    assert out["success"] is True
    assert len(out["clients"]) == 1
    assert out["clients"][0]["name"] == "Nuwa"
    assert out["clients"][0]["operatingCountries"] == ["colombia", "costarica"]
    assert out["clients"][0]["usage"]["screenings"] == 10
    assert out["stats"]["totalClients"] == 1
    assert out["stats"]["totalTokensConsumed"] == 100


def test_parse_operating_countries() -> None:
    assert parse_operating_countries(["México", "Costa Rica", "mexico"]) == ["mexico", "costarica"]
    assert parse_operating_countries("guatemala,colombia") == ["guatemala", "colombia"]
    assert parse_operating_countries(["peru"]) is None
    assert parse_operating_countries([]) is None
    assert _countries_from_row({}) == ["mexico"]
    assert _countries_from_row({"operating_countries": "{mexico,guatemala}"}) == ["mexico", "guatemala"]


def test_clients_create_rejects_unknown_country() -> None:
    with pytest.raises(SupabaseRestError) as exc:
        clients_create({"name": "Sadah", "rfc": "SAD", "operatingCountries": ["peru"]})
    assert exc.value.status == 400


@mock.patch("nuwa_admin_platform_pg._conn")
def test_clients_create_stores_operating_countries(mock_conn) -> None:
    seen: list[tuple[str, Any]] = []

    class _Rec(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _FakeCursor:
            seen.append((sql, params))
            return super().execute(sql, params)

    script: list[Any] = [
        {"id": 8},
        None,
        {
            "id": 8,
            "name": "Sadah",
            "rfc": "SAD",
            "plan": "professional",
            "status": "active",
            "token_limit": 2000,
            "tokens_used": 0,
            "operating_countries": ["mexico", "guatemala"],
        },
        None,
    ]

    @contextmanager
    def _cm():
        yield _Rec(script)

    mock_conn.side_effect = lambda: _cm()
    out = clients_create(
        {"name": "Sadah", "rfc": "sad", "operatingCountries": ["México", "Guatemala"]},
    )
    assert out["client"]["operatingCountries"] == ["mexico", "guatemala"]
    insert_params = next(params for sql, params in seen if "operating_countries" in sql)
    assert insert_params[-1] == ["mexico", "guatemala"]


@mock.patch("nuwa_admin_platform_pg.hash_password", return_value="pbkdf2_sha256$s$hash")
@mock.patch("nuwa_admin_platform_pg._generate_temp_password", return_value="TempPass123!")
@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_invite_returns_temp_password(mock_conn, _gen, _hash) -> None:
    mock_conn.side_effect = lambda: _fake_conn(
        [
            [{"id": 3, "slug": "user"}],  # role lookup
            {"name": "Nuwa"},
            None,
            {
                "id": 99,
                "email": "new@nuwa.space",
                "full_name": "New",
                "client_id": 1,
                "is_active": True,
            },
            {"slug": "user"},
        ]
    )

    out = admin_users_invite(
        {
            "email": "new@nuwa.space",
            "name": "New User",
            "role": "analyst",
            "clientId": 1,
        }
    )
    assert out["success"] is True
    assert out["tempPassword"] == "TempPass123!"
    assert out["user"]["email"] == "new@nuwa.space"
    assert out["user"]["status"] == "invited"
    _hash.assert_called_once_with("TempPass123!")


@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_invite_email_exists(mock_conn) -> None:
    mock_conn.side_effect = lambda: _fake_conn(
        [
            [{"id": 3, "slug": "user"}],  # role lookup
            {"name": "Nuwa"},
            {"id": 1},
        ]
    )
    with pytest.raises(SupabaseRestError) as exc:
        admin_users_invite(
            {
                "email": "exists@nuwa.space",
                "name": "X",
                "role": "analyst",
                "clientId": 1,
            }
        )
    assert exc.value.status == 409


@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_list_filters_by_target_not_actor(mock_conn) -> None:
    seen: list[tuple[str, Any]] = []

    class _Rec(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _FakeCursor:
            seen.append((sql, params))
            return super().execute(sql, params)

    @contextmanager
    def _cm():
        yield _Rec([[]])

    mock_conn.side_effect = lambda: _cm()
    admin_users_list_platform({"clientId": 1, "userId": 9, "targetClientId": 4})
    sql, params = seen[0]
    assert "u.client_id = %s" in sql
    assert params == [4]

    seen.clear()
    mock_conn.side_effect = lambda: _cm()
    admin_users_list_platform({"clientId": 1, "userId": 9})
    sql, _params = seen[0]
    assert "u.client_id = %s" not in sql


@mock.patch("nuwa_admin_platform_pg.hash_password", return_value="pbkdf2_sha256$s$hash")
@mock.patch("nuwa_admin_platform_pg._generate_temp_password", return_value="TempPass123!")
@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_invite_uses_target_client(mock_conn, _gen, _hash) -> None:
    seen: list[tuple[str, Any]] = []

    class _Rec(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _FakeCursor:
            seen.append((sql, params))
            return super().execute(sql, params)

    script: list[Any] = [
        [{"id": 3, "slug": "user"}],
        {"name": "Sadah"},
        None,
        {
            "id": 99,
            "email": "new@nuwa.space",
            "full_name": "New",
            "client_id": 4,
            "is_active": True,
        },
        {"slug": "user"},
    ]

    @contextmanager
    def _cm():
        yield _Rec(script)

    mock_conn.side_effect = lambda: _cm()
    out = admin_users_invite(
        {
            "email": "new@nuwa.space",
            "name": "New User",
            "role": "analyst",
            "clientId": 1,
            "userId": 9,
            "targetClientId": 4,
        }
    )
    assert out["user"]["clientId"] == 4
    company_params = next(params for sql, params in seen if "FROM companies" in sql)
    assert company_params == (4,)
    insert_params = next(params for sql, params in seen if "INSERT INTO nuwa_users" in sql)
    assert insert_params[0] == 4


@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_update_reassigns_client(mock_conn) -> None:
    seen: list[tuple[str, Any]] = []

    class _Rec(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _FakeCursor:
            seen.append((sql, params))
            return super().execute(sql, params)

    script: list[Any] = [
        {
            "id": 5,
            "email": "a@b.com",
            "full_name": "Ana",
            "client_id": 4,
            "is_active": True,
            "role_id": 3,
        },
        {"slug": "user"},
        {"name": "Sadah"},
    ]

    @contextmanager
    def _cm():
        yield _Rec(script)

    mock_conn.side_effect = lambda: _cm()
    out = admin_users_update_platform(
        {"targetUserId": 5, "targetClientId": 4, "clientId": 1, "userId": 9}
    )
    assert out["user"]["clientId"] == 4
    update_params = next(params for sql, params in seen if sql.startswith("UPDATE nuwa_users"))
    assert update_params[0] == 4
    assert "client_id = %s" in seen[0][0]


@mock.patch("nuwa_admin_platform_pg._conn")
def test_admin_users_delete_removes_row(mock_conn) -> None:
    seen: list[str] = []

    class _Rec(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _FakeCursor:
            seen.append(sql)
            return super().execute(sql, params)

    @contextmanager
    def _cm():
        yield _Rec([[], {"id": 5}])

    mock_conn.side_effect = lambda: _cm()
    out = admin_users_delete_platform({"targetUserId": 5}, fallback_user_id=9)
    assert out["deleted"] is True
    assert any(sql.startswith("DELETE FROM nuwa_users") for sql in seen)
    assert not any("is_active" in sql for sql in seen)
