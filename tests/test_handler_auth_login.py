"""Login picks the product tenant and returns mustChangePassword."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest import mock


def _install_stub(name: str) -> ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = ModuleType(name)
        sys.modules[name] = mod
    return mod


_install_stub("boto3")
_botocore = _install_stub("botocore")
_botocore_exc = _install_stub("botocore.exceptions")
_botocore_exc.ClientError = Exception
_botocore.exceptions = _botocore_exc
_install_stub("jwt")
_crypto = _install_stub("cryptography")
_fernet = _install_stub("cryptography.fernet")
_fernet.Fernet = object
_fernet.InvalidToken = Exception
_crypto.fernet = _fernet

import handler_auth as ha


def _row(**kwargs):
    base = {
        "id": 1,
        "client_id": 1,
        "email": "a@b.com",
        "password_hash": "hash",
        "full_name": "User",
        "role_id": 1,
        "is_active": True,
        "must_change_password": False,
        "role_slug": "admin",
        "role_name": "Admin",
    }
    base.update(kwargs)
    return base


def test_pick_login_account_prefers_single_product_tenant() -> None:
    picked = ha.pick_login_account(
        [
            _row(id=1, client_id=1, role_slug="super_admin"),
            _row(id=8, client_id=4, role_slug="admin"),
        ]
    )
    assert picked is not None
    assert picked["id"] == 8


def test_resolve_ignores_platform_client_hint() -> None:
    user, err = ha.resolve_login_account(
        [
            _row(id=1, client_id=1, role_slug="super_admin"),
            _row(id=8, client_id=4, role_slug="admin", must_change_password=True),
        ],
        1,
    )
    assert err is None
    assert user is not None
    assert user["client_id"] == 4


def test_resolve_two_product_tenants_requires_client_id() -> None:
    rows = [
        _row(id=8, client_id=4, role_slug="admin"),
        _row(id=9, client_id=7, role_slug="analyst"),
    ]
    user, err = ha.resolve_login_account(rows, None)
    assert user is None
    assert err == "client_id_required"
    chosen, err2 = ha.resolve_login_account(rows, 7)
    assert err2 is None
    assert chosen is not None
    assert chosen["id"] == 9


@mock.patch.object(ha, "mint_access_token", return_value=("tok", 3600))
@mock.patch.object(ha, "verify_password", return_value=True)
@mock.patch.object(ha, "rest_json", return_value=[{"name": "Sadah"}])
@mock.patch.object(ha, "_load_login_rows")
def test_login_returns_product_account_and_password_flag(mock_rows, mock_rest, _verify, mock_mint) -> None:
    mock_rows.return_value = [
        _row(id=1, client_id=1, role_slug="super_admin", role_name="Super", must_change_password=False),
        _row(id=8, client_id=4, role_slug="admin", role_name="Admin", must_change_password=True, full_name="Ana"),
    ]
    out = ha._login({"email": "A@b.com", "password": "secret", "clientId": 1})
    assert out["statusCode"] == 200
    body = json.loads(out["body"])
    assert body["user"]["id"] == 8
    assert body["user"]["clientId"] == 4
    assert body["user"]["roleSlug"] == "admin"
    assert body["user"]["mustChangePassword"] is True
    assert mock_mint.call_args.kwargs["client_id"] == 4
    assert mock_mint.call_args.kwargs["user_id"] == 8
    assert mock_mint.call_args.kwargs["role_slug"] == "admin"
    company_query = mock_rest.call_args.kwargs["query"]
    assert "client_id=eq.4" in company_query
