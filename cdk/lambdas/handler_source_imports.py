"""
Source imports API (background processing of uploaded source files).

POST /v1/sources/imports/create  {clientId,userId,sourceId,fileName,contentType,fileSize,mode,config}
     → {import, upload: {url, headers, expiresIn}}  (PUT the file to url, then /start)
POST /v1/sources/imports/start   {clientId,userId,importId} → queued; the worker Lambda runs it
POST /v1/sources/imports/get     {clientId,userId,importId} → {import} (status, progress, result)
POST /v1/sources/imports/list    {clientId,userId,sourceId?,limit?} → {imports}

mode: sync (the file is the whole list), incremental (new / changed records) or replace.
config: headerRowIndex, sheetName, columnMapping {header: role}, keyField, riskLevel, visibility,
entityType, sourceDate. The original file stays in S3 (clients/<cid>/source-imports/...).
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from typing import Any

import boto3

from nuwa_api_auth import jwt_allows_client, jwt_matches_actor_body, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_http import json_response
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_s3_documents import head_object, presigned_put_url
import source_import_db as db

MODES = ("sync", "incremental", "replace")
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GB


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        out = json.loads(raw) if isinstance(raw, str) else {}
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


def _err(status: int, code: str, message: str) -> dict[str, Any]:
    return json_response(status, {"success": False, "code": code, "message": message})


def _safe_file_name(name: str) -> str:
    base = os.path.basename(name or "archivo").strip() or "archivo"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:180]


def _clean_config(raw: Any) -> dict[str, Any]:
    cfg = raw if isinstance(raw, dict) else {}
    mapping = cfg.get("columnMapping") if isinstance(cfg.get("columnMapping"), dict) else {}
    out: dict[str, Any] = {
        "headerRowIndex": max(0, int(cfg.get("headerRowIndex") or 0)),
        "columnMapping": {str(k)[:300]: str(v)[:300] for k, v in mapping.items()},
        "keyField": str(cfg.get("keyField") or "")[:300],
    }
    for k in ("sheetName", "visibility", "entityType", "sourceDate"):
        if cfg.get(k):
            out[k] = str(cfg[k])[:300]
    if cfg.get("riskLevel") is not None:
        out["riskLevel"] = int(cfg["riskLevel"])
    return out


def _can_see(claims: dict[str, Any], row: dict[str, Any]) -> bool:
    return claims.get("role") == "super_admin" or int(row["client_id"]) == int(claims.get("cid"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("source_imports", event, context)
    if (event.get("httpMethod") or "POST").upper() == "OPTIONS":
        return json_response(200, {"message": "ok"})
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _err(503, "BACKEND_NOT_CONFIGURED", str(e))

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _err(401, "UNAUTHORIZED", claims)
    body = _body(event)
    try:
        cid = int(body["clientId"])
        int(body["userId"])
    except (KeyError, TypeError, ValueError):
        return _err(400, "BAD_REQUEST", "clientId y userId requeridos (enteros).")
    if not jwt_matches_actor_body(claims, body) or not jwt_allows_client(claims, cid):
        return _err(403, "FORBIDDEN", "clientId y userId deben coincidir con el JWT.")
    is_super = claims.get("role") == "super_admin"

    action = (event.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
    conn = db.connect(autocommit=True)
    try:
        if action == "create":
            return _create(conn, body, cid, is_super)
        if action == "start":
            return _start(conn, body, claims)
        if action == "get":
            row = db.get_import(conn, str(body.get("importId") or ""))
            if row is None or not _can_see(claims, row):
                return _err(404, "NOT_FOUND", "Carga no encontrada.")
            return json_response(200, {"success": True, "import": db.to_api(row)})
        if action == "list":
            sid = body.get("sourceId")
            rows = db.list_imports(
                conn,
                client_id=None if is_super else cid,
                source_id=int(sid) if sid not in (None, "") else None,
                limit=max(1, min(int(body.get("limit") or 20), 100)),
            )
            return json_response(200, {"success": True, "imports": [db.to_api(r) for r in rows]})
        return _err(404, "NOT_FOUND", f"Ruta no encontrada: {action}")
    except (ValueError, TypeError) as e:
        return _err(400, "BAD_REQUEST", str(e))
    finally:
        conn.close()


def _create(conn, body: dict[str, Any], cid: int, is_super: bool) -> dict[str, Any]:
    try:
        source_id = int(body["sourceId"])
    except (KeyError, TypeError, ValueError):
        return _err(400, "BAD_REQUEST", "sourceId requerido (entero).")
    mode = str(body.get("mode") or "")
    if mode not in MODES:
        return _err(400, "BAD_REQUEST", "mode debe ser sync, incremental o replace.")
    file_name = str(body.get("fileName") or "").strip()
    if not file_name:
        return _err(400, "BAD_REQUEST", "fileName requerido.")
    size = int(body.get("fileSize") or 0)
    if size > MAX_FILE_BYTES:
        return _err(400, "FILE_TOO_LARGE", "El archivo supera 1 GB.")
    source = db.fetch_source(conn, source_id)
    if source is None:
        return _err(404, "NOT_FOUND", "Fuente no encontrada.")
    if not is_super and int(source["client_id"]) != cid:
        return _err(403, "FORBIDDEN", "Sin permiso para cargar en esta fuente.")

    import_id = str(uuid.uuid4())
    content_type = str(body.get("contentType") or "application/octet-stream")[:200]
    s3_key = f"clients/{int(source['client_id'])}/source-imports/{source_id}/{import_id}/{_safe_file_name(file_name)}"
    row = db.create_import(
        conn,
        id=import_id,
        source_id=source_id,
        client_id=int(source["client_id"]),
        user_id=int(body["userId"]),
        mode=mode,
        s3_key=s3_key,
        file_name=file_name[:300],
        file_size=size or None,
        content_type=content_type,
        config=_clean_config(body.get("config")),
    )
    url, headers, ttl = presigned_put_url(s3_key=s3_key, mime_type=content_type)
    log_phase("source_imports", f"create {import_id} source={source_id} mode={mode} file={file_name}")
    return json_response(
        200, {"success": True, "import": db.to_api(row), "upload": {"url": url, "headers": headers, "expiresIn": ttl}}
    )


def _start(conn, body: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    import_id = str(body.get("importId") or "")
    row = db.get_import(conn, import_id)
    if row is None or not _can_see(claims, row):
        return _err(404, "NOT_FOUND", "Carga no encontrada.")
    if row["status"] != "pending_upload":
        return json_response(200, {"success": True, "import": db.to_api(row)})
    if head_object(row["s3_key"]) is None:
        return _err(409, "NOT_UPLOADED", "El archivo aún no está en S3: súbalo con la URL de /create.")
    db.set_status(conn, import_id, "queued", progress={"message": "En cola"})
    boto3.client("lambda").invoke(
        FunctionName=os.environ["NUWA_SOURCE_IMPORT_WORKER"],
        InvocationType="Event",
        Payload=json.dumps({"importId": import_id}).encode("utf-8"),
    )
    log_phase("source_imports", f"start {import_id}")
    return json_response(200, {"success": True, "import": db.to_api(db.get_import(conn, import_id))})
