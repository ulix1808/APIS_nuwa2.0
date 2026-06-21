"""Entidades PF/PM y monitoreo — PostgreSQL directo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg.errors
from psycopg.types.json import Json

from entity_helpers import (
    build_display_name,
    classify_id_number,
    find_matches,
    has_strong_match,
    merge_risk,
    normalize_identifier,
    normalize_name,
    parse_full_name_fields,
    party_type_label,
    risk_from_report,
    validate_relationship_role,
)
from nuwa_errors import SupabaseRestError
from nuwa_pg_dispatch import _conn


def _iso_z(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(dt)


def _metadata_as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _contact_fields(meta: dict[str, Any]) -> dict[str, str | None]:
    def pick(*keys: str) -> str | None:
        for key in keys:
            raw = meta.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                return text
        return None

    return {
        "address": pick("address", "direccion", "domicilio"),
        "phone": pick("phone", "telefono", "tel"),
        "email": pick("email", "correo"),
        "website": pick("website", "sitioWeb", "web", "url"),
    }


def _merge_entity_metadata(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    meta = _metadata_as_dict(row.get("metadata"))
    if isinstance(body.get("metadata"), dict):
        meta.update(body["metadata"])
    for key in ("address", "phone", "email", "website"):
        if key not in body:
            continue
        val = body[key]
        if val is None or (isinstance(val, str) and not val.strip()):
            meta.pop(key, None)
        else:
            meta[key] = str(val).strip()
    return meta


def _entity_api(row: dict[str, Any], *, report_count: int | None = None) -> dict[str, Any]:
    meta = _metadata_as_dict(row.get("metadata"))
    contact = _contact_fields(meta)
    return {
        "entityId": str(row["id"]),
        "name": row.get("name"),
        "partyType": row.get("party_type"),
        "partyTypeLabel": row.get("party_type_label"),
        "legalName": row.get("legal_name"),
        "firstName": row.get("first_name"),
        "lastName": row.get("last_name"),
        "fullName": row.get("full_name"),
        "category": row.get("category"),
        "rfc": row.get("rfc"),
        "curp": row.get("curp"),
        "country": row.get("country"),
        "riskLevel": row.get("risk_level"),
        "status": row.get("status"),
        "relationshipRole": row.get("relationship_role"),
        "parentEntityId": str(row["parent_entity_id"]) if row.get("parent_entity_id") else None,
        "reportCount": report_count if report_count is not None else int(row.get("report_count") or 0),
        "lastScreeningAt": _iso_z(row.get("last_screening_at")),
        "createdAt": _iso_z(row.get("created_at")),
        "updatedAt": _iso_z(row.get("updated_at")),
        "metadata": meta,
        **contact,
    }


def _fetch_candidates(client_id: int, party_type: str | None) -> list[dict[str, Any]]:
    where = [
        "e.client_id = %s",
        "e.status <> 'deleted'",
        """(
            e.last_screening_at >= now() - interval '30 days'
            OR e.created_at >= now() - interval '30 days'
            OR EXISTS (
                SELECT 1 FROM public.reports r
                WHERE r.entity_id = e.id AND r.status = 'active'
            )
        )""",
    ]
    params: list[Any] = [client_id]
    if party_type in ("individual", "organization"):
        where.append("e.party_type = %s")
        params.append(party_type)
    sql = f"""
    SELECT e.*,
           (SELECT COUNT(*)::int FROM public.reports r
            WHERE r.entity_id = e.id AND r.status = 'active') AS report_count
    FROM public.entities e
    WHERE {' AND '.join(where)}
    ORDER BY e.updated_at DESC
    LIMIT 200
    """
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _duplicate_identifier(client_id: int, rfc: str | None, curp: str | None, exclude_id: str | None = None) -> str | None:
    if rfc:
        nr = normalize_identifier(rfc)
        sql = """
        SELECT id FROM public.entities
        WHERE client_id = %s AND status <> 'deleted' AND upper(rfc) = %s
        """
        params: list[Any] = [client_id, nr]
        if exclude_id:
            sql += " AND id <> %s::uuid"
            params.append(exclude_id)
        with _conn() as conn:
            row = conn.execute(sql, params).fetchone()
        if row:
            return "DUPLICATE_RFC"
    if curp:
        nc = normalize_identifier(curp)
        sql = """
        SELECT id FROM public.entities
        WHERE client_id = %s AND status <> 'deleted' AND upper(curp) = %s
        """
        params = [client_id, nc]
        if exclude_id:
            sql += " AND id <> %s::uuid"
            params.append(exclude_id)
        with _conn() as conn:
            row = conn.execute(sql, params).fetchone()
        if row:
            return "DUPLICATE_CURP"
    return None


def entities_match_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    party_type = body.get("partyType")
    if party_type not in ("individual", "organization"):
        raise SupabaseRestError(400, "partyType debe ser individual u organization")
    min_conf = int(body.get("minConfidence") or 60)
    rfc = body.get("rfc")
    curp = body.get("curp")
    if not rfc and not curp and body.get("identifier"):
        rfc, curp = classify_id_number(str(body["identifier"]))

    candidates = _fetch_candidates(client_id, party_type)
    matches = find_matches(
        party_type=party_type,
        legal_name=body.get("legalName"),
        first_name=body.get("firstName"),
        last_name=body.get("lastName"),
        full_name=body.get("fullName"),
        rfc=rfc,
        curp=curp,
        candidates=candidates,
        min_confidence=min_conf,
    )
    return {"matches": matches, "hasStrongMatch": has_strong_match(matches)}


def _resolve_entity_fields(body: dict[str, Any]) -> dict[str, Any]:
    party_type = body.get("partyType")
    if party_type not in ("individual", "organization"):
        raise SupabaseRestError(400, "partyType debe ser individual u organization")
    label = party_type_label(party_type, body.get("partyTypeLabel"))

    first_name = (body.get("firstName") or "").strip() or None
    last_name = (body.get("lastName") or "").strip() or None
    legal_name = (body.get("legalName") or "").strip() or None
    full_name = (body.get("fullName") or "").strip() or None
    rfc = body.get("rfc")
    curp = body.get("curp")
    if not rfc and not curp and body.get("identifier"):
        rfc, curp = classify_id_number(str(body["identifier"]))

    if full_name and not (first_name or last_name or legal_name):
        parsed = parse_full_name_fields(full_name, party_type)
        first_name = parsed.get("first_name") or first_name
        last_name = parsed.get("last_name") or last_name
        legal_name = parsed.get("legal_name") or legal_name
        full_name = parsed.get("full_name") or full_name

    name = build_display_name(
        party_type=party_type,
        first_name=first_name,
        last_name=last_name,
        legal_name=legal_name,
        full_name=full_name,
    )
    if not name:
        raise SupabaseRestError(400, "Se requiere nombre o identificador para crear la entidad")

    return {
        "party_type": party_type,
        "party_type_label": label,
        "name": name,
        "name_normalized": normalize_name(name),
        "first_name": first_name,
        "last_name": last_name,
        "legal_name": legal_name,
        "full_name": full_name or name,
        "rfc": normalize_identifier(rfc) or None,
        "curp": normalize_identifier(curp) or None,
        "country": (body.get("country") or None),
        "category": body.get("category") or "screening",
        "relationship_role": body.get("relationshipRole"),
        "parent_entity_id": body.get("parentEntityId"),
        "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    }


def entities_create_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    fields = _resolve_entity_fields(body)
    err = validate_relationship_role(fields["party_type"], fields["relationship_role"])
    if err:
        raise SupabaseRestError(400, err)

    dup = _duplicate_identifier(client_id, fields["rfc"], fields["curp"])
    if dup:
        raise SupabaseRestError(409, dup)

    parent = fields.get("parent_entity_id")
    sql = """
    INSERT INTO public.entities (
      client_id, created_by_user_id, name, name_normalized, party_type, party_type_label,
      first_name, last_name, legal_name, full_name, category, rfc, curp, country,
      relationship_role, parent_entity_id, metadata, status
    ) VALUES (
      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active'
    )
    RETURNING *
    """
    vals = [
        client_id,
        user_id,
        fields["name"],
        fields["name_normalized"],
        fields["party_type"],
        fields["party_type_label"],
        fields["first_name"],
        fields["last_name"],
        fields["legal_name"],
        fields["full_name"],
        fields["category"],
        fields["rfc"],
        fields["curp"],
        fields["country"],
        fields["relationship_role"],
        parent,
        Json(fields["metadata"]),
    ]
    try:
        with _conn() as conn:
            row = conn.execute(sql, vals).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise SupabaseRestError(409, str(e)) from e
    except psycopg.errors.ForeignKeyViolation as e:
        raise SupabaseRestError(400, str(e)) from e
    if not row:
        raise SupabaseRestError(500, "No se pudo crear la entidad")
    api = _entity_api(dict(row), report_count=0)
    return {
        "entityId": api["entityId"],
        "name": api["name"],
        "partyType": api["partyType"],
        "partyTypeLabel": api["partyTypeLabel"],
        "category": api["category"],
        "status": api["status"],
        "createdAt": api["createdAt"],
    }


def _entity_visible_in_ui_sql(alias: str = "e") -> str:
    """Menciones documentales secundarias ocultas; primarias de documento y con reportes visibles."""
    return f"""(
      {alias}.category <> 'document_mention'
      OR EXISTS (
        SELECT 1 FROM public.documents d
        WHERE d.client_id = {alias}.client_id
          AND d.primary_entity_id = {alias}.id
      )
      OR EXISTS (
        SELECT 1 FROM public.reports r
        WHERE r.entity_id = {alias}.id AND r.status = 'active'
      )
    )"""


def promote_entity_on_document_primary(
    entity_id: str,
    client_id: int,
    user_id: int,
) -> None:
    """Entidad principal de un documento pasa a categoría screening (visible en /entities)."""
    sql = """
    UPDATE public.entities SET
      category = 'screening',
      updated_by_user_id = %s,
      updated_at = now()
    WHERE id = %s::uuid AND client_id = %s
      AND status <> 'deleted'
      AND category = 'document_mention'
    """
    with _conn() as conn:
        conn.execute(sql, [user_id, entity_id, client_id])
        conn.commit()


def entities_list_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    lim = max(1, min(int(body.get("limit") or 50), 200))
    off = max(0, int(body.get("offset") or 0))
    where = ["e.client_id = %s", "e.status <> 'deleted'"]
    params: list[Any] = [client_id]

    if body.get("partyType") in ("individual", "organization"):
        where.append("e.party_type = %s")
        params.append(body["partyType"])
    if body.get("category"):
        where.append("e.category = %s")
        params.append(body["category"])
    if body.get("status"):
        where.append("e.status = %s")
        params.append(body["status"])
    if body.get("riskLevel"):
        where.append("e.risk_level = %s")
        params.append(body["riskLevel"])
    if body.get("parentEntityId"):
        where.append("e.parent_entity_id = %s::uuid")
        params.append(body["parentEntityId"])
    if not body.get("includeDocumentMentions"):
        where.append(_entity_visible_in_ui_sql("e"))
    search = (body.get("search") or "").strip()
    if search:
        where.append(
            """(
            e.name ILIKE %s OR e.rfc ILIKE %s OR e.curp ILIKE %s
            OR e.legal_name ILIKE %s OR e.full_name ILIKE %s
            OR e.id::text ILIKE %s
            )"""
        )
        pat = f"%{search}%"
        params.extend([pat] * 6)

    wsql = " AND ".join(where)
    count_sql = f"SELECT COUNT(*)::int AS c FROM public.entities e WHERE {wsql}"
    list_sql = f"""
    SELECT e.*,
           (SELECT COUNT(*)::int FROM public.reports r
            WHERE r.entity_id = e.id AND r.status = 'active') AS report_count
    FROM public.entities e
    WHERE {wsql}
    ORDER BY e.updated_at DESC
    LIMIT %s OFFSET %s
    """
    with _conn() as conn:
        total = int(conn.execute(count_sql, params).fetchone()["c"])
        rows = conn.execute(list_sql, [*params, lim, off]).fetchall()
    items = [_entity_api(dict(r)) for r in rows]
    return {"entities": items, "total": total, "limit": lim, "offset": off}


def _get_entity_row(client_id: int, entity_id: str) -> dict[str, Any] | None:
    sql = """
    SELECT e.*,
           (SELECT COUNT(*)::int FROM public.reports r
            WHERE r.entity_id = e.id AND r.status = 'active') AS report_count
    FROM public.entities e
    WHERE e.id = %s::uuid AND e.client_id = %s AND e.status <> 'deleted'
    """
    with _conn() as conn:
        row = conn.execute(sql, [entity_id, client_id]).fetchone()
    return dict(row) if row else None


def entities_get_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    entity_id = str(body["entityId"])
    row = _get_entity_row(client_id, entity_id)
    if not row:
        raise SupabaseRestError(404, "Entidad no encontrada")
    entity = _entity_api(row)
    out: dict[str, Any] = {"entity": entity}
    if body.get("includeReports", True):
        rsql = """
        SELECT folio, fecha, nivel_riesgo, created_at
        FROM public.reports
        WHERE client_id = %s AND entity_id = %s::uuid AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 100
        """
        with _conn() as conn:
            reps = conn.execute(rsql, [client_id, entity_id]).fetchall()
        out["reports"] = [
            {
                "folio": r["folio"],
                "fecha": str(r["fecha"]) if r.get("fecha") else None,
                "nivelRiesgo": r.get("nivel_riesgo"),
                "createdAt": _iso_z(r.get("created_at")),
            }
            for r in reps
        ]
    mon = _monitoring_for_entity(entity_id)
    if mon:
        entity["monitoring"] = mon
    out["entity"] = entity
    return out


def _monitoring_for_entity(entity_id: str) -> dict[str, Any] | None:
    sql = """
    SELECT is_enabled, frequency, sources, next_run_at
    FROM public.entity_monitoring WHERE entity_id = %s::uuid
    """
    with _conn() as conn:
        row = conn.execute(sql, [entity_id]).fetchone()
    if not row:
        return None
    return {
        "enabled": bool(row["is_enabled"]),
        "frequency": row["frequency"],
        "sources": list(row["sources"] or []),
        "nextRunAt": _iso_z(row.get("next_run_at")),
    }


def entities_update_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    entity_id = str(body["entityId"])
    row = _get_entity_row(client_id, entity_id)
    if not row:
        raise SupabaseRestError(404, "Entidad no encontrada")

    party_type = body.get("partyType") or row["party_type"]
    merged = {
        "partyType": party_type,
        "partyTypeLabel": body.get("partyTypeLabel") or row["party_type_label"],
        "firstName": body.get("firstName", row.get("first_name")),
        "lastName": body.get("lastName", row.get("last_name")),
        "legalName": body.get("legalName", row.get("legal_name")),
        "fullName": body.get("fullName", row.get("full_name")),
        "rfc": body.get("rfc", row.get("rfc")),
        "curp": body.get("curp", row.get("curp")),
        "country": body.get("country", row.get("country")),
        "category": body.get("category", row.get("category")),
        "relationshipRole": body.get("relationshipRole", row.get("relationship_role")),
    }
    fields = _resolve_entity_fields(merged)
    err = validate_relationship_role(fields["party_type"], fields["relationship_role"])
    if err:
        raise SupabaseRestError(400, err)

    dup = _duplicate_identifier(client_id, fields["rfc"], fields["curp"], exclude_id=entity_id)
    if dup:
        raise SupabaseRestError(409, dup)

    status = body.get("status") or row["status"]
    risk = body.get("riskLevel") or row.get("risk_level")
    metadata = _merge_entity_metadata(row, body)

    sql = """
    UPDATE public.entities SET
      updated_by_user_id = %s,
      name = %s, name_normalized = %s,
      party_type = %s, party_type_label = %s,
      first_name = %s, last_name = %s, legal_name = %s, full_name = %s,
      category = %s, rfc = %s, curp = %s, country = %s,
      relationship_role = %s, status = %s, risk_level = %s,
      metadata = %s
    WHERE id = %s::uuid AND client_id = %s
    RETURNING *
    """
    vals = [
        user_id,
        fields["name"],
        fields["name_normalized"],
        fields["party_type"],
        fields["party_type_label"],
        fields["first_name"],
        fields["last_name"],
        fields["legal_name"],
        fields["full_name"],
        fields["category"],
        fields["rfc"],
        fields["curp"],
        fields["country"],
        fields["relationship_role"],
        status,
        risk,
        Json(metadata),
        entity_id,
        client_id,
    ]
    with _conn() as conn:
        updated = conn.execute(sql, vals).fetchone()
        conn.commit()
    return _entity_api(dict(updated))


def entities_delete_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    entity_id = str(body["entityId"])
    sql = """
    UPDATE public.entities SET
      status = 'deleted', deleted_at = now(), deleted_by_user_id = %s, updated_by_user_id = %s
    WHERE id = %s::uuid AND client_id = %s AND status <> 'deleted'
    RETURNING id
    """
    with _conn() as conn:
        row = conn.execute(sql, [user_id, user_id, entity_id, client_id]).fetchone()
        conn.commit()
    if not row:
        raise SupabaseRestError(404, "Entidad no encontrada")
    return {"entityId": entity_id, "status": "deleted"}


def entities_stats_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    visibility = _entity_visible_in_ui_sql("e")
    sql = f"""
    SELECT
      COUNT(*)::int AS total,
      COUNT(*) FILTER (WHERE e.status IN ('under_review','flagged'))::int AS in_review,
      COUNT(*) FILTER (WHERE e.party_type = 'individual')::int AS individuals,
      COUNT(*) FILTER (
        WHERE e.party_type = 'individual' AND e.risk_level IN ('high','critical')
      )::int AS individuals_high,
      COUNT(*) FILTER (WHERE e.party_type = 'organization')::int AS organizations,
      COUNT(*) FILTER (
        WHERE e.party_type = 'organization' AND e.risk_level IN ('high','critical')
      )::int AS organizations_high,
      COUNT(*) FILTER (WHERE e.risk_level IN ('high','critical'))::int AS high_total
    FROM public.entities e
    WHERE e.client_id = %s AND e.status <> 'deleted'
      AND {visibility}
    """
    with _conn() as conn:
        r = conn.execute(sql, [client_id]).fetchone()
    total = int(r["total"])
    high = int(r["high_total"])
    pct = round((high / total) * 100, 2) if total else 0.0
    return {
        "totalEntities": total,
        "inReviewCount": int(r["in_review"]),
        "individualsCount": int(r["individuals"]),
        "individualsHighRisk": int(r["individuals_high"]),
        "organizationsCount": int(r["organizations"]),
        "organizationsHighRisk": int(r["organizations_high"]),
        "highRiskTotal": high,
        "highRiskPercent": pct,
    }


def _next_run_at(frequency: str) -> datetime:
    now = datetime.now(timezone.utc)
    deltas = {
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
        "semi-annual": timedelta(days=182),
        "annual": timedelta(days=365),
    }
    return now + deltas.get(frequency, timedelta(days=7))


def entities_monitoring_upsert_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    entity_id = str(body["entityId"])
    enabled = bool(body.get("enabled"))
    row = _get_entity_row(client_id, entity_id)
    if not row:
        raise SupabaseRestError(400, "entityId no existe; cree la entidad antes de activar monitoreo")

    if not enabled:
        sql = """
        UPDATE public.entity_monitoring SET is_enabled = false, updated_at = now()
        WHERE entity_id = %s::uuid
        RETURNING id, frequency, sources, next_run_at
        """
        with _conn() as conn:
            m = conn.execute(sql, [entity_id]).fetchone()
            conn.commit()
        if not m:
            return {"entityId": entity_id, "enabled": False}
        return {
            "monitoringId": str(m["id"]),
            "entityId": entity_id,
            "enabled": False,
            "frequency": m["frequency"],
            "sources": list(m["sources"] or []),
            "nextRunAt": _iso_z(m.get("next_run_at")),
        }

    frequency = body.get("frequency") or "weekly"
    if frequency not in ("weekly", "monthly", "semi-annual", "annual"):
        raise SupabaseRestError(400, "frequency inválida")
    sources = body.get("sources") or ["compliance", "media"]
    if not isinstance(sources, list):
        raise SupabaseRestError(400, "sources debe ser array")
    sources = [str(s) for s in sources]
    nxt = _next_run_at(frequency)

    sql = """
    INSERT INTO public.entity_monitoring (
      entity_id, client_id, is_enabled, frequency, sources, next_run_at, created_by_user_id
    ) VALUES (%s::uuid, %s, true, %s, %s, %s, %s)
    ON CONFLICT (entity_id) DO UPDATE SET
      is_enabled = true, frequency = EXCLUDED.frequency, sources = EXCLUDED.sources,
      next_run_at = EXCLUDED.next_run_at, updated_at = now()
    RETURNING id, frequency, sources, next_run_at
    """
    with _conn() as conn:
        m = conn.execute(
            sql, [entity_id, client_id, frequency, sources, nxt, user_id]
        ).fetchone()
        conn.commit()
    return {
        "monitoringId": str(m["id"]),
        "entityId": entity_id,
        "enabled": True,
        "frequency": m["frequency"],
        "sources": list(m["sources"] or []),
        "nextRunAt": _iso_z(m.get("next_run_at")),
    }


def entities_monitoring_list_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    active_only = bool(body.get("activeOnly", True))
    lim = max(1, min(int(body.get("limit") or 100), 200))
    off = max(0, int(body.get("offset") or 0))
    where = ["e.client_id = %s", "m.is_enabled = true"] if active_only else ["e.client_id = %s"]
    if not active_only:
        where = ["e.client_id = %s"]
    wsql = " AND ".join(where)
    sql = f"""
    SELECT e.*, m.id AS monitoring_id, m.frequency, m.sources, m.next_run_at,
           (SELECT COUNT(*)::int FROM public.reports r
            WHERE r.entity_id = e.id AND r.status = 'active') AS report_count
    FROM public.entity_monitoring m
    JOIN public.entities e ON e.id = m.entity_id
    WHERE {wsql}
    ORDER BY m.next_run_at NULLS LAST
    LIMIT %s OFFSET %s
    """
    count_sql = f"""
    SELECT COUNT(*)::int AS c FROM public.entity_monitoring m
    JOIN public.entities e ON e.id = m.entity_id
    WHERE {wsql}
    """
    with _conn() as conn:
        total = int(conn.execute(count_sql, [client_id]).fetchone()["c"])
        rows = conn.execute(sql, [client_id, lim, off]).fetchall()
    items = []
    for r in rows:
        ent = _entity_api(dict(r))
        ent["monitoringId"] = str(r["monitoring_id"])
        ent["frequency"] = r["frequency"]
        ent["sources"] = list(r["sources"] or [])
        ent["nextRunAt"] = _iso_z(r.get("next_run_at"))
        items.append(ent)
    return {"items": items, "total": total}


def enrich_entity_from_party_pg(
    *,
    entity_id: str,
    party: dict[str, Any],
    user_id: int,
) -> bool:
    """Completa RFC/CURP/país/contacto vacíos desde una party extraída de documento."""
    sql = """
    SELECT * FROM public.entities
    WHERE id = %s::uuid AND status <> 'deleted'
    """
    with _conn() as conn:
        row = conn.execute(sql, [entity_id]).fetchone()
        if not row:
            return False
        current = dict(row)
        meta = _metadata_as_dict(current.get("metadata"))
        changed = False

        party_rfc = normalize_identifier(party.get("rfc"))
        if party_rfc and not current.get("rfc"):
            current["rfc"] = party_rfc
            changed = True

        party_curp = normalize_identifier(party.get("curp"))
        if party_curp and not current.get("curp"):
            current["curp"] = party_curp
            changed = True

        party_country = party.get("country")
        if party_country and not current.get("country"):
            current["country"] = str(party_country).strip()
            changed = True

        address = party.get("address") or party.get("domicilio") or party.get("direccion")
        if address and not meta.get("address"):
            meta["address"] = str(address).strip()
            changed = True

        phones = party.get("phones") or []
        if isinstance(phones, list) and phones and not meta.get("phone"):
            first_phone = str(phones[0]).strip()
            if first_phone:
                meta["phone"] = first_phone
                changed = True

        emails = party.get("emails") or []
        if isinstance(emails, list) and emails and not meta.get("email"):
            first_email = str(emails[0]).strip()
            if first_email:
                meta["email"] = first_email
                changed = True

        website = party.get("website") or party.get("sitioWeb") or party.get("url")
        if website and not meta.get("website"):
            meta["website"] = str(website).strip()
            changed = True

        if not changed:
            return False

        conn.execute(
            """
            UPDATE public.entities SET
              updated_by_user_id = %s,
              rfc = %s,
              curp = %s,
              country = %s,
              metadata = %s,
              updated_at = now()
            WHERE id = %s::uuid
            """,
            [
                user_id,
                current.get("rfc"),
                current.get("curp"),
                current.get("country"),
                Json(meta),
                entity_id,
            ],
        )
        conn.commit()
    return True


def touch_entity_after_report_pg(
    *,
    entity_id: str,
    client_id: int,
    folio: str,
    nivel_riesgo: str | None,
    nivel_numerico: int | None,
) -> None:
    new_risk = risk_from_report(nivel_riesgo, nivel_numerico)
    sql = """
    UPDATE public.entities SET
      last_screening_at = now(),
      last_report_folio = %s,
      risk_level = COALESCE(%s, risk_level),
      updated_at = now()
    WHERE id = %s::uuid AND client_id = %s AND status <> 'deleted'
    RETURNING risk_level
    """
    with _conn() as conn:
        cur = conn.execute(
            sql,
            [folio, new_risk, entity_id, client_id],
        ).fetchone()
        if cur and new_risk:
            merged = merge_risk(cur.get("risk_level"), new_risk)
            if merged != cur.get("risk_level"):
                conn.execute(
                    "UPDATE public.entities SET risk_level = %s WHERE id = %s::uuid",
                    [merged, entity_id],
                )
        conn.execute(
            """
            UPDATE public.entities SET category = 'screening', updated_at = now()
            WHERE id = %s::uuid AND client_id = %s AND category = 'document_mention' AND status <> 'deleted'
            """,
            [entity_id, client_id],
        )
        conn.commit()
