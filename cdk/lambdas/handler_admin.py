"""Administración: compañías, roles, usuarios (RBAC)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from nuwa_api_auth import jwt_matches_actor_body, require_jwt
from nuwa_app_crypto import AppCryptoConfigError, encrypt_apigw_secret
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response
from nuwa_obs_log import log_await, log_done, log_handler_enter, log_phase
from nuwa_password import hash_password
from nuwa_rbac import can_manage_company, can_manage_users
from nuwa_supabase import fetch_user_with_role, rest_json


@lru_cache(maxsize=1)
def _usage_plan_id() -> str:
    """Evita referenciar el id del usage plan en CDK (dependencia circular con deployment)."""
    pid = (os.environ.get("NUWA_APIGW_USAGE_PLAN_ID") or "").strip()
    if pid:
        return pid
    name = (os.environ.get("NUWA_APIGW_USAGE_PLAN_NAME") or "").strip()
    if not name:
        raise RuntimeError(
            "Configura NUWA_APIGW_USAGE_PLAN_NAME (o NUWA_APIGW_USAGE_PLAN_ID) en la Lambda admin."
        )
    gw = boto3.client("apigateway")
    position: str | None = None
    log_await("apigateway", "get_usage_plans", f"name={name!r}")
    while True:
        kwargs: dict[str, Any] = {"limit": 500}
        if position:
            kwargs["position"] = position
        r = gw.get_usage_plans(**kwargs)
        for item in r.get("items", []):
            if item.get("name") == name:
                log_done("apigateway", "get_usage_plans", f"id={item['id']}")
                return str(item["id"])
        position = r.get("position")
        if not position:
            break
    raise RuntimeError(f"No se encontró API Gateway usage plan con name={name!r}")


def _create_tenant_api_key(client_id: int) -> tuple[str, str]:
    """Crea API Key en API Gateway; devuelve (key_id, key_value). key_value solo existe aquí."""
    usage_plan_id = _usage_plan_id()
    prefix = (os.environ.get("NUWA_RESOURCE_PREFIX") or "nuwa2").strip()
    gw = boto3.client("apigateway")
    name = f"{prefix}-tenant-{client_id}"
    log_await("apigateway", "create_api_key", name)
    r = gw.create_api_key(
        name=name,
        description=f"Nuwa tenant client_id={client_id}",
        enabled=True,
        generateDistinctId=True,
    )
    kid = r["id"]
    val = r.get("value")
    if not val:
        gw.delete_api_key(apiKey=kid)
        raise RuntimeError("API Gateway no devolvió el valor de la API key (reintentar).")
    log_done("apigateway", "create_api_key", f"id={kid}")
    try:
        log_await("apigateway", "create_usage_plan_key", f"plan={usage_plan_id} key={kid}")
        gw.create_usage_plan_key(usagePlanId=usage_plan_id, keyId=kid, keyType="API_KEY")
        log_done("apigateway", "create_usage_plan_key", "")
    except ClientError:
        gw.delete_api_key(apiKey=kid)
        raise
    return kid, val


def _delete_tenant_api_key(key_id: str) -> None:
    log_await("apigateway", "delete_api_key", key_id)
    boto3.client("apigateway").delete_api_key(apiKey=key_id)
    log_done("apigateway", "delete_api_key", key_id)


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return json_response(status, body)


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _actor(claims: dict[str, Any], body: dict[str, Any]) -> dict[str, Any] | None:
    if not jwt_matches_actor_body(claims, body):
        return None
    try:
        cid = int(body.get("clientId"))
        uid = int(body.get("userId"))
    except (TypeError, ValueError):
        return None
    a = fetch_user_with_role(user_id=uid)
    if not a:
        return None
    if a["role_slug"] == "super_admin":
        return a
    if a["client_id"] != cid:
        return None
    return a


# --- companies ---
_COMPANY_LIST_SELECT = "id,client_id,name,details,apigw_key_id,created_at,updated_at"


def companies_list(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if actor["role_slug"] == "super_admin":
        rows = rest_json(
            "GET",
            "companies",
            query=f"select={_COMPANY_LIST_SELECT}&order=id.asc",
        )
    else:
        rows = rest_json(
            "GET",
            "companies",
            query=f"client_id=eq.{actor['client_id']}&select={_COMPANY_LIST_SELECT}",
        )
    return _resp(200, {"items": rows or []})


def companies_create(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if actor["role_slug"] != "super_admin":
        return _resp(403, {"code": "FORBIDDEN", "message": "Solo super_admin crea compañías."})
    row = {
        "client_id": int(body["newClientId"]),
        "name": body["name"],
        "details": body.get("details") or {},
    }
    out = rest_json("POST", "companies", body=row)
    item = out[0] if isinstance(out, list) else out
    cid = int(item["client_id"])
    key_id: str | None = None
    key_value: str | None = None
    try:
        key_id, key_value = _create_tenant_api_key(cid)
    except (ClientError, RuntimeError) as e:
        rest_json("DELETE", "companies", query=f"client_id=eq.{cid}")
        return _resp(
            502,
            {
                "code": "APIGW_PROVISION_FAILED",
                "message": str(e),
            },
        )
    try:
        try:
            secret_stored = encrypt_apigw_secret(key_value)
        except AppCryptoConfigError as e:
            try:
                if key_id:
                    _delete_tenant_api_key(key_id)
            except ClientError:
                pass
            rest_json("DELETE", "companies", query=f"client_id=eq.{cid}")
            return _resp(503, {"code": "CRYPTO_NOT_CONFIGURED", "message": str(e)})
        rest_json(
            "PATCH",
            "companies",
            query=f"client_id=eq.{cid}",
            body={"apigw_key_id": key_id, "apigw_key_secret": secret_stored},
        )
    except SupabaseRestError as e:
        try:
            if key_id:
                _delete_tenant_api_key(key_id)
        except ClientError:
            pass
        rest_json("DELETE", "companies", query=f"client_id=eq.{cid}")
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": e.body},
        )
    merged = {**item, "apigw_key_id": key_id}
    return _resp(
        201,
        {
            "item": merged,
            "apiKey": key_value,
            "apiKeyWarning": "Guarda apiKey en un gestor seguro; también queda en companies.apigw_key_secret para login.",
        },
    )


def companies_update(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    if not can_manage_company(actor, target):
        return _resp(403, {"code": "FORBIDDEN", "message": "Sin permiso."})
    patch: dict[str, Any] = {}
    if "name" in body:
        patch["name"] = body["name"]
    if "details" in body:
        patch["details"] = body["details"]
    if not patch:
        return _resp(400, {"code": "BAD_REQUEST", "message": "name o details"})
    q = f"client_id=eq.{target}"
    out = rest_json("PATCH", "companies", query=q, body=patch)
    return _resp(200, {"item": out[0] if isinstance(out, list) else out})


def companies_delete(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if actor["role_slug"] != "super_admin":
        return _resp(403, {"code": "FORBIDDEN", "message": "Solo super_admin elimina compañías."})
    target = int(body["targetClientId"])
    rows = rest_json(
        "GET",
        "companies",
        query=f"client_id=eq.{target}&select=apigw_key_id&limit=1",
    )
    kid = None
    if rows:
        r0 = rows[0] if isinstance(rows, list) else rows
        kid = r0.get("apigw_key_id")
    rest_json("DELETE", "companies", query=f"client_id=eq.{target}")
    if kid:
        try:
            _delete_tenant_api_key(str(kid))
        except ClientError as e:
            return _resp(
                200,
                {
                    "deleted": True,
                    "targetClientId": target,
                    "apiKeyDeleteWarning": str(e),
                },
            )
    return _resp(200, {"deleted": True, "targetClientId": target})


# --- roles ---
def roles_list(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    rows = rest_json("GET", "nuwa_roles", query="select=*&order=id.asc")
    return _resp(200, {"items": rows or []})


# --- users ---
def users_list(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    target = int(body.get("targetClientId") or actor["client_id"])
    if not can_manage_users(actor, target):
        return _resp(403, {"code": "FORBIDDEN", "message": "Sin permiso."})
    rows = rest_json(
        "GET",
        "nuwa_users",
        query=f"client_id=eq.{target}&select=id,client_id,email,full_name,role_id,is_active,created_at",
    )
    return _resp(200, {"items": rows or []})


def users_create(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    target = int(body.get("targetClientId") or actor["client_id"])
    if not can_manage_users(actor, target):
        return _resp(403, {"code": "FORBIDDEN", "message": "Sin permiso."})
    pwd = body.get("password") or ""
    if len(pwd) < 8:
        return _resp(400, {"code": "BAD_REQUEST", "message": "password mínimo 8 caracteres."})
    row = {
        "client_id": target,
        "email": body["email"].strip().lower(),
        "password_hash": hash_password(pwd),
        "full_name": body["fullName"],
        "role_id": int(body["roleId"]),
        "is_active": body.get("isActive", True),
    }
    out = rest_json("POST", "nuwa_users", body=row)
    return _resp(201, {"item": out[0] if isinstance(out, list) else out})


def users_update(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    uid = int(body["targetUserId"])
    rows = rest_json("GET", "nuwa_users", query=f"id=eq.{uid}&select=*")
    if not rows:
        return _resp(404, {"code": "NOT_FOUND", "message": "Usuario no encontrado."})
    u = rows[0]
    if not can_manage_users(actor, int(u["client_id"])):
        return _resp(403, {"code": "FORBIDDEN", "message": "Sin permiso."})
    patch: dict[str, Any] = {}
    if "fullName" in body:
        patch["full_name"] = body["fullName"]
    if "roleId" in body:
        patch["role_id"] = int(body["roleId"])
    if "isActive" in body:
        patch["is_active"] = bool(body["isActive"])
    if body.get("password"):
        patch["password_hash"] = hash_password(body["password"])
    if not patch:
        return _resp(400, {"code": "BAD_REQUEST", "message": "Nada que actualizar."})
    out = rest_json("PATCH", "nuwa_users", query=f"id=eq.{uid}", body=patch)
    return _resp(200, {"item": out[0] if isinstance(out, list) else out})


def users_delete(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return users_update(actor, {**body, "isActive": False, "targetUserId": body["targetUserId"]})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("admin", event, context)
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})

    path = (event.get("path") or "").rstrip("/")
    method = (event.get("httpMethod") or "POST").upper()
    log_phase("admin_route", f"{method} {path}")
    if method != "POST":
        return _resp(405, {"code": "METHOD_NOT_ALLOWED", "message": "Usar POST"})

    body = _body(event)
    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})

    actor = _actor(claims, body)
    if not actor:
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId/userId inválidos o no coinciden con el token."})

    try:
        if path.endswith("/admin/companies/list"):
            return companies_list(actor, body)
        if path.endswith("/admin/companies/create"):
            return companies_create(actor, body)
        if path.endswith("/admin/companies/update"):
            return companies_update(actor, body)
        if path.endswith("/admin/companies/delete"):
            return companies_delete(actor, body)
        if path.endswith("/admin/roles/list"):
            return roles_list(actor, body)
        if path.endswith("/admin/users/list"):
            return users_list(actor, body)
        if path.endswith("/admin/users/create"):
            return users_create(actor, body)
        if path.endswith("/admin/users/update"):
            return users_update(actor, body)
        if path.endswith("/admin/users/delete"):
            return users_delete(actor, body)
        return _resp(404, {"code": "NOT_FOUND", "message": path})
    except SupabaseRestError as e:
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": e.body},
        )
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": str(e)})
