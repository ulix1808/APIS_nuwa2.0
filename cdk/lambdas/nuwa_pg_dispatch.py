"""
Acceso directo a PostgreSQL cuando NUWA_DATABASE_SECRET_ARN está definido.
Emula el subconjunto de PostgREST usado por los handlers (reports, admin).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote

import psycopg
import psycopg.errors
from psycopg.rows import dict_row
from psycopg.types.json import Json

from nuwa_config import get_database_config
from nuwa_errors import SupabaseRestError
from nuwa_obs_log import log_await, log_done, log_phase


def _parse_query(qs: str | None) -> dict[str, str]:
    if not qs:
        return {}
    out: dict[str, str] = {}
    for part in qs.split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[unquote(k)] = unquote(v)
    return out


def _eq(parts: dict[str, str], key: str) -> str | None:
    v = parts.get(key)
    if v is None:
        return None
    if v.startswith("eq."):
        return v[3:]
    return None


def _safe_ident_list(s: str, allowed: frozenset[str]) -> str:
    """Devuelve lista de identificadores SQL separados por coma o *."""
    if s == "*":
        return "*"
    cols = [c.strip() for c in s.split(",") if c.strip()]
    for c in cols:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", c) or c not in allowed:
            raise SupabaseRestError(400, f"columna no permitida en select: {c}")
    return ", ".join(cols)


_COMPANY_COLS = frozenset(
    {
        "id",
        "client_id",
        "name",
        "details",
        "apigw_key_id",
        "apigw_key_secret",
        "created_at",
        "updated_at",
    }
)


def _companies_select_sql(parts: dict[str, str]) -> str:
    raw = (parts.get("select") or "").strip()
    if not raw or raw == "*":
        return "id, client_id, name, details, apigw_key_id, created_at, updated_at"
    cols = _safe_ident_list(raw, _COMPANY_COLS)
    return cols


_REPORT_COLS = frozenset(
    {
        "id",
        "folio",
        "client_id",
        "created_by_user_id",
        "report_json",
        "search_context",
        "title",
        "status",
        "created_at",
        "updated_at",
        "entidad",
        "tipo_consulta",
        "fecha",
        "hora",
        "nivel_riesgo",
        "nivel_riesgo_numerico",
        "total_listas_original",
        "total_listas_activas",
        "total_descartadas",
        "es_actualizacion",
        "total_listas",
        "total_menciones",
        "grok_resumen",
        "grok_falsos_positivos",
        "grok_confirmados",
    }
)


@contextmanager
def _conn():
    cfg = get_database_config()
    conninfo = (
        f"host={cfg['host']} port={cfg['port']} dbname={cfg['dbname']} "
        f"user={cfg['user']} password={cfg['password']} sslmode={cfg['sslmode']}"
    )
    target = f"{cfg['host']}:{cfg['port']} dbname={cfg['dbname']}"
    log_await("postgresql", "connect", target)
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        log_done("postgresql", "connect", target)
        yield conn


def fetch_user_with_role_pg(*, user_id: int) -> dict[str, Any] | None:
    sql = """
    SELECT u.id, u.client_id, u.email, u.full_name, u.role_id, u.is_active, r.slug AS role_slug
    FROM public.nuwa_users u
    JOIN public.nuwa_roles r ON r.id = u.role_id
    WHERE u.id = %s
    """
    with _conn() as conn:
        row = conn.execute(sql, (user_id,)).fetchone()
    if not row or not row.get("is_active", True):
        return None
    return {
        "id": int(row["id"]),
        "client_id": int(row["client_id"]),
        "email": row["email"],
        "full_name": row.get("full_name") or "",
        "role_slug": row["role_slug"],
    }


def _reports_get(parts: dict[str, str]) -> list[dict[str, Any]]:
    sel = parts.get("select", "*")
    try:
        cols = _safe_ident_list(sel, _REPORT_COLS)
    except SupabaseRestError:
        cols = "*"

    where = ["1=1"]
    params: list[Any] = []
    st = _eq(parts, "status")
    if st is not None:
        params.append(st)
        where.append(f"status = %s")
    fo = _eq(parts, "folio")
    if fo is not None:
        params.append(fo)
        where.append("folio = %s")
    cid = _eq(parts, "client_id")
    if cid is not None:
        params.append(int(cid))
        where.append("client_id = %s")
    uid = _eq(parts, "created_by_user_id")
    if uid is not None:
        params.append(int(uid))
        where.append("created_by_user_id = %s")

    order_sql = "ORDER BY created_at DESC"
    ordv = parts.get("order", "")
    if ordv == "created_at.desc":
        order_sql = "ORDER BY created_at DESC"

    lim = int(parts.get("limit", "1000"))
    off = int(parts.get("offset", "0"))
    lim = max(1, min(lim, 1000))
    off = max(0, off)

    sql = f"SELECT {cols} FROM public.reports WHERE {' AND '.join(where)} {order_sql} LIMIT %s OFFSET %s"
    params.extend([lim, off])
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _reports_post(body: dict[str, Any]) -> list[dict[str, Any]]:
    body = {k: v for k, v in body.items() if k in _REPORT_COLS}
    keys = list(body.keys())
    if not keys:
        raise SupabaseRestError(400, "body vacío")
    placeholders = ", ".join(["%s"] * len(keys))
    colnames = ", ".join(keys)
    vals: list[Any] = []
    for k in keys:
        v = body[k]
        if k in ("report_json", "search_context", "details") and isinstance(v, (dict, list)):
            vals.append(Json(v))
        else:
            vals.append(v)
    sql = f"""
    INSERT INTO public.reports ({colnames})
    VALUES ({placeholders})
    RETURNING *
    """
    try:
        with _conn() as conn:
            row = conn.execute(sql, vals).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise SupabaseRestError(409, str(e)) from e
    except psycopg.errors.ForeignKeyViolation as e:
        raise SupabaseRestError(400, str(e)) from e
    if not row:
        return []
    return [dict(row)]


def _reports_patch(parts: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    rid = _eq(parts, "id")
    if not rid:
        raise SupabaseRestError(400, "PATCH reports requiere id=eq.uuid")
    sets = []
    vals: list[Any] = []
    for k, v in body.items():
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", k):
            continue
        if k not in _REPORT_COLS:
            continue
        sets.append(f"{k} = %s")
        if k in ("report_json", "search_context") and isinstance(v, (dict, list)):
            vals.append(Json(v))
        else:
            vals.append(v)
    if not sets:
        raise SupabaseRestError(400, "nada que actualizar")
    vals.append(rid)
    sql = f"UPDATE public.reports SET {', '.join(sets)} WHERE id = %s::uuid RETURNING *"
    with _conn() as conn:
        row = conn.execute(sql, vals).fetchone()
        conn.commit()
    if not row:
        return []
    return [dict(row)]


def _nuwa_users_get(parts: dict[str, str]) -> list[dict[str, Any]]:
    sel = parts.get("select", "*")
    allowed = frozenset(
        {
            "id",
            "client_id",
            "email",
            "full_name",
            "role_id",
            "is_active",
            "created_at",
            "password_hash",
            "updated_at",
        }
    )
    try:
        cols = _safe_ident_list(sel, allowed)
    except SupabaseRestError:
        cols = "*"

    where = ["1=1"]
    params: list[Any] = []
    uid = _eq(parts, "id")
    if uid is not None:
        params.append(int(uid))
        where.append("id = %s")
    cid = _eq(parts, "client_id")
    if cid is not None:
        params.append(int(cid))
        where.append("client_id = %s")
    em = _eq(parts, "email")
    if em is not None:
        params.append(em)
        where.append("email = %s")

    sql = f"SELECT {cols} FROM public.nuwa_users WHERE {' AND '.join(where)}"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _nuwa_users_post(body: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ["client_id", "email", "password_hash", "full_name", "role_id", "is_active"]
    vals = [
        body["client_id"],
        body["email"],
        body["password_hash"],
        body["full_name"],
        int(body["role_id"]),
        bool(body.get("is_active", True)),
    ]
    sql = """
    INSERT INTO public.nuwa_users (client_id, email, password_hash, full_name, role_id, is_active)
    VALUES (%s,%s,%s,%s,%s,%s)
    RETURNING id, client_id, email, full_name, role_id, is_active, created_at
    """
    try:
        with _conn() as conn:
            row = conn.execute(sql, vals).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise SupabaseRestError(409, str(e)) from e
    except psycopg.errors.ForeignKeyViolation as e:
        raise SupabaseRestError(400, str(e)) from e
    return [dict(row)] if row else []


def _nuwa_users_patch(parts: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    uid = _eq(parts, "id")
    if not uid:
        raise SupabaseRestError(400, "PATCH nuwa_users requiere id=eq.")
    allowed_cols = frozenset({"full_name", "role_id", "is_active", "password_hash"})
    sets = []
    vals: list[Any] = []
    for k, v in body.items():
        if k not in allowed_cols:
            continue
        sets.append(f"{k} = %s")
        vals.append(v)
    if not sets:
        raise SupabaseRestError(400, "nada que actualizar")
    vals.append(int(uid))
    sql = f"UPDATE public.nuwa_users SET {', '.join(sets)} WHERE id = %s RETURNING *"
    with _conn() as conn:
        row = conn.execute(sql, vals).fetchone()
        conn.commit()
    return [dict(row)] if row else []


def _companies_get(parts: dict[str, str]) -> list[dict[str, Any]]:
    cols_sql = _companies_select_sql(parts)
    where = ["1=1"]
    params: list[Any] = []
    cid = _eq(parts, "client_id")
    if cid is not None:
        params.append(int(cid))
        where.append("client_id = %s")
    order_sql = "ORDER BY id ASC" if parts.get("order") == "id.asc" else ""
    sql = f"SELECT {cols_sql} FROM public.companies WHERE {' AND '.join(where)} {order_sql}"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _companies_post(body: dict[str, Any]) -> list[dict[str, Any]]:
    sql = """
    INSERT INTO public.companies (client_id, name, details)
    VALUES (%s, %s, %s)
    RETURNING id, client_id, name, details, apigw_key_id, created_at, updated_at
    """
    vals = [int(body["client_id"]), body["name"], Json(body.get("details") or {})]
    try:
        with _conn() as conn:
            row = conn.execute(sql, vals).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise SupabaseRestError(409, str(e)) from e
    return [dict(row)] if row else []


def _companies_patch(parts: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    cid = _eq(parts, "client_id")
    if cid is None:
        raise SupabaseRestError(400, "PATCH companies requiere client_id=eq.")
    sets = []
    vals: list[Any] = []
    if "name" in body:
        sets.append("name = %s")
        vals.append(body["name"])
    if "details" in body:
        sets.append("details = %s")
        vals.append(Json(body["details"]))
    if "apigw_key_id" in body:
        sets.append("apigw_key_id = %s")
        vals.append(body["apigw_key_id"])
    if "apigw_key_secret" in body:
        sets.append("apigw_key_secret = %s")
        vals.append(body["apigw_key_secret"])
    if not sets:
        raise SupabaseRestError(400, "name o details")
    vals.append(int(cid))
    sql = f"""
    UPDATE public.companies SET {", ".join(sets)}
    WHERE client_id = %s
    RETURNING id, client_id, name, details, apigw_key_id, created_at, updated_at
    """
    with _conn() as conn:
        row = conn.execute(sql, vals).fetchone()
        conn.commit()
    return [dict(row)] if row else []


def _companies_delete(parts: dict[str, str]) -> None:
    cid = _eq(parts, "client_id")
    if cid is None:
        raise SupabaseRestError(400, "DELETE companies requiere client_id=eq.")
    with _conn() as conn:
        conn.execute("DELETE FROM public.companies WHERE client_id = %s", (int(cid),))
        conn.commit()


def _nuwa_roles_get() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM public.nuwa_roles ORDER BY id ASC",
        ).fetchall()
    return [dict(r) for r in rows]


def _iso_z(dt: Any) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        s = dt.isoformat()
        if s.endswith("+00:00"):
            return s[:-6] + "Z"
        return s
    return str(dt)


def source_row_to_api(row: dict[str, Any]) -> dict[str, Any]:
    md = row.get("metadata")
    if md is None or not isinstance(md, dict):
        md = {}
    return {
        "sourceId": int(row["id"]),
        "name": row["name"],
        "riskLevel": int(row["risk_level"]),
        "visibility": row["visibility"],
        "clientId": int(row["client_id"]),
        "createdByUserId": int(row["created_by_user_id"]),
        "metadata": md,
        "createdAt": _iso_z(row.get("created_at")),
        "updatedAt": _iso_z(row.get("updated_at")),
    }


def list_sources_pg(viewer_client_id: int, limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    where = "(visibility = 'public' OR client_id = %s)"
    with _conn() as conn:
        crow = conn.execute(
            f"SELECT COUNT(*)::bigint AS n FROM public.sources WHERE {where}",
            (viewer_client_id,),
        ).fetchone()
        total = int(crow["n"]) if crow else 0
        rows = conn.execute(
            f"""
            SELECT id, name, risk_level, visibility, client_id, created_by_user_id,
                   metadata, created_at, updated_at
            FROM public.sources
            WHERE {where}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (viewer_client_id, lim, off),
        ).fetchall()
    return total, [dict(r) for r in rows]


def get_source_visible_pg(source_id: int, viewer_client_id: int, is_super_admin: bool) -> dict[str, Any] | None:
    with _conn() as conn:
        if is_super_admin:
            row = conn.execute(
                """
                SELECT id, name, risk_level, visibility, client_id, created_by_user_id,
                       metadata, created_at, updated_at
                FROM public.sources WHERE id = %s
                """,
                (source_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, name, risk_level, visibility, client_id, created_by_user_id,
                       metadata, created_at, updated_at
                FROM public.sources
                WHERE id = %s AND (visibility = 'public' OR client_id = %s)
                """,
                (source_id, viewer_client_id),
            ).fetchone()
    return dict(row) if row else None


def fetch_source_by_id_pg(source_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT id, name, risk_level, visibility, client_id, created_by_user_id,
                   metadata, created_at, updated_at
            FROM public.sources WHERE id = %s
            """,
            (source_id,),
        ).fetchone()
    return dict(row) if row else None


def can_mutate_source_row(row: dict[str, Any], viewer_client_id: int, is_super_admin: bool) -> bool:
    if is_super_admin:
        return True
    return int(row["client_id"]) == int(viewer_client_id)


def create_source_pg(
    *,
    name: str,
    risk_level: int,
    visibility: str,
    client_id: int,
    created_by_user_id: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    sql = """
    INSERT INTO public.sources (name, risk_level, visibility, client_id, created_by_user_id, metadata)
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING id, name, risk_level, visibility, client_id, created_by_user_id, metadata, created_at, updated_at
    """
    vals = [name, risk_level, visibility, client_id, created_by_user_id, Json(metadata)]
    with _conn() as conn:
        row = conn.execute(sql, vals).fetchone()
        conn.commit()
    if not row:
        raise SupabaseRestError(500, "INSERT sources sin fila")
    return dict(row)


def update_source_pg(
    source_id: int,
    *,
    name: str | None,
    risk_level: int | None,
    visibility: str | None,
    metadata: dict[str, Any] | None,
    viewer_client_id: int,
    is_super_admin: bool,
) -> dict[str, Any] | None:
    row0 = fetch_source_by_id_pg(source_id)
    if not row0:
        return None
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        raise SupabaseRestError(403, "Sin permiso para actualizar esta fuente.")
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        vals.append(name)
    if risk_level is not None:
        sets.append("risk_level = %s")
        vals.append(int(risk_level))
    if visibility is not None:
        sets.append("visibility = %s")
        vals.append(visibility)
    if metadata is not None:
        sets.append("metadata = %s")
        vals.append(Json(metadata))
    if not sets:
        return row0
    vals.append(source_id)
    sql = f"""
    UPDATE public.sources SET {", ".join(sets)}
    WHERE id = %s
    RETURNING id, name, risk_level, visibility, client_id, created_by_user_id, metadata, created_at, updated_at
    """
    with _conn() as conn:
        row = conn.execute(sql, vals).fetchone()
        conn.commit()
    return dict(row) if row else None


def delete_source_pg(source_id: int, viewer_client_id: int, is_super_admin: bool) -> str:
    """'ok' | 'not_found' | 'forbidden'"""
    row0 = fetch_source_by_id_pg(source_id)
    if not row0:
        return "not_found"
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        return "forbidden"
    with _conn() as conn:
        conn.execute("DELETE FROM public.sources WHERE id = %s", (source_id,))
        conn.commit()
    return "ok"


def ingest_chunks_pg(
    source_id: int,
    *,
    viewer_client_id: int,
    is_super_admin: bool,
    replace_strategy: str,
    chunk_texts: list[str],
    risk_level: int | None,
    visibility: str | None,
    entity_type: str | None,
) -> dict[str, Any]:
    """
    Inserta filas en public.risk_entity_chunks. client_id se toma de la fuente (catálogo).
    replace_strategy: 'all' borra antes todos los chunks de source_id; 'append' solo inserta.
    Si risk_level, visibility o entity_type son None, se toman de la fuente (entity_type default "entity").
    """
    row0 = fetch_source_by_id_pg(source_id)
    if not row0:
        raise SupabaseRestError(404, "Fuente no encontrada.")
    if not can_mutate_source_row(row0, viewer_client_id, is_super_admin):
        raise SupabaseRestError(403, "Sin permiso para ingestar chunks en esta fuente.")
    if replace_strategy not in ("all", "append"):
        raise SupabaseRestError(400, "replaceStrategy debe ser all o append.")
    if not chunk_texts:
        raise SupabaseRestError(400, "chunks no puede estar vacío.")

    eff_rl = int(risk_level) if risk_level is not None else int(row0["risk_level"])
    eff_vis = visibility if visibility is not None else str(row0["visibility"])
    eff_et = (entity_type or "").strip() or "entity"
    if eff_rl not in (1, 2, 3):
        raise SupabaseRestError(400, "risk_level inválido.")
    if eff_vis not in ("public", "private"):
        raise SupabaseRestError(400, "visibility inválida.")

    client_id_src = int(row0["client_id"])
    deleted = 0
    insert_sql = """
    INSERT INTO public.risk_entity_chunks (client_id, risk_level, source_id, entity_type, chunk_text, visibility)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    batch = [
        (client_id_src, eff_rl, source_id, eff_et[:200], txt, eff_vis) for txt in chunk_texts
    ]
    with _conn() as conn:
        if replace_strategy == "all":
            cur = conn.execute(
                "DELETE FROM public.risk_entity_chunks WHERE source_id = %s",
                (source_id,),
            )
            deleted = int(cur.rowcount or 0)
        with conn.cursor() as cur:
            cur.executemany(insert_sql, batch)
        conn.commit()

    out: dict[str, Any] = {
        "sourceId": source_id,
        "status": "completed",
        "insertedChunks": len(chunk_texts),
    }
    if replace_strategy == "all":
        out["deletedChunks"] = deleted
    return out


def rest_json_pg(
    method: str,
    path: str,
    *,
    query: str | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> Any:
    path = path.strip().strip("/")
    m = method.upper()
    parts = _parse_query(query)
    log_phase("rest_json_pg", f"{m} path={path}")

    try:
        if path == "reports":
            if m == "GET":
                rows = _reports_get(parts)
                return rows
            if m == "POST":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _reports_post(body)
            if m == "PATCH":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _reports_patch(parts, body)
        if path == "nuwa_users":
            if m == "GET":
                return _nuwa_users_get(parts)
            if m == "POST":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _nuwa_users_post(body)
            if m == "PATCH":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _nuwa_users_patch(parts, body)
        if path == "companies":
            if m == "GET":
                return _companies_get(parts)
            if m == "POST":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _companies_post(body)
            if m == "PATCH":
                if not isinstance(body, dict):
                    raise SupabaseRestError(400, "body debe ser objeto")
                return _companies_patch(parts, body)
            if m == "DELETE":
                _companies_delete(parts)
                return None
        if path == "nuwa_roles" and m == "GET":
            return _nuwa_roles_get()
    except SupabaseRestError:
        raise
    except psycopg.Error as e:
        raise SupabaseRestError(500, str(e)) from e

    raise SupabaseRestError(404, f"PG: {m} {path} no soportado")


def search_risk_entities_pg(
    *,
    client_id: int,
    query: str = "",
    rfc: str | None = None,
    entity_types: list[str] | None = None,
    risk_levels: list[int] | None = None,
    limit: int = 20,
    word_similarity_threshold: float = 0.38,
) -> list[dict[str, Any]]:
    et = entity_types if entity_types else None
    rl = risk_levels if risk_levels else None
    log_phase("search_risk_entities_pg", f"client_id={client_id} limit={limit}")
    sql = """
    SELECT id, client_id, risk_level, source_id, entity_type, chunk_text, visibility,
           score, rank_ts, snippet
    FROM public.search_risk_entities(
        %s::integer, %s::text, %s::text, %s::text[], %s::smallint[], %s::integer, %s::real
    )
    """
    with _conn() as conn:
        rows = conn.execute(
            sql,
            (
                client_id,
                query or "",
                rfc,
                et,
                rl,
                limit,
                word_similarity_threshold,
            ),
        ).fetchall()
    return [dict(r) for r in rows]
