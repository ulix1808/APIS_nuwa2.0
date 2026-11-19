"""
SIGER partners (nuwa-siger container). Worker only: x-monitoring-worker-secret.

POST /v1/siger/pending — companies to look up. Body { clientIds: [int], since?, until?, limit? }.
  Organizations with an active report created in [since, until) and no lookup yet (or a failed one
  with attempts left). since/until are ISO dates or instants; since never goes before SIGER_START_AT
  (reports before the container went live are never looked up), so the container can keep its own
  cursor (last reportCreatedAt seen) and ask only for what is newer.
  Returns the latest report of each: folio + createdByUserId (actor for the BFF save-report update).
POST /v1/siger/claim — before paying. Body { clientId, entityId, folio?, rfc?, searchName?,
  reuseFromId?, force? }. Reuses a lookup paid for any client (≤ SIGER_REUSE_DAYS, default 180) only
  when it is surely the same company: same RFC, or the same full legal name WITH its legal form.
    {claimed: false, reused: true, lookup}  → copied for this client at cost 0: write lookup.partners
                                               to the report, do not run SIGER.
    {claimed: false, candidates: [...]}     → earlier lookups whose name matches without legal form
                                               ("Gráficas Corona" → "Gráficas Corona J.E., S.A. de C.V."):
                                               the container checks them against the report and calls
                                               claim again with reuseFromId (same company) or force (pay).
    {claimed: true}                          → run SIGER.
    {claimed: false, lookup}                 → already answered or running: skip.
POST /v1/siger/record — result of a claimed lookup. Body { clientId, entityId, status, rfc?, legalName?,
  searchName?, aliases?, matchReason?, fems?, candidates?, partners?, cost?, error?, folio? };
  status ok | no_match | multiple | error. Only over a 'running' claim. legalName/rfc are the company
  chosen in SIGER (with several results the container picks the one the report talks about).
POST /v1/siger/get — the lookup of one company. Body { clientId, entityId }.

One row per (client, entity) in siger_lookups: each lookup pays fees, so a company is never looked up
twice once it got an answer (ok, no_match or multiple), and a 'running' claim is never retried here.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json

from nuwa_config import DatabaseConfigError, SupabaseConfigError, ensure_data_backend, is_database_mode
from nuwa_http import CORS_HEADERS
from nuwa_monitoring_worker import monitoring_worker_secret_ok
from nuwa_obs_log import log_handler_enter, log_phase
from nuwa_pg_dispatch import _conn

MAX_ATTEMPTS = 3
FINAL_STATUSES = ("ok", "no_match", "multiple", "error")
INACTIVE_ENTITY_STATUSES = ("deleted", "inactive")
# Legal forms dropped from the base key: "Gráficas Corona, S.A. de C.V." and "Gráficas Corona" share it.
LEGAL_FORMS = re.compile(
    r"\b(s\s*a\s*p\s*i|s\s*a\s*b|s\s*a|s\s*de\s*r\s*l|s\s*r\s*l|s\s*c|s\s*p\s*r|a\s*c|de\s*c\s*v|de\s*r\s*l|c\s*v)\b"
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
        out = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {}


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _text(value: Any, limit: int = 500) -> str | None:
    s = str(value or "").strip()
    return s[:limit] if s else None


def _list(value: Any, limit: int = 50) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def start_at() -> str | None:
    """Reports created before this instant are never looked up (container go-live)."""
    return _text(os.environ.get("SIGER_START_AT"), 40)


def _instant(value: Any) -> datetime | None:
    s = _text(value, 40)
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ascii(name: Any) -> str:
    s = unicodedata.normalize("NFD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s)


def legal_key(name: Any) -> str | None:
    """Full legal name, legal form kept: "Gráficas Corona J.E., S.A. de C.V." == "GRAFICAS CORONA JE SA DE CV"."""
    s = re.sub(r"\s+", "", _ascii(name))
    return s or None


def base_key(name: Any) -> str | None:
    """Name without legal form: several companies can share it, so it only finds candidates."""
    s = re.sub(r"\s+", "", LEGAL_FORMS.sub(" ", _ascii(name)))
    return s or None


def has_legal_form(name: Any) -> bool:
    return legal_key(name) != base_key(name)


def reuse_days() -> int:
    try:
        return max(0, int(os.environ.get("SIGER_REUSE_DAYS") or 180))
    except ValueError:
        return 180


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "clientId": r["client_id"],
        "entityId": str(r["entity_id"]),
        "reportFolio": r.get("report_folio"),
        "searchName": r.get("search_name"),
        "rfc": r.get("rfc"),
        "legalName": r.get("legal_name"),
        "aliases": r.get("aliases") or [],
        "matchReason": r.get("match_reason"),
        "status": r["status"],
        "attempts": r["attempts"],
        "fems": r.get("fems") or [],
        "candidates": r.get("candidates") or [],
        "partners": r.get("partners") or [],
        "cost": float(r["cost"]) if r.get("cost") is not None else None,
        "error": r.get("error"),
        "reusedFrom": r.get("reused_from"),
        "createdAt": r.get("created_at"),
        "updatedAt": r.get("updated_at"),
    }


def _candidate(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "legalName": r.get("legal_name"),
        "rfc": r.get("rfc"),
        "aliases": r.get("aliases") or [],
        "fems": r.get("fems") or [],
        "partners": len(r.get("partners") or []),
        "updatedAt": r.get("updated_at"),
    }


def handle_pending(body: dict[str, Any]) -> dict[str, Any]:
    floor = _instant(start_at())
    if floor is None:
        return _resp(503, {"code": "SIGER_START_AT_MISSING", "message": "Falta SIGER_START_AT en la Lambda."})
    for key in ("since", "until"):
        if body.get(key) and _instant(body.get(key)) is None:
            return _resp(400, {"code": "BAD_REQUEST", "message": f"{key} debe ser fecha ISO (2026-11-18 o 2026-11-18T12:00:00Z)."})
    asked = _instant(body.get("since"))
    since = max(floor, asked) if asked else floor
    until = _instant(body.get("until"))
    client_ids = sorted({n for n in (_int(c) for c in _list(body.get("clientIds"), 500)) if n})
    if not client_ids:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientIds requerido (lista de clientes habilitados)."})
    limit = max(1, min(_int(body.get("limit")) or 50, 200))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
              SELECT DISTINCT ON (e.id)
                     e.id AS entity_id, e.client_id, e.name, e.legal_name, e.rfc,
                     r.folio, r.created_at AS report_created_at, r.created_by_user_id,
                     s.status AS lookup_status, s.attempts AS lookup_attempts
                FROM public.reports r
                JOIN public.entities e
                  ON e.client_id = r.client_id AND (e.id = r.entity_id OR e.last_report_folio = r.folio)
                LEFT JOIN public.siger_lookups s ON s.client_id = e.client_id AND s.entity_id = e.id
               WHERE r.status = 'active'
                 AND r.created_at >= %s
                 AND (%s::timestamptz IS NULL OR r.created_at < %s::timestamptz)
                 AND r.client_id = ANY(%s::int[])
                 AND e.party_type = 'organization'
                 AND e.status <> ALL(%s::text[])
                 AND (s.id IS NULL OR (s.status = 'error' AND s.attempts < %s))
               ORDER BY e.id, r.created_at DESC
            ) x
            ORDER BY x.report_created_at
            LIMIT %s
            """,
            (since, until, until, client_ids, list(INACTIVE_ENTITY_STATUSES), MAX_ATTEMPTS, limit),
        ).fetchall()
    return _resp(
        200,
        {
            "success": True,
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "companies": [
                {
                    "clientId": r["client_id"],
                    "entityId": str(r["entity_id"]),
                    "name": r["name"],
                    "legalName": r.get("legal_name"),
                    "rfc": r.get("rfc"),
                    "folio": r["folio"],
                    "reportCreatedAt": r["report_created_at"],
                    "createdByUserId": r["created_by_user_id"],
                    "previousStatus": r.get("lookup_status"),
                    "attempts": r.get("lookup_attempts") or 0,
                }
                for r in rows
            ],
        },
    )


def _ids(body: dict[str, Any]) -> tuple[int, str] | dict[str, Any]:
    client_id = _int(body.get("clientId"))
    entity_id = _text(body.get("entityId"), 64)
    if client_id is None or not entity_id:
        return _resp(400, {"code": "BAD_REQUEST", "message": "clientId y entityId requeridos."})
    return client_id, entity_id


def _aliases(*names: Any) -> list[str]:
    out: list[str] = []
    for n in names:
        for v in n if isinstance(n, list) else [n]:
            t = _text(v, 300)
            if t and t.lower() not in (a.lower() for a in out):
                out.append(t)
    return out[:20]


def _copy(conn: Any, client_id: int, entity_id: str, source: dict[str, Any], folio: str | None, search_name: str | None):
    """Copy of another client's lookup for this one (cost 0); only over no row or a failed one."""
    return conn.execute(
        """
        INSERT INTO public.siger_lookups
          (client_id, entity_id, report_folio, search_name, rfc, legal_name, legal_key, base_key, aliases,
           match_reason, status, fems, candidates, partners, cost, reused_from)
        VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, 'reused', %s, %s, %s, %s, 0, %s)
        ON CONFLICT (client_id, entity_id) DO UPDATE
           SET report_folio = COALESCE(EXCLUDED.report_folio, siger_lookups.report_folio),
               search_name = COALESCE(EXCLUDED.search_name, siger_lookups.search_name),
               rfc = EXCLUDED.rfc, legal_name = EXCLUDED.legal_name, legal_key = EXCLUDED.legal_key,
               base_key = EXCLUDED.base_key, aliases = EXCLUDED.aliases, match_reason = 'reused',
               status = EXCLUDED.status, fems = EXCLUDED.fems, candidates = EXCLUDED.candidates,
               partners = EXCLUDED.partners, cost = 0, reused_from = EXCLUDED.reused_from, error = NULL,
               updated_at = now()
         WHERE siger_lookups.status = 'error'
        RETURNING *
        """,
        (
            client_id, entity_id, folio, search_name, source.get("rfc"), source.get("legal_name"),
            source.get("legal_key"), source.get("base_key"), Json(_aliases(source.get("aliases") or [], search_name)),
            source["status"], Json(source.get("fems") or []), Json(source.get("candidates") or []),
            Json(source.get("partners") or []), source["id"],
        ),
    ).fetchone()


def _remember_alias(conn: Any, source: dict[str, Any], search_name: str | None) -> None:
    """The name this client searched also resolves to the source company next time."""
    if search_name and search_name.lower() not in (str(a).lower() for a in (source.get("aliases") or [])):
        conn.execute(
            "UPDATE public.siger_lookups SET aliases = %s WHERE id = %s",
            (Json(_aliases(source.get("aliases") or [], search_name)), source["id"]),
        )


def handle_claim(body: dict[str, Any]) -> dict[str, Any]:
    ids = _ids(body)
    if isinstance(ids, dict):
        return ids
    client_id, entity_id = ids
    folio = _text(body.get("folio"), 120)
    with _conn() as conn:
        entity = conn.execute(
            "SELECT party_type, status, name, legal_name, rfc FROM public.entities WHERE id = %s::uuid AND client_id = %s",
            (entity_id, client_id),
        ).fetchone()
        if not entity:
            return _resp(404, {"code": "NOT_FOUND", "message": "La entidad no existe para este cliente."})
        if entity["party_type"] != "organization":
            return _resp(400, {"code": "NOT_ORGANIZATION", "message": "SIGER solo aplica a personas morales."})
        rfc = (_text(body.get("rfc"), 20) or _text(entity.get("rfc"), 20) or "").upper() or None
        search_name = (
            _text(body.get("searchName") or body.get("queryName"), 300)
            or _text(entity.get("legal_name"), 300)
            or _text(entity.get("name"), 300)
        )
        mine = conn.execute(
            "SELECT * FROM public.siger_lookups WHERE client_id = %s AND entity_id = %s::uuid",
            (client_id, entity_id),
        ).fetchone()
        open_slot = mine is None or (mine["status"] == "error" and mine["attempts"] < MAX_ATTEMPTS)
        if not open_slot:
            return _resp(200, {"success": True, "claimed": False, "lookup": _row(mine)})

        days = reuse_days()
        reuse_id = _int(body.get("reuseFromId"))
        if reuse_id:
            source = conn.execute(
                """
                SELECT * FROM public.siger_lookups
                 WHERE id = %s AND reused_from IS NULL AND status IN ('ok', 'no_match')
                   AND updated_at > now() - make_interval(days => %s)
                """,
                (reuse_id, days),
            ).fetchone()
            if not source:
                return _resp(404, {"code": "REUSE_NOT_FOUND", "message": "reuseFromId no existe, no es reutilizable o venció."})
        else:
            # Sure matches only: same RFC, or the same full legal name with its legal form.
            lkey = legal_key(search_name) if has_legal_form(search_name) else None
            source = conn.execute(
                """
                SELECT * FROM public.siger_lookups
                 WHERE reused_from IS NULL
                   AND NOT (client_id = %s AND entity_id = %s::uuid)
                   AND updated_at > now() - make_interval(days => %s)
                   AND ((%s::text IS NOT NULL AND upper(rfc) = %s AND status IN ('ok', 'no_match'))
                        OR (%s::text IS NOT NULL AND legal_key = %s AND status = 'ok'))
                 ORDER BY (%s::text IS NOT NULL AND upper(rfc) = %s) DESC, updated_at DESC
                 LIMIT 1
                """,
                (client_id, entity_id, days, rfc, rfc, lkey, lkey, rfc, rfc),
            ).fetchone()
        if source:
            row = _copy(conn, client_id, entity_id, source, folio, search_name)
            if row:
                _remember_alias(conn, source, search_name)
                conn.commit()
                return _resp(200, {"success": True, "claimed": False, "reused": True, "lookup": _row(row)})

        if not reuse_id and not body.get("force"):
            # Same name without legal form (or a known alias): maybe the same company, maybe not.
            bkey = base_key(search_name)
            candidates = conn.execute(
                """
                SELECT * FROM public.siger_lookups
                 WHERE reused_from IS NULL AND status = 'ok'
                   AND NOT (client_id = %s AND entity_id = %s::uuid)
                   AND updated_at > now() - make_interval(days => %s)
                   AND (base_key = %s OR aliases @> to_jsonb(ARRAY[%s::text]))
                 ORDER BY updated_at DESC
                 LIMIT 10
                """,
                (client_id, entity_id, days, bkey, search_name),
            ).fetchall() if bkey else []
            if candidates:
                return _resp(
                    200,
                    {"success": True, "claimed": False, "candidates": [_candidate(c) for c in candidates]},
                )

        row = conn.execute(
            """
            INSERT INTO public.siger_lookups (client_id, entity_id, report_folio, search_name, rfc, status)
            VALUES (%s, %s::uuid, %s, %s, %s, 'running')
            ON CONFLICT (client_id, entity_id) DO UPDATE
               SET status = 'running', attempts = siger_lookups.attempts + 1, error = NULL,
                   report_folio = COALESCE(EXCLUDED.report_folio, siger_lookups.report_folio),
                   search_name = COALESCE(EXCLUDED.search_name, siger_lookups.search_name),
                   rfc = COALESCE(EXCLUDED.rfc, siger_lookups.rfc),
                   updated_at = now()
             WHERE siger_lookups.status = 'error' AND siger_lookups.attempts < %s
            RETURNING *
            """,
            (client_id, entity_id, folio, search_name, rfc, MAX_ATTEMPTS),
        ).fetchone()
        if row:
            conn.commit()
            return _resp(200, {"success": True, "claimed": True, "lookup": _row(row)})
        existing = conn.execute(
            "SELECT * FROM public.siger_lookups WHERE client_id = %s AND entity_id = %s::uuid",
            (client_id, entity_id),
        ).fetchone()
    return _resp(200, {"success": True, "claimed": False, "lookup": _row(existing) if existing else None})


def handle_record(body: dict[str, Any]) -> dict[str, Any]:
    ids = _ids(body)
    if isinstance(ids, dict):
        return ids
    client_id, entity_id = ids
    status = str(body.get("status") or "")
    if status not in FINAL_STATUSES:
        return _resp(400, {"code": "BAD_REQUEST", "message": f"status debe ser uno de {', '.join(FINAL_STATUSES)}."})
    cost = body.get("cost")
    try:
        cost = round(float(cost), 2) if cost is not None else None
    except (TypeError, ValueError):
        return _resp(400, {"code": "BAD_REQUEST", "message": "cost debe ser numérico."})
    legal_name = _text(body.get("legalName"), 300)
    search_name = _text(body.get("searchName"), 300)
    with _conn() as conn:
        row = conn.execute(
            """
            UPDATE public.siger_lookups
               SET status = %s, rfc = COALESCE(%s, rfc), legal_name = COALESCE(%s, legal_name),
                   legal_key = COALESCE(%s, legal_key), base_key = COALESCE(%s, base_key),
                   search_name = COALESCE(%s, search_name),
                   aliases = %s, match_reason = %s, fems = %s, candidates = %s, partners = %s,
                   cost = COALESCE(%s, cost), error = %s,
                   report_folio = COALESCE(%s, report_folio), updated_at = now()
             WHERE client_id = %s AND entity_id = %s::uuid AND status = 'running'
            RETURNING *
            """,
            (
                status,
                (_text(body.get("rfc"), 20) or "").upper() or None,
                legal_name,
                legal_key(legal_name),
                base_key(legal_name),
                search_name,
                Json(_aliases(body.get("aliases") or [], search_name)),
                _text(body.get("matchReason"), 40),
                Json(_list(body.get("fems"))),
                Json(_list(body.get("candidates"), 20)),
                Json(_list(body.get("partners"), 200)),
                cost,
                _text(body.get("error"), 1500) if status == "error" else None,
                _text(body.get("folio"), 120),
                client_id,
                entity_id,
            ),
        ).fetchone()
        if not row:
            return _resp(409, {"code": "NOT_CLAIMED", "message": "No hay una consulta 'running' para esta entidad (llama a /siger/claim)."})
        conn.commit()
    return _resp(200, {"success": True, "lookup": _row(row)})


def handle_get(body: dict[str, Any]) -> dict[str, Any]:
    ids = _ids(body)
    if isinstance(ids, dict):
        return ids
    client_id, entity_id = ids
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM public.siger_lookups WHERE client_id = %s AND entity_id = %s::uuid",
            (client_id, entity_id),
        ).fetchone()
    return _resp(200, {"success": True, "lookup": _row(row) if row else None})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_handler_enter("siger", event, context)
    method = (event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return _resp(200, {"message": "ok"})
    if not monitoring_worker_secret_ok(event):
        return _resp(401, {"code": "UNAUTHORIZED", "message": "Se requiere x-monitoring-worker-secret"})
    try:
        ensure_data_backend()
    except (SupabaseConfigError, DatabaseConfigError) as e:
        return _resp(503, {"code": "BACKEND_NOT_CONFIGURED", "message": str(e)})
    if not is_database_mode():
        return _resp(501, {"code": "NOT_SUPPORTED", "message": "SIGER requiere modo PostgreSQL."})

    path = (event.get("path") or "").rstrip("/")
    log_phase("siger_route", f"{method} {path}")
    body = _parse_json_body(event)
    try:
        if method == "POST" and path.endswith("/siger/pending"):
            return handle_pending(body)
        if method == "POST" and path.endswith("/siger/claim"):
            return handle_claim(body)
        if method == "POST" and path.endswith("/siger/record"):
            return handle_record(body)
        if method == "POST" and path.endswith("/siger/get"):
            return handle_get(body)
        return _resp(404, {"code": "NOT_FOUND", "message": "Ruta no encontrada", "path": path})
    except Exception as e:
        return _resp(500, {"code": "INTERNAL", "message": "Error interno", "error": str(e)})
