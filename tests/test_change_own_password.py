"""Password change clears must_change_password for the token user only."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest

from nuwa_errors import SupabaseRestError
from nuwa_password import hash_password
from nuwa_pg_dispatch import change_own_password


def test_change_own_password_clears_flag() -> None:
    stored = hash_password("old-password")
    seen: list[tuple[str, Any]] = []

    class _Conn:
        def execute(self, sql: str, params: Any = None) -> Any:
            seen.append((sql, params))

            class _Res:
                def fetchone(_self) -> dict[str, Any] | None:
                    if "SELECT" in sql:
                        return {"id": 8, "password_hash": stored, "is_active": True}
                    return None

            return _Res()

        def commit(self) -> None:
            seen.append(("COMMIT", None))

    @contextmanager
    def _cm():
        yield _Conn()

    with mock.patch("nuwa_pg_dispatch._conn", lambda: _cm()):
        out = change_own_password(user_id=8, current_password="old-password", new_password="new-password")

    assert out == {"success": True, "mustChangePassword": False}
    update = next(item for item in seen if "UPDATE" in item[0])
    assert "must_change_password = false" in update[0]
    assert update[1][1] == 8


def test_change_own_password_rejects_wrong_current() -> None:
    stored = hash_password("old-password")
    seen: list[str] = []

    class _Conn:
        def execute(self, sql: str, params: Any = None) -> Any:
            seen.append(sql)

            class _Res:
                def fetchone(_self) -> dict[str, Any]:
                    return {"id": 8, "password_hash": stored, "is_active": True}

            return _Res()

        def commit(self) -> None:
            raise AssertionError("no commit")

    @contextmanager
    def _cm():
        yield _Conn()

    with mock.patch("nuwa_pg_dispatch._conn", lambda: _cm()):
        with pytest.raises(SupabaseRestError) as exc:
            change_own_password(user_id=8, current_password="nope", new_password="new-password")
    assert exc.value.status == 401
    assert not any("UPDATE" in sql for sql in seen)
