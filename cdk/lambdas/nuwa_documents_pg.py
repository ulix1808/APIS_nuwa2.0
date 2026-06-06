"""Documentos del cliente — PostgreSQL."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import psycopg.errors
from psycopg.types.json import Json

from document_helpers import build_index_chunks, document_s3_key, max_upload_bytes, mime_allowed
from entity_helpers import find_matches, has_strong_match, normalize_identifier
from nuwa_chunks import ingest_chunks
from nuwa_errors import SupabaseRestError
from nuwa_pg_dispatch import _conn
from nuwa_s3_documents import ensure_client_storage_prefix, head_object, presigned_get_url, presigned_put_url


def _iso_z(dt: Any) -> str | None:
    if dt is None:
        return None
    from datetime import datetime, timezone

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def _doc_summary(row: dict[str, Any], *, include_extracted: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "documentId": str(row["document_id"]),
        "clientId": int(row["client_id"]),
        "userId": int(row["user_id"]),
        "filename": row["filename"],
        "originalFilename": row["original_filename"],
        "mimeType": row.get("mime_type"),
        "fileSizeBytes": row.get("file_size_bytes"),
        "fileType": row.get("file_type"),
        "category": row.get("category"),
        "description": row.get("description"),
        "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
        "status": row.get("status"),
        "summary": row.get("summary"),
        "documentDate": _iso_z(row.get("document_date")),
        "primaryEntityId": str(row["primary_entity_id"]) if row.get("primary_entity_id") else None,
        "sourceId": int(row["source_id"]) if row.get("source_id") is not None else None,
        "createdAt": _iso_z(row.get("created_at")),
        "updatedAt": _iso_z(row.get("updated_at")),
    }
    if include_extracted:
        out["extractedJson"] = row.get("extracted_json") or {}
        out["extractedText"] = row.get("extracted_text")
    return out


def storage_init_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body.get("userId") or 0)
    init_by = f"user:{user_id}" if user_id else "system"
    s3 = ensure_client_storage_prefix(client_id, initialized_by=init_by)
    sql = """
    INSERT INTO public.client_storage_profiles (client_id, s3_prefix, initialized_by)
    VALUES (%s, %s, %s)
    ON CONFLICT (client_id) DO UPDATE SET s3_prefix = EXCLUDED.s3_prefix
    RETURNING client_id
    """
    with _conn() as conn:
        conn.execute(sql, [client_id, s3["s3Prefix"], init_by]).fetchone()
        conn.commit()
    return {"success": True, **s3}


def _get_doc(client_id: int, document_id: str) -> dict[str, Any] | None:
    sql = "SELECT * FROM public.documents WHERE document_id = %s::uuid AND client_id = %s"
    with _conn() as conn:
        row = conn.execute(sql, [document_id, client_id]).fetchone()
    return dict(row) if row else None


def _default_source_category_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM public.source_categories WHERE is_active = true ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not row:
        raise SupabaseRestError(422, "No hay source_categories activas para indexar documentos.")
    return int(row["id"])


def documents_presign_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    filename = str(body.get("filename") or "").strip()
    mime = str(body.get("mimeType") or "").strip()
    size = int(body.get("fileSizeBytes") or 0)
    if not filename:
        raise SupabaseRestError(400, "filename requerido")
    if not mime_allowed(mime):
        raise SupabaseRestError(400, "mimeType no permitido")
    if size <= 0 or size > max_upload_bytes():
        raise SupabaseRestError(400, f"fileSizeBytes inválido (max {max_upload_bytes()})")

    req_id = (body.get("requestId") or "").strip() or None
    if req_id:
        with _conn() as conn:
            prev = conn.execute(
                """
                SELECT document_id, s3_bucket, s3_key, mime_type, status
                FROM public.documents
                WHERE client_id = %s AND request_id = %s
                LIMIT 1
                """,
                [client_id, req_id],
            ).fetchone()
        if prev and prev["status"] == "pending_upload":
            url, headers, ttl = presigned_put_url(s3_key=prev["s3_key"], mime_type=prev["mime_type"] or mime)
            return {
                "success": True,
                "documentId": str(prev["document_id"]),
                "uploadUrl": url,
                "uploadMethod": "PUT",
                "uploadHeaders": headers,
                "s3Bucket": prev["s3_bucket"],
                "s3Key": prev["s3_key"],
                "expiresInSeconds": ttl,
            }

    ensure_client_storage_prefix(client_id, initialized_by=f"user:{user_id}")
    doc_id = str(uuid.uuid4())
    from nuwa_s3_documents import _bucket

    bucket = _bucket()
    s3_key = document_s3_key(client_id, doc_id, filename)
    file_type = str(body.get("fileType") or "otro")
    category = body.get("category")

    sql = """
    INSERT INTO public.documents (
      document_id, client_id, user_id, filename, original_filename, mime_type,
      file_size_bytes, file_type, category, s3_bucket, s3_key, status, request_id
    ) VALUES (
      %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_upload', %s
    )
    RETURNING *
    """
    with _conn() as conn:
        row = conn.execute(
            sql,
            [
                doc_id,
                client_id,
                user_id,
                filename,
                filename,
                mime,
                size,
                file_type,
                category,
                bucket,
                s3_key,
                req_id,
            ],
        ).fetchone()
        conn.commit()

    url, headers, ttl = presigned_put_url(s3_key=s3_key, mime_type=mime)
    return {
        "success": True,
        "documentId": doc_id,
        "uploadUrl": url,
        "uploadMethod": "PUT",
        "uploadHeaders": headers,
        "s3Bucket": bucket,
        "s3Key": s3_key,
        "expiresInSeconds": ttl,
    }


def documents_upload_complete_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    document_id = str(body["documentId"])
    row = _get_doc(client_id, document_id)
    if not row:
        raise SupabaseRestError(404, "Documento no encontrado")
    if row["status"] == "deleted":
        raise SupabaseRestError(404, "Documento eliminado")

    meta = head_object(row["s3_key"])
    if not meta:
        raise SupabaseRestError(409, "upload_not_found")

    size = int(meta.get("ContentLength") or 0)
    sql = """
    UPDATE public.documents SET status = 'uploaded', file_size_bytes = %s, updated_at = now()
    WHERE document_id = %s::uuid AND client_id = %s
    RETURNING *
    """
    with _conn() as conn:
        upd = conn.execute(sql, [size, document_id, client_id]).fetchone()
        conn.commit()
    return {"success": True, "documentId": document_id, "status": upd["status"], "fileSizeBytes": size}


def _fetch_all_entity_candidates(client_id: int) -> list[dict[str, Any]]:
    sql = """
    SELECT e.* FROM public.entities e
    WHERE e.client_id = %s AND e.status <> 'deleted'
    ORDER BY e.updated_at DESC LIMIT 500
    """
    with _conn() as conn:
        rows = conn.execute(sql, [client_id]).fetchall()
    return [dict(r) for r in rows]


def _party_entity_body(client_id: int, user_id: int, party: dict[str, Any], document_id: str) -> dict[str, Any]:
    party_type = party.get("partyType") or party.get("party_type") or "organization"
    if party_type not in ("individual", "organization"):
        party_type = "organization" if party.get("legalName") or party.get("rfc") else "individual"
    rfc = party.get("rfc")
    curp = party.get("curp")
    body: dict[str, Any] = {
        "clientId": client_id,
        "userId": user_id,
        "partyType": party_type,
        "name": party.get("name"),
        "legalName": party.get("legalName") or party.get("name"),
        "fullName": party.get("name"),
        "firstName": party.get("firstName"),
        "lastName": party.get("lastName"),
        "rfc": rfc,
        "curp": curp,
        "country": party.get("country"),
        "category": "document_mention",
        "relationshipRole": party.get("role"),
        "metadata": {
            "phones": party.get("phones") or [],
            "emails": party.get("emails") or [],
            "address": party.get("address"),
            "originDocumentId": document_id,
        },
    }
    return body


def _resolve_party_entity(
    *,
    client_id: int,
    user_id: int,
    party: dict[str, Any],
    document_id: str,
    primary_entity_id: str | None,
    candidates: list[dict[str, Any]],
    auto_create: bool,
) -> tuple[str | None, str]:
    """Returns (entity_id, action) action in matched|created|skipped|primary."""
    from nuwa_entities_pg import entities_create_pg

    rfc = party.get("rfc")
    curp = party.get("curp")
    if rfc:
        nr = normalize_identifier(str(rfc))
        for c in candidates:
            if c.get("rfc") and normalize_identifier(str(c["rfc"])) == nr:
                eid = str(c["id"])
                if primary_entity_id and eid == primary_entity_id:
                    return eid, "primary"
                return eid, "matched"
    if curp:
        nc = normalize_identifier(str(curp))
        for c in candidates:
            if c.get("curp") and normalize_identifier(str(c["curp"])) == nc:
                eid = str(c["id"])
                if primary_entity_id and eid == primary_entity_id:
                    return eid, "primary"
                return eid, "matched"

    party_type = party.get("partyType") or "organization"
    matches = find_matches(
        party_type=party_type if party_type in ("individual", "organization") else "organization",
        legal_name=party.get("legalName") or party.get("name"),
        first_name=party.get("firstName"),
        last_name=party.get("lastName"),
        full_name=party.get("name"),
        rfc=rfc,
        curp=curp,
        candidates=candidates,
        min_confidence=90,
    )
    if has_strong_match(matches):
        eid = matches[0]["entityId"]
        if primary_entity_id and eid == primary_entity_id:
            return eid, "primary"
        return eid, "matched"

    if not auto_create:
        return None, "skipped"

    eb = _party_entity_body(client_id, user_id, party, document_id)
    created = entities_create_pg(eb)
    eid = created["entityId"]
    candidates.append({"id": eid, **{k: eb.get(k) for k in eb}})
    return eid, "created"


def _upsert_link(
    conn,
    *,
    client_id: int,
    document_id: str,
    entity_id: str,
    role: str | None,
    is_primary: bool,
    confidence: float | None,
    payload: dict[str, Any],
    mention_source: str = "grok",
) -> None:
    conn.execute(
        """
        INSERT INTO public.document_entity_links (
          client_id, document_id, entity_id, role, is_primary, confidence,
          mention_source, mention_payload
        ) VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s)
        ON CONFLICT (document_id, entity_id) DO UPDATE SET
          role = EXCLUDED.role,
          is_primary = EXCLUDED.is_primary,
          confidence = EXCLUDED.confidence,
          mention_payload = EXCLUDED.mention_payload
        """,
        [
            client_id,
            document_id,
            entity_id,
            role,
            is_primary,
            confidence,
            mention_source,
            Json(payload),
        ],
    )


def documents_finalize_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    user_id = int(body["userId"])
    document_id = str(body["documentId"])
    extracted = body.get("extractedJson") or {}
    if not isinstance(extracted, dict):
        raise SupabaseRestError(400, "extractedJson debe ser objeto")

    row = _get_doc(client_id, document_id)
    if not row:
        raise SupabaseRestError(404, "Documento no encontrado")
    if row["status"] not in ("uploaded", "processing", "ready"):
        raise SupabaseRestError(409, f"Estado inválido para finalize: {row['status']}")

    fin_req = (body.get("requestId") or "").strip()
    if row["status"] == "ready" and fin_req:
        meta = row.get("extracted_json") if isinstance(row.get("extracted_json"), dict) else {}
        if meta.get("_finalizeRequestId") == fin_req:
            return {
                "success": True,
                "documentId": document_id,
                "status": "ready",
                "primaryEntityId": str(row["primary_entity_id"]) if row.get("primary_entity_id") else None,
                "linksCreated": meta.get("_linksCreated", 0),
                "entitiesCreated": meta.get("_entitiesCreated", 0),
                "entitiesMatched": meta.get("_entitiesMatched", 0),
                "sourceId": int(row["source_id"]) if row.get("source_id") else None,
                "idempotent": True,
            }

    primary_entity_id = body.get("primaryEntityId")
    if primary_entity_id:
        primary_entity_id = str(primary_entity_id)
        with _conn() as conn:
            er = conn.execute(
                "SELECT id FROM public.entities WHERE id = %s::uuid AND client_id = %s AND status <> 'deleted'",
                [primary_entity_id, client_id],
            ).fetchone()
        if not er:
            raise SupabaseRestError(400, "primaryEntityId no existe en el tenant")

    auto_create = body.get("autoCreateEntities", True) is not False
    auto_index = body.get("autoIndex", True) is not False
    extracted_text = body.get("extractedText") or extracted.get("summary") or ""
    summary = extracted.get("summary")
    doc_date_raw = extracted.get("dateIssued") or extracted.get("documentDate")
    doc_date = None
    if doc_date_raw:
        try:
            doc_date = date.fromisoformat(str(doc_date_raw)[:10])
        except ValueError:
            doc_date = None

    if fin_req:
        extracted = {**extracted, "_finalizeRequestId": fin_req}

    candidates = _fetch_all_entity_candidates(client_id)
    links_created = 0
    entities_created = 0
    entities_matched = 0
    source_id: int | None = int(row["source_id"]) if row.get("source_id") else None
    chunk_texts: list[str] = []

    with _conn() as conn:
        conn.execute(
            """
            UPDATE public.documents SET
              status = 'processing', extracted_json = %s, extracted_text = %s,
              summary = %s, document_date = %s, updated_at = now()
            WHERE document_id = %s::uuid
            """,
            [Json(extracted), extracted_text, summary, doc_date, document_id],
        )

        if primary_entity_id:
            conn.execute(
                "UPDATE public.documents SET primary_entity_id = %s::uuid WHERE document_id = %s::uuid",
                [primary_entity_id, document_id],
            )
            conn.execute(
                "UPDATE public.document_entity_links SET is_primary = false WHERE document_id = %s::uuid",
                [document_id],
            )
            _upsert_link(
                conn,
                client_id=client_id,
                document_id=document_id,
                entity_id=primary_entity_id,
                role="primary",
                is_primary=True,
                confidence=100.0,
                payload={"source": "user_primary"},
                mention_source="manual",
            )
            links_created += 1

        for party in extracted.get("parties") or []:
            if not isinstance(party, dict):
                continue
            eid, action = _resolve_party_entity(
                client_id=client_id,
                user_id=user_id,
                party=party,
                document_id=document_id,
                primary_entity_id=primary_entity_id,
                candidates=candidates,
                auto_create=auto_create,
            )
            if not eid:
                continue
            if action == "created":
                entities_created += 1
            elif action in ("matched", "primary"):
                entities_matched += 1
            is_pri = primary_entity_id == eid
            conf = party.get("confidence")
            try:
                conf_f = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf_f = None
            _upsert_link(
                conn,
                client_id=client_id,
                document_id=document_id,
                entity_id=eid,
                role=party.get("role"),
                is_primary=is_pri,
                confidence=conf_f,
                payload=party,
            )
            links_created += 1

        if auto_index:
            chunk_texts = build_index_chunks(extracted)
            if chunk_texts and source_id is None:
                scid = _default_source_category_id(conn)
                ins = conn.execute(
                    """
                    INSERT INTO public.sources (
                      name, risk_level, visibility, client_id, created_by_user_id,
                      metadata, source_category_id
                    ) VALUES (%s, 1, 'private', %s, %s, %s, %s)
                    RETURNING id
                    """,
                    [
                        f"doc:{document_id}:{row['original_filename']}",
                        client_id,
                        user_id,
                        Json(
                            {
                                "documentId": document_id,
                                "clientId": client_id,
                                "primaryEntityId": primary_entity_id,
                                "documentType": extracted.get("documentType"),
                            }
                        ),
                        scid,
                    ],
                ).fetchone()
                source_id = int(ins["id"])
                conn.execute(
                    "UPDATE public.documents SET source_id = %s WHERE document_id = %s::uuid",
                    [source_id, document_id],
                )

        extracted_out = {
            **extracted,
            "_linksCreated": links_created,
            "_entitiesCreated": entities_created,
            "_entitiesMatched": entities_matched,
        }
        conn.execute(
            """
            UPDATE public.documents SET status = 'ready', extracted_json = %s, updated_at = now()
            WHERE document_id = %s::uuid
            """,
            [Json(extracted_out), document_id],
        )
        conn.commit()

    if auto_index and source_id and chunk_texts:
        ingest_chunks(
            source_id=source_id,
            viewer_client_id=client_id,
            is_super_admin=False,
            chunk_texts=chunk_texts,
            replace_strategy="all",
            risk_level=None,
            visibility="private",
            entity_type="document",
        )

    return {
        "success": True,
        "documentId": document_id,
        "status": "ready",
        "primaryEntityId": primary_entity_id,
        "linksCreated": links_created,
        "entitiesCreated": entities_created,
        "entitiesMatched": entities_matched,
        "sourceId": source_id,
    }


def documents_list_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    lim = max(1, min(int(body.get("limit") or 50), 200))
    off = max(0, int(body.get("offset") or 0))
    where = ["client_id = %s", "status <> 'deleted'"]
    params: list[Any] = [client_id]
    if body.get("status"):
        where.append("status = %s")
        params.append(body["status"])
    if body.get("fileType"):
        where.append("file_type = %s")
        params.append(body["fileType"])
    if body.get("primaryEntityId"):
        where.append("primary_entity_id = %s::uuid")
        params.append(str(body["primaryEntityId"]))
    search = (body.get("search") or "").strip()
    if search:
        where.append(
            "(original_filename ILIKE %s OR summary ILIKE %s OR extracted_text ILIKE %s)"
        )
        pat = f"%{search}%"
        params.extend([pat, pat, pat])
    wsql = " AND ".join(where)
    include_ex = bool(body.get("includeExtractedJson"))
    with _conn() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*)::int AS c FROM public.documents WHERE {wsql}", params).fetchone()["c"]
        )
        rows = conn.execute(
            f"SELECT * FROM public.documents WHERE {wsql} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*params, lim, off],
        ).fetchall()
    return {
        "items": [_doc_summary(dict(r), include_extracted=include_ex) for r in rows],
        "total": total,
        "limit": lim,
        "offset": off,
    }


def documents_get_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    document_id = str(body["documentId"])
    row = _get_doc(client_id, document_id)
    if not row or row["status"] == "deleted":
        raise SupabaseRestError(404, "Documento no encontrado")
    doc = _doc_summary(row, include_extracted=True)
    with _conn() as conn:
        links = conn.execute(
            """
            SELECT l.*, e.name AS entity_name, e.party_type, e.category AS entity_category
            FROM public.document_entity_links l
            JOIN public.entities e ON e.id = l.entity_id
            WHERE l.document_id = %s::uuid AND l.client_id = %s
            ORDER BY l.is_primary DESC, l.created_at ASC
            """,
            [document_id, client_id],
        ).fetchall()
    doc["entityLinks"] = [
        {
            "linkId": str(l["link_id"]),
            "entityId": str(l["entity_id"]),
            "entityName": l.get("entity_name"),
            "partyType": l.get("party_type"),
            "entityCategory": l.get("entity_category"),
            "role": l.get("role"),
            "isPrimary": bool(l["is_primary"]),
            "confidence": float(l["confidence"]) if l.get("confidence") is not None else None,
            "mentionSource": l.get("mention_source"),
        }
        for l in links
    ]
    return doc


def documents_update_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    document_id = str(body["documentId"])
    row = _get_doc(client_id, document_id)
    if not row or row["status"] == "deleted":
        raise SupabaseRestError(404, "Documento no encontrado")

    sets: list[str] = []
    vals: list[Any] = []
    for col, key in (
        ("file_type", "fileType"),
        ("category", "category"),
        ("description", "description"),
    ):
        if key in body:
            sets.append(f"{col} = %s")
            vals.append(body[key])
    if "tags" in body and isinstance(body["tags"], list):
        sets.append("tags = %s")
        vals.append(Json(body["tags"]))
    if "primaryEntityId" in body:
        pe = body["primaryEntityId"]
        sets.append("primary_entity_id = %s")
        vals.append(str(pe) if pe else None)

    if not sets:
        raise SupabaseRestError(400, "Nada que actualizar")

    with _conn() as conn:
        conn.execute(
            f"UPDATE public.documents SET {', '.join(sets)}, updated_at = now() WHERE document_id = %s::uuid AND client_id = %s",
            [*vals, document_id, client_id],
        )
        if "primaryEntityId" in body and body["primaryEntityId"]:
            peid = str(body["primaryEntityId"])
            conn.execute(
                "UPDATE public.document_entity_links SET is_primary = false WHERE document_id = %s::uuid",
                [document_id],
            )
            _upsert_link(
                conn,
                client_id=client_id,
                document_id=document_id,
                entity_id=peid,
                role="primary",
                is_primary=True,
                confidence=100.0,
                payload={"source": "user_update"},
                mention_source="manual",
            )
        conn.commit()
    return documents_get_pg(body)


def documents_delete_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    document_id = str(body["documentId"])
    row = _get_doc(client_id, document_id)
    if not row or row["status"] == "deleted":
        raise SupabaseRestError(404, "Documento no encontrado")
    delete_s3 = bool(body.get("deleteFromS3"))
    if delete_s3:
        from nuwa_s3_documents import delete_object

        try:
            delete_object(row["s3_key"])
        except Exception:
            pass
    with _conn() as conn:
        conn.execute(
            """
            UPDATE public.documents SET status = 'deleted', deleted_at = now(), updated_at = now()
            WHERE document_id = %s::uuid AND client_id = %s
            """,
            [document_id, client_id],
        )
        conn.commit()
    return {"success": True, "documentId": document_id, "status": "deleted"}


def documents_download_url_pg(body: dict[str, Any]) -> dict[str, Any]:
    client_id = int(body["clientId"])
    document_id = str(body["documentId"])
    row = _get_doc(client_id, document_id)
    if not row or row["status"] == "deleted":
        raise SupabaseRestError(404, "Documento no encontrado")
    url, ttl = presigned_get_url(s3_key=row["s3_key"])
    return {"downloadUrl": url, "expiresInSeconds": ttl, "documentId": document_id}


def promote_entity_after_screening_pg(entity_id: str, client_id: int) -> None:
    """document_mention → screening tras primer reporte."""
    sql = """
    UPDATE public.entities SET category = 'screening', updated_at = now()
    WHERE id = %s::uuid AND client_id = %s AND category = 'document_mention' AND status <> 'deleted'
    """
    with _conn() as conn:
        conn.execute(sql, [entity_id, client_id])
        conn.commit()
