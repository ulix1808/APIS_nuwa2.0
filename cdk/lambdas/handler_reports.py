"""
Reportes (PostgREST Supabase o PostgreSQL directo según configuración).

GET|POST /v1/reports/get — query string (body opcional en POST, ignorado):
  - Al menos uno de: clientId, userId, folio
  - Opcional: includePayload, nextKey, limit
  - RBAC opcional: actorUserId (+ actorClientId); si se envía, se filtra con nuwa_users.

POST /v1/reports/save — body { clientId, userId, report | reporte } (objeto con folio y JSON editorial completo).
POST|PUT /v1/reports/update — body { folio, report | reporte, clientId? }.
POST /v1/reports/delete — borrado lógico (status=deleted).
"""

from __future__ import annotations

import json
from typing import Any

from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import CORS_HEADERS
from nuwa_rbac import can_read_report
from nuwa_supabase import fetch_user_with_role, rest_json
from nuwa_api_auth import effective_tenant_scope, jwt_allows_client, require_jwt
from nuwa_obs_log import log_handler_enter, log_phase

from report_helpers import (
    db_row_to_api_summary,
    decode_next_key,
    encode_next_key,
    extract_report_metadata,
    metadata_to_db_row,
    now_iso_z,
    report_payload_from_body,
    validate_report_for_save,
    validate_report_for_update,
)


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    qs = event.get("queryStringParameters") or {}
    return {k: (v if v is not None else "") for k, v in qs.items()}


def _int(q: dict[str, str], key: str) -> int | None:
    v = q.get(key)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _truthy(q: dict[str, str], key: str) -> bool:
    return (q.get(key) or "").lower() in ("1", "true", "yes")


def _apply_rbac(actor: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not actor:
        return rows
    return [r for r in rows if can_read_report(actor, r)]


def _select_cols_for_list() -> str:
    return (
        "id,folio,client_id,created_by_user_id,entidad,tipo_consulta,fecha,hora,"
        "nivel_riesgo,nivel_riesgo_numerico,total_listas_original,total_listas_activas,"
        "total_descartadas,es_actualizacion,total_listas,total_menciones,grok_resumen,"
        "grok_falsos_positivos,grok_confirmados,created_at,updated_at,status,"
        "report_json"
    )


def handle_get(event: dict[str, Any]) -> dict[str, Any]:
    q = _query_params(event)
    client_id = _int(q, "clientId")
    user_id = _int(q, "userId")
    folio = (q.get("folio") or "").strip()
    include_payload = _truthy(q, "includePayload")
    next_tok = q.get("nextKey") or ""
    lim = _int(q, "limit") or 20
    lim = max(1, min(lim, 100))
    offset = decode_next_key(next_tok) or 0

    jwt_msg = require_jwt(event)
    if isinstance(jwt_msg, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": jwt_msg})
    bound = effective_tenant_scope(jwt_msg)
    if bound is not None:
        if client_id is not None and client_id != bound:
            return _resp(
                403,
                {"code": "FORBIDDEN", "message": "clientId no autorizado para este token."},
            )
        if user_id is not None:
            urows = rest_json(
                "GET",
                "nuwa_users",
                query=f"id=eq.{user_id}&select=client_id&limit=1",
            )
            if not urows or int(urows[0]["client_id"]) != bound:
                return _resp(
                    403,
                    {"code": "FORBIDDEN", "message": "userId no pertenece al tenant del token."},
                )

    actor_user = _int(q, "actorUserId")
    actor_client = _int(q, "actorClientId")
    actor: dict[str, Any] | None = None
    if actor_user is not None:
        actor = fetch_user_with_role(user_id=actor_user)
        if not actor:
            return _resp(403, {"message": "actorUserId no válido o inactivo"})
        if actor["role_slug"] != "super_admin":
            if actor_client is None or actor["client_id"] != actor_client:
                return _resp(403, {"message": "actorClientId debe coincidir con la compañía del actor"})

    if client_id is None and user_id is None and not folio:
        return _resp(
            400,
            {"message": "Debes enviar al menos uno de estos parámetros: clientId, userId o folio"},
        )

    base = f"select={_select_cols_for_list()}&status=eq.active&order=created_at.desc"

    try:
        if folio:
            fparts = [base, f"folio=eq.{folio}"]
            if bound is not None:
                fparts.append(f"client_id=eq.{bound}")
            elif client_id is not None:
                fparts.append(f"client_id=eq.{client_id}")
            if user_id is not None:
                fparts.append(f"created_by_user_id=eq.{user_id}")
            query = "&".join(fparts) + "&limit=20&offset=0"
            rows = rest_json("GET", "reports", query=query)
            if not isinstance(rows, list):
                rows = [rows] if rows else []
            rows = _apply_rbac(actor, rows)
            if not rows:
                return _resp(404, {"message": f"No se encontró reporte con folio {folio}"})
            row = rows[0]
            if include_payload:
                full = db_row_to_api_summary(row)
                full["payload"] = row.get("report_json")
                return _resp(200, full)
            return _resp(200, db_row_to_api_summary(row))

        parts = [base]
        if bound is not None:
            parts.append(f"client_id=eq.{bound}")
            if user_id is not None:
                parts.append(f"created_by_user_id=eq.{user_id}")
        elif client_id is not None and user_id is not None:
            parts.append(f"client_id=eq.{client_id}")
            parts.append(f"created_by_user_id=eq.{user_id}")
        elif client_id is not None:
            parts.append(f"client_id=eq.{client_id}")
        elif user_id is not None:
            parts.append(f"created_by_user_id=eq.{user_id}")

        query = "&".join(parts) + f"&limit={lim + 1}&offset={offset}"
        rows = rest_json("GET", "reports", query=query)
        if not isinstance(rows, list):
            rows = [rows] if rows else []
        rows = _apply_rbac(actor, rows)
        has_more = len(rows) > lim
        page = rows[:lim]
        summaries = [db_row_to_api_summary(r) for r in page]
        if include_payload:
            for i, r in enumerate(page):
                summaries[i] = {**summaries[i], "payload": r.get("report_json")}
        return _resp(
            200,
            {
                "items": summaries,
                "count": len(summaries),
                "nextKey": encode_next_key(offset + lim) if has_more else None,
                "filters": {
                    k: v
                    for k, v in {
                        "clientId": client_id,
                        "userId": user_id,
                    }.items()
                    if v is not None
                },
            },
        )
    except SupabaseRestError as e:
        return _resp(e.status if 400 <= e.status < 600 else 500, {"message": e.body})


def _folio_exists_globally(folio: str) -> bool:
    rows = rest_json(
        "GET",
        "reports",
        query=f"folio=eq.{folio}&select=id&status=eq.active&limit=1",
    )
    return bool(rows)


def handle_save(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    client_id = body.get("clientId")
    user_id = body.get("userId")
    reporte = report_payload_from_body(body)
    err = validate_report_for_save(client_id, user_id, reporte)
    if err:
        return _resp(400, {"message": err})
    client_id = int(client_id)
    user_id = int(user_id)
    folio = str(reporte.get("folio"))

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})
    if not jwt_allows_client(claims, client_id):
        return _resp(403, {"code": "FORBIDDEN", "message": "clientId no permitido para este token."})
    if claims.get("role") == "user" and int(claims["sub"]) != user_id:
        return _resp(
            403,
            {"code": "FORBIDDEN", "message": "Con rol user solo puedes guardar con tu propio userId."},
        )

    ucheck = rest_json("GET", "nuwa_users", query=f"id=eq.{user_id}&select=id&limit=1")
    if not ucheck:
        return _resp(
            400,
            {"message": f"userId {user_id} no existe en nuwa_users; registra el usuario antes de guardar."},
        )

    if _folio_exists_globally(folio):
        return _resp(409, {"message": f"Ya existe un reporte con folio {folio}", "folio": folio})

    meta = extract_report_metadata(reporte)
    db_meta = metadata_to_db_row(meta)
    created_at = now_iso_z()
    row: dict[str, Any] = {
        "folio": folio,
        "client_id": client_id,
        "created_by_user_id": user_id,
        "report_json": reporte,
        "search_context": {},
        "status": "active",
        **db_meta,
    }
    out = rest_json("POST", "reports", body=row)
    item = out[0] if isinstance(out, list) else out
    return _resp(
        201,
        {
            "message": "Reporte guardado correctamente",
            "folio": item.get("folio"),
            "clientId": client_id,
            "userId": user_id,
            "createdAt": item.get("created_at") or created_at,
        },
    )


def _find_report_rows(folio: str, client_id: int | None) -> list[dict[str, Any]]:
    q = f"folio=eq.{folio}&select=*"
    if client_id is not None:
        q += f"&client_id=eq.{client_id}"
    rows = rest_json("GET", "reports", query=q)
    if not isinstance(rows, list):
        return [rows] if rows else []
    return rows


def handle_update(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    folio = body.get("folio")
    reporte = report_payload_from_body(body)
    err = validate_report_for_update(folio, reporte)
    if err:
        return _resp(400, {"message": err})
    folio = str(folio)
    client_hint = body.get("clientId")
    cid = int(client_hint) if client_hint is not None else None
    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})
    bound = effective_tenant_scope(claims)
    if bound is not None:
        if cid is not None and cid != bound:
            return _resp(
                403,
                {"message": "El clientId no corresponde al token.", "code": "FORBIDDEN"},
            )

    rows = _find_report_rows(folio, cid)
    if bound is not None:
        rows = [r for r in rows if int(r["client_id"]) == bound]
    if not rows:
        return _resp(404, {"message": f"No se encontró reporte con folio {folio}"})
    if len(rows) > 1 and cid is None:
        return _resp(400, {"message": "Varios reportes con el mismo folio; envía clientId."})
    existing = rows[0]

    meta = extract_report_metadata(reporte)
    db_meta = metadata_to_db_row(meta)
    patch = {
        "report_json": reporte,
        "updated_at": now_iso_z(),
        **db_meta,
    }
    rid = existing["id"]
    out = rest_json("PATCH", "reports", query=f"id=eq.{rid}", body=patch)
    updated = out[0] if isinstance(out, list) else out
    return _resp(
        200,
        {
            "message": "Reporte actualizado correctamente",
            "folio": folio,
            "updatedAt": updated.get("updated_at"),
            "status": updated.get("status"),
        },
    )


def handle_delete(body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    folio = body.get("folio")
    client_hint = body.get("clientId")
    if not folio:
        return _resp(400, {"message": "folio es requerido"})
    cid = int(client_hint) if client_hint is not None else None
    claims = require_jwt(event)
    if isinstance(claims, str):
        return _resp(401, {"code": "UNAUTHORIZED", "message": claims})
    bound = effective_tenant_scope(claims)
    if bound is not None and cid is not None and cid != bound:
        return _resp(403, {"message": "El clientId no corresponde al token.", "code": "FORBIDDEN"})
    rows = _find_report_rows(str(folio), cid)
    if bound is not None:
        rows = [r for r in rows if int(r["client_id"]) == bound]
    if not rows:
        return _resp(404, {"message": "No encontrado"})
    rid = rows[0]["id"]
    rest_json(
        "PATCH",
        "reports",
        query=f"id=eq.{rid}",
        body={"status": "deleted", "updated_at": now_iso_z()},
    )
    return _resp(200, {"message": "Reporte eliminado", "folio": folio})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("reports", event, context)
    method = (event.get("httpMethod") or "GET").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"message": str(e), "code": "BACKEND_NOT_CONFIGURED"})

    path = (event.get("path") or "").rstrip("/")
    log_phase("reports_route", f"{method} {path}")

    try:
        if method in ("GET", "POST") and path.endswith("/reports/get"):
            return handle_get(event)
        body = _parse_json_body(event) if method != "GET" else {}
        if method == "POST" and path.endswith("/reports/save"):
            return handle_save(body, event)
        if method in ("POST", "PUT") and path.endswith("/reports/update"):
            return handle_update(body, event)
        if method == "POST" and path.endswith("/reports/delete"):
            return handle_delete(body, event)
        return _resp(404, {"message": "Ruta no encontrada", "method": method, "path": path})
    except SupabaseRestError as e:
        return _resp(
            e.status if 400 <= e.status < 600 else 500,
            {"message": e.body, "code": "DATA_BACKEND_ERROR"},
        )
    except json.JSONDecodeError:
        return _resp(400, {"message": "El body no contiene un JSON válido"})
    except Exception as e:
        return _resp(500, {"message": "Error interno", "error": str(e)})
