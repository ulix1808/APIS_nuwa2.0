"""Handler routing — panel admin plataforma (mock auth + PG layer)."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest import mock


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_handler_admin():
    _install_stub("boto3", client=lambda *a, **k: mock.MagicMock())
    botocore_exc = ModuleType("botocore.exceptions")
    botocore_exc.ClientError = Exception
    _install_stub("botocore", exceptions=botocore_exc)
    sys.modules["botocore.exceptions"] = botocore_exc

    _install_stub(
        "nuwa_api_auth",
        jwt_matches_actor_body=lambda claims, body: True,
        require_jwt=lambda event: {"sub": "1", "cid": "1", "role": "super_admin"},
    )
    _install_stub(
        "nuwa_config",
        DatabaseConfigError=Exception,
        SupabaseConfigError=Exception,
        ensure_data_backend=lambda: None,
        is_database_mode=lambda: True,
    )
    _install_stub(
        "nuwa_errors",
        SupabaseRestError=type(
            "SupabaseRestError",
            (Exception,),
            {
                "__init__": lambda self, status, body: (
                    setattr(self, "status", status),
                    setattr(self, "body", body),
                    None,
                )[-1]
            },
        ),
    )
    _install_stub("nuwa_http", json_response=lambda status, body: {"statusCode": status, "body": json.dumps(body)})
    _install_stub(
        "nuwa_obs_log",
        log_handler_enter=lambda *a, **k: None,
        log_phase=lambda *a, **k: None,
        log_await=lambda *a, **k: None,
        log_done=lambda *a, **k: None,
    )
    _install_stub("nuwa_password", hash_password=lambda p: f"hash:{p}")
    _install_stub("nuwa_rbac", can_manage_company=lambda *a: True, can_manage_users=lambda *a: True)
    _install_stub("nuwa_supabase", rest_json=lambda *a, **k: [], fetch_user_with_role=lambda **k: None)
    _install_stub(
        "nuwa_app_crypto",
        AppCryptoConfigError=Exception,
        encrypt_apigw_secret=lambda s: f"enc:{s}",
    )

    sys.modules.pop("handler_admin", None)
    import handler_admin

    return handler_admin


def _event(path: str, body: dict) -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/prod{path}",
        "body": json.dumps(body),
        "headers": {"Authorization": "Bearer tok"},
    }


def _super_admin_actor() -> dict:
    return {
        "id": 1,
        "client_id": 1,
        "email": "nuwa@nuwa.space",
        "full_name": "Super",
        "role_slug": "super_admin",
    }


def test_clients_list_route() -> None:
    ha = _load_handler_admin()
    with mock.patch.object(ha, "fetch_user_with_role", return_value=_super_admin_actor()):
        with mock.patch.object(
            ha,
            "clients_list",
            return_value={"success": True, "clients": [], "stats": {}},
        ) as mock_list:
            body = {"clientId": 1, "userId": 1}
            out = ha.handler(_event("/v1/clients/list", body), None)
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["success"] is True
    mock_list.assert_called_once_with(body)


def test_admin_users_invite_route() -> None:
    ha = _load_handler_admin()
    with mock.patch.object(ha, "fetch_user_with_role", return_value=_super_admin_actor()):
        with mock.patch.object(
            ha,
            "admin_users_invite",
            return_value={"success": True, "user": {"id": 2}, "tempPassword": "x"},
        ) as mock_invite:
            body = {
                "clientId": 1,
                "userId": 1,
                "email": "uli@nuwa.space",
                "name": "Uli",
                "role": "analyst",
            }
            out = ha.handler(_event("/v1/admin/users/invite", body), None)
    assert out["statusCode"] == 201
    mock_invite.assert_called_once_with(body)


def test_admin_users_list_platform_for_super_admin() -> None:
    ha = _load_handler_admin()
    with mock.patch.object(ha, "fetch_user_with_role", return_value=_super_admin_actor()):
        with mock.patch.object(
            ha,
            "admin_users_list_platform",
            return_value={"success": True, "users": []},
        ) as mock_list:
            body = {"clientId": 1, "userId": 1, "search": "uli"}
            out = ha.handler(_event("/v1/admin/users/list", body), None)
    assert out["statusCode"] == 200
    mock_list.assert_called_once_with(body)


def test_admin_users_list_with_target_stays_on_platform() -> None:
    ha = _load_handler_admin()
    with mock.patch.object(ha, "fetch_user_with_role", return_value=_super_admin_actor()):
        with mock.patch.object(
            ha,
            "admin_users_list_platform",
            return_value={"success": True, "users": []},
        ) as mock_list:
            with mock.patch.object(ha, "users_list") as legacy:
                body = {"clientId": 1, "userId": 1, "targetClientId": 4}
                out = ha.handler(_event("/v1/admin/users/list", body), None)
    assert out["statusCode"] == 200
    mock_list.assert_called_once_with(body)
    legacy.assert_not_called()


def test_admin_users_delete_hard_deletes_for_super_admin() -> None:
    ha = _load_handler_admin()
    with mock.patch.object(ha, "fetch_user_with_role", return_value=_super_admin_actor()):
        with mock.patch.object(
            ha,
            "admin_users_delete_platform",
            return_value={"success": True, "deleted": True},
        ) as mock_delete:
            with mock.patch.object(ha, "users_delete") as legacy:
                body = {"clientId": 1, "userId": 1, "targetUserId": 5}
                out = ha.handler(_event("/v1/admin/users/delete", body), None)
    assert out["statusCode"] == 200
    mock_delete.assert_called_once_with(body, fallback_user_id=1)
    legacy.assert_not_called()


def test_clients_list_forbidden_for_admin() -> None:
    ha = _load_handler_admin()
    admin_actor = {
        "id": 2,
        "client_id": 1,
        "role_slug": "admin",
        "email": "a@b.com",
        "full_name": "Admin",
    }

    def deny(_actor: dict) -> None:
        raise ha.SupabaseRestError(403, "Solo super_admin.")

    with mock.patch.object(ha, "fetch_user_with_role", return_value=admin_actor):
        with mock.patch.object(ha, "require_super_admin", side_effect=deny):
            with mock.patch.object(ha, "clients_list") as mock_clients_list:
                body = {"clientId": 1, "userId": 2}
                out = ha.handler(_event("/v1/clients/list", body), None)
    assert out["statusCode"] == 403
    mock_clients_list.assert_not_called()
