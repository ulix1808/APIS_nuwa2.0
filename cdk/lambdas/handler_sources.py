"""Catálogo de fuentes y categorías (`sources`, `source_categories`)."""

from __future__ import annotations

import base64
import json
from typing import Any

from nuwa_api_auth import jwt_allows_client, jwt_matches_actor_body, require_jwt
from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend
from nuwa_errors import SupabaseRestError
from nuwa_http import json_response, no_content_response
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_source_categories import (
    SourceCategoryError,
    create_category,
    get_category_by_id,
    list_categories,
    list_categories_catalog,
    validate_assignable_category,
)
from nuwa_sources import (
    _UPDATE_CATEGORY_OMIT,
    create_source,
    delete_source,
    get_source,
    list_sources,
    resolve_create_visibility,
    update_source,
)
from nuwa_supabase import fetch_user_with_role
from source_risk_level import RISK_LEVEL_API_MESSAGE, parse_source_risk_level


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return json_response(status, body)


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _bad(msg: str) -> dict[str, Any]:
    return _response(400, {"code": "BAD_REQUEST", "message": msg})


def _forbidden(msg: str) -> dict[str, Any]:
    return _response(403, {"code": "FORBIDDEN", "message": msg})


def _not_found(msg: str = "Recurso no encontrado.") -> dict[str, Any]:
    return _response(404, {"code": "NOT_FOUND", "message": msg})


def _require_actor(claims: dict[str, Any], body: dict[str, Any]) -> dict[str, Any] | None:
    if not jwt_matches_actor_body(claims, body):
        return _forbidden("clientId y userId deben coincidir con el JWT (sub / cid).")
    return None


def _parse_actor_ids(body: dict[str, Any]) -> tuple[int, int] | None:
    try:
        cid = int(body["clientId"])
        uid = int(body["userId"])
        return cid, uid
    except (KeyError, TypeError, ValueError):
        return None


def _validate_visibility(v: Any) -> str | None:
    if isinstance(v, str) and v in ("public", "private"):
        return v
    return None


def _parse_include_category(body: dict[str, Any]) -> bool:
    return bool(body.get("includeCategory"))


def _parse_include_inactive_categories(event: dict[str, Any], body: dict[str, Any]) -> bool:
    qs = event.get("queryStringParameters") or {}
    raw = qs.get("includeInactiveCategories")
    if raw is None and "includeInactiveCategories" in body:
        raw = body.get("includeInactiveCategories")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes")


def _category_id_from_event(event: dict[str, Any], path: str) -> int | None:
    pp = event.get("pathParameters") or {}
    rid = pp.get("id")
    if rid is not None and str(rid).isdigit():
        return int(rid)
    if "/source-category-id/" in path:
        tail = path.split("/source-category-id/")[-1]
        if tail.isdigit():
            return int(tail)
    return None


def _handle_category_create(claims: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    err = _require_actor(claims, body)
    if err:
        return err
    ids = _parse_actor_ids(body)
    if not ids:
        return _bad("clientId y userId requeridos (enteros).")
    _, uid = ids
    actor = fetch_user_with_role(user_id=uid)
    if not actor or actor["role_slug"] != "super_admin":
        return _forbidden("Solo super_admin puede crear categorías.")
    slug = body.get("slug")
    name_es = body.get("nameEs")
    name_en = body.get("nameEn")
    is_active = bool(body.get("isActive", True))
    try:
        row = create_category(slug=slug, name_es=name_es, name_en=name_en, is_active=is_active)
    except SourceCategoryError as e:
        return _response(400, {"code": e.code, "message": e.message})
    log_phase("source_category_create", f"id={row.get('id')}")
    return json_response(201, row)


def _handle_category_list(claims: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    err = _require_actor(claims, body)
    if err:
        return err
    ids = _parse_actor_ids(body)
    if not ids:
        return _bad("clientId y userId requeridos (enteros).")
    _, uid = ids
    actor = fetch_user_with_role(user_id=uid)
    if not actor or actor["role_slug"] not in ("super_admin", "admin"):
        return _forbidden("Solo super_admin o admin pueden listar categorías.")
    is_active: bool | None
    if "isActive" in body:
        if body["isActive"] is None:
            is_active = None
        else:
            is_active = bool(body["isActive"])
    else:
        is_active = None
    lim = int(body.get("limit") or 50)
    off = int(body.get("offset") or 0)
    total, items = list_categories(is_active=is_active, limit=lim, offset=off)
    out: dict[str, Any] = {"items": items}
    if total is not None:
        out["total"] = total
    return _response(200, out)


def _handle_category_get(event: dict[str, Any], claims: dict[str, Any], path: str) -> dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    try:
        body_like = {"clientId": int(qs["clientId"]), "userId": int(qs["userId"])}
    except (KeyError, TypeError, ValueError):
        return _bad("Query clientId y userId (enteros) son requeridos y deben alinear el JWT.")
    err = _require_actor(claims, body_like)
    if err:
        return err
    uid = body_like["userId"]
    actor = fetch_user_with_role(user_id=uid)
    if not actor or actor["role_slug"] not in ("super_admin", "admin"):
        return _forbidden("Solo super_admin o admin pueden consultar categorías.")
    cid = _category_id_from_event(event, path)
    if cid is None:
        return _bad("Id de categoría inválido en la ruta.")
    row = get_category_by_id(cid)
    if not row:
        return _not_found("Categoría no encontrada.")
    log_phase("source_category_get", f"id={cid}")
    return _response(200, row)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("sources", event, context)
    path = (event.get("path") or "").rstrip("/")
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _response(200, {"message": "ok"})

    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _response(
            503,
            {"code": "BACKEND_NOT_CONFIGURED", "message": str(e), "path": path},
        )

    claims = require_jwt(event)
    if isinstance(claims, str):
        return _response(401, {"code": "UNAUTHORIZED", "message": claims})

    is_super_admin = claims.get("role") == "super_admin"
    body = _body(event)

    try:
        if path.endswith("/source-category-id/create") and method == "POST":
            return _handle_category_create(claims, body)

        if path.endswith("/source-category-id/list") and method == "POST":
            return _handle_category_list(claims, body)

        if method == "GET" and "/source-category-id/" in path and not path.endswith("/create"):
            return _handle_category_get(event, claims, path)

        if path.endswith("/sources/delete"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            log_phase("sources_delete", f"id={sid}")
            st = delete_source(source_id=sid, viewer_client_id=cid, is_super_admin=is_super_admin)
            if st == "not_found":
                return _not_found("Fuente no encontrada.")
            if st == "forbidden":
                return _forbidden("Sin permiso para eliminar esta fuente.")
            return no_content_response()

        if path.endswith("/sources/update"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            name = body.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                return _bad("name no puede estar vacío.")
            name = name.strip() if isinstance(name, str) else None
            rl = None
            if "riskLevel" in body:
                rl = parse_source_risk_level(body.get("riskLevel"))
                if rl is None:
                    return _bad(RISK_LEVEL_API_MESSAGE)
            vis = None
            if "visibility" in body:
                vis = _validate_visibility(body.get("visibility"))
                if vis is None:
                    return _bad("visibility debe ser public o private.")
            meta = body.get("metadata")
            if meta is not None and not isinstance(meta, dict):
                return _bad("metadata debe ser un objeto JSON.")
            sc_kw: Any = _UPDATE_CATEGORY_OMIT
            if "sourceCategoryId" in body:
                raw_sc = body.get("sourceCategoryId")
                if raw_sc is None:
                    sc_kw = None
                else:
                    try:
                        sc_int = int(raw_sc)
                    except (TypeError, ValueError):
                        return _bad("sourceCategoryId debe ser un entero o null.")
                    try:
                        validate_assignable_category(sc_int)
                    except SourceCategoryError as e:
                        return _response(400, {"code": e.code, "message": e.message})
                    sc_kw = sc_int
            log_phase("sources_update", f"id={sid}")
            out = update_source(
                source_id=sid,
                viewer_client_id=cid,
                is_super_admin=is_super_admin,
                name=name,
                risk_level=rl,
                visibility=vis,
                metadata=meta,
                source_category_id=sc_kw,
            )
            if out == "forbidden":
                return _forbidden("Sin permiso para actualizar esta fuente.")
            if out is None:
                return _not_found("Fuente no encontrada.")
            return _response(200, out)

        if path.endswith("/sources/get"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            try:
                sid = int(body["sourceId"])
            except (KeyError, TypeError, ValueError):
                return _bad("sourceId requerido (entero).")
            inc_cat = _parse_include_category(body)
            log_phase("sources_get", f"id={sid}")
            row = get_source(
                source_id=sid,
                viewer_client_id=cid,
                is_super_admin=is_super_admin,
                include_category=inc_cat,
            )
            if not row:
                return _not_found("Fuente no encontrada o no visible para este tenant.")
            return _response(200, row)

        if path.endswith("/sources/list"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, _uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            lim = int(body.get("limit") or 50)
            off = int(body.get("offset") or 0)
            inc_cat = _parse_include_category(body)
            want_inactive_meta = _parse_include_inactive_categories(event, body)
            if want_inactive_meta and not is_super_admin:
                return _forbidden(
                    "includeInactiveCategories solo está permitido para super_admin."
                )
            log_phase("sources_list", f"clientId={cid}")
            inc_doc_sources = body.get("includeDocumentSources") is True
            items, total = list_sources(
                viewer_client_id=cid,
                limit=lim,
                offset=off,
                include_category=inc_cat,
                include_document_sources=inc_doc_sources,
            )
            try:
                cat_rows = list_categories_catalog(active_only=not want_inactive_meta)
            except Exception as cat_err:
                log_phase("sources_list", f"categories_catalog_failed: {cat_err!s}")
                cat_rows = []
            out: dict[str, Any] = {
                "success": True,
                "clientId": cid,
                "items": items,
                "meta": {"categories": cat_rows},
            }
            if total is not None:
                out["total"] = total
                if off + lim < total:
                    out["nextOffset"] = off + lim
            return _response(200, out)

        if path.endswith("/sources"):
            err = _require_actor(claims, body)
            if err:
                return err
            ids = _parse_actor_ids(body)
            if not ids:
                return _bad("clientId y userId requeridos (enteros).")
            cid, uid = ids
            if not jwt_allows_client(claims, cid):
                return _forbidden("clientId no permitido para este token.")
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                return _bad("name requerido (string no vacío).")
            rl = parse_source_risk_level(body.get("riskLevel"))
            if rl is None:
                return _bad(f"riskLevel requerido: {RISK_LEVEL_API_MESSAGE}")
            vis = _validate_visibility(body.get("visibility"))
            if vis is None:
                return _bad("visibility requerido: public o private.")
            if "sourceCategoryId" not in body:
                return _bad(
                    "sourceCategoryId es obligatorio al crear una fuente; "
                    "use POST /v1/source-category-id/list para elegir un id activo."
                )
            try:
                scid = int(body["sourceCategoryId"])
            except (TypeError, ValueError):
                return _bad("sourceCategoryId debe ser un entero.")
            try:
                validate_assignable_category(scid)
            except SourceCategoryError as e:
                return _response(400, {"code": e.code, "message": e.message})
            meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            vis = resolve_create_visibility(cid, uid, vis)
            log_phase("sources_create", f"name={name!r} clientId={cid}")
            row = create_source(
                name=name.strip(),
                risk_level=rl,
                visibility=vis,
                client_id=cid,
                created_by_user_id=uid,
                metadata=meta,
                source_category_id=scid,
            )
            return json_response(201, row)

    except SourceCategoryError as e:
        return _response(400, {"code": e.code, "message": e.message})
    except SupabaseRestError as e:
        return _response(
            e.status if 400 <= e.status < 600 else 500,
            {"code": "DATA_BACKEND_ERROR", "message": str(e.body)[:2000]},
        )
    except Exception as e:
        return _response(500, {"code": "INTERNAL", "message": str(e)})

    return _response(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
