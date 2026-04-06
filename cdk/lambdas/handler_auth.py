"""Login de aplicación (email + password) — sin API Key en gateway."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from botocore.exceptions import ClientError

from nuwa_app_crypto import AppCryptoConfigError
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_jwt import mint_access_token
from nuwa_password import verify_password
from nuwa_supabase import rest_json

_USER_SELECT = "id,client_id,email,password_hash,full_name,role_id,is_active"
_COMPANY_SELECT_LOGIN = "name,apigw_key_secret"

_LOG = logging.getLogger("nuwa.obs")


def _aws_client_error_body(exc: ClientError) -> dict[str, Any]:
    err = (exc.response or {}).get("Error") or {}
    return {
        "code": "AWS_SDK_ERROR",
        "awsErrorCode": err.get("Code", ""),
        "message": err.get("Message") or str(exc),
    }


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


def _login(body: dict[str, Any]) -> dict[str, Any]:
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return _resp(400, {"code": "BAD_REQUEST", "message": "email y password son requeridos."})

    hint_cid = body.get("clientId")
    try:
        hint_cid_i = int(hint_cid) if hint_cid is not None and hint_cid != "" else None
    except (TypeError, ValueError):
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId debe ser entero si se envía."})

    rows = rest_json(
        "GET",
        "nuwa_users",
        query=f"email=eq.{quote(email, safe='')}&select={_USER_SELECT}",
    )
    if not rows:
        return _resp(401, {"code": "UNAUTHORIZED", "message": "Credenciales inválidas."})
    if not isinstance(rows, list):
        rows = [rows]

    active = [r for r in rows if r.get("is_active", True)]
    if not active:
        return _resp(401, {"code": "UNAUTHORIZED", "message": "Credenciales inválidas."})

    if len(active) > 1:
        if hint_cid_i is None:
            return _resp(
                400,
                {
                    "code": "CLIENT_ID_REQUIRED",
                    "message": "Hay varias cuentas con este email; envía clientId.",
                },
            )
        active = [r for r in active if int(r["client_id"]) == hint_cid_i]
        if len(active) != 1:
            return _resp(401, {"code": "UNAUTHORIZED", "message": "Credenciales inválidas."})

    u = active[0]
    if not verify_password(password, u.get("password_hash") or ""):
        return _resp(401, {"code": "UNAUTHORIZED", "message": "Credenciales inválidas."})

    roles = rest_json(
        "GET",
        "nuwa_roles",
        query=f"id=eq.{u['role_id']}&select=id,slug,name",
    )
    if not roles:
        return _resp(500, {"code": "INTERNAL", "message": "Rol no encontrado."})
    r0 = roles[0] if isinstance(roles, list) else roles

    cid = int(u["client_id"])
    comps = rest_json(
        "GET",
        "companies",
        query=f"client_id=eq.{cid}&select={_COMPANY_SELECT_LOGIN}&limit=1",
    )
    company_name: str | None = None
    if comps:
        c0 = comps[0] if isinstance(comps, list) else comps
        company_name = c0.get("name")

    try:
        token, expires_in = mint_access_token(
            user_id=int(u["id"]),
            client_id=cid,
            role_slug=str(r0["slug"]),
            email=str(u["email"]),
        )
    except AppCryptoConfigError as e:
        return _resp(503, {"code": "AUTH_NOT_CONFIGURED", "message": str(e)})

    out_user = {
        "id": int(u["id"]),
        "clientId": cid,
        "email": u["email"],
        "fullName": u.get("full_name") or "",
        "roleId": int(u["role_id"]),
        "roleSlug": r0["slug"],
        "roleName": r0.get("name") or "",
        "isActive": bool(u.get("is_active", True)),
    }
    return _resp(
        200,
        {
            "user": out_user,
            "company": {"clientId": cid, "name": company_name},
            "accessToken": token,
            "tokenType": "Bearer",
            "expiresIn": expires_in,
        },
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("auth", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})
    except ClientError as e:
        _LOG.exception("auth ensure_data_backend: AWS SDK error")
        return _resp(502, _aws_client_error_body(e))

    path = (event.get("path") or "").rstrip("/")
    if method != "POST" or not path.endswith("/auth/login"):
        return _resp(404, {"code": "NOT_FOUND", "message": path or "/"})

    try:
        log_phase("auth_login", "start _login")
        return _login(_body(event))
    except SupabaseRestError as e:
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": e.body},
        )
    except ClientError as e:
        _LOG.exception("auth login: AWS SDK error (p. ej. Secrets Manager / KMS)")
        return _resp(502, _aws_client_error_body(e))
    except Exception as e:
        _LOG.exception("auth login: error no manejado")
        return _resp(
            500,
            {
                "code": "INTERNAL",
                "message": str(e),
                "errorType": type(e).__name__,
            },
        )
