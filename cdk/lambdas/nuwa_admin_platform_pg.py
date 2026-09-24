"""Panel admin plataforma: clients, token usage, nuwa_users (invite/reset)."""

from __future__ import annotations

import re
import secrets
from typing import Any

import psycopg.errors

from nuwa_errors import SupabaseRestError
from nuwa_password import hash_password
from nuwa_pg_dispatch import _conn


ROLE_SLUG_TO_APP: dict[str, str] = {
    "super_admin": "master",
    "master": "master",
    "admin": "admin",
    "compliance_officer": "compliance_officer",
    "compliance": "compliance_officer",
    "analyst": "analyst",
    "analista": "analyst",
    "viewer": "viewer",
    "user": "analyst",
}

# Preferred nuwa_roles.slug candidates per app role (first match / insert wins).
APP_ROLE_SLUG_CANDIDATES: dict[str, list[str]] = {
    "master": ["super_admin", "master"],
    "compliance_officer": ["compliance_officer", "compliance"],
    "admin": ["admin", "administrator"],
    "analyst": ["analyst", "analista", "user"],
    "viewer": ["viewer", "read_only", "visualizador"],
}

APP_ROLE_DISPLAY_NAME: dict[str, str] = {
    "master": "Super Admin",
    "compliance_officer": "Compliance Officer",
    "admin": "Admin",
    "analyst": "Analyst",
    "viewer": "Viewer",
}


OPERATING_COUNTRY_CODES = ("mexico", "colombia", "costarica", "guatemala")


def _canonicalize_country_token(raw: str) -> str:
    token = re.sub(r"[\s_-]+", "", raw.strip().lower())
    if token == "méxico":
        return "mexico"
    return token


def parse_operating_countries(raw: Any) -> list[str] | None:
    """Uno o más de mexico|colombia|costarica|guatemala. None si falta o hay un valor inválido."""
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        return None
    out: list[str] = []
    for item in items:
        if not isinstance(item, (str, int)):
            return None
        code = _canonicalize_country_token(str(item))
        if code not in OPERATING_COUNTRY_CODES:
            return None
        if code not in out:
            out.append(code)
    return out or None


def _countries_from_row(row: dict[str, Any]) -> list[str]:
    raw = row.get("operating_countries")
    if isinstance(raw, str):
        raw = raw.strip().strip("{}")
    parsed = parse_operating_countries(raw) if raw not in (None, "") else None
    return parsed or ["mexico"]


def _countries_from_body(body: dict[str, Any]) -> list[str] | None:
    """None si el body no trae el campo. 400 si viene y no es válido."""
    if "operatingCountries" in body:
        raw = body.get("operatingCountries")
    elif "operating_countries" in body:
        raw = body.get("operating_countries")
    else:
        return None
    parsed = parse_operating_countries(raw)
    if not parsed:
        raise SupabaseRestError(
            400,
            "operatingCountries inválido. Usa mexico, colombia, costarica o guatemala.",
        )
    return parsed


def require_super_admin(actor: dict[str, Any]) -> None:
    if actor.get("role_slug") != "super_admin":
        raise SupabaseRestError(403, "Solo super_admin.")


def _generate_temp_password(length: int = 14) -> str:
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnpqrstuvwxyz"
    digits = "23456789"
    special = "!@#$%&*"
    all_chars = upper + lower + digits + special
    pick = lambda chars: secrets.choice(chars)  # noqa: E731
    base = [pick(upper), pick(lower), pick(digits), pick(special)]
    while len(base) < length:
        base.append(pick(all_chars))
    secrets.SystemRandom().shuffle(base)
    return "".join(base)


def _map_role_slug(slug: str | None) -> str:
    return ROLE_SLUG_TO_APP.get((slug or "user").lower(), "analyst")


def _normalize_app_role(role: str | None) -> str:
    raw = (role or "analyst").strip().lower().replace("-", "_")
    return ROLE_SLUG_TO_APP.get(raw, raw if raw in APP_ROLE_SLUG_CANDIDATES else "analyst")


def _resolve_role_id(conn: Any, role: str) -> int:
    app_role = _normalize_app_role(role)
    slugs = APP_ROLE_SLUG_CANDIDATES.get(app_role) or APP_ROLE_SLUG_CANDIDATES["analyst"]

    def lookup() -> int | None:
        rows = conn.execute(
            "SELECT id, slug FROM nuwa_roles WHERE LOWER(slug) = ANY(%s)",
            (list(slugs),),
        ).fetchall()
        rank = {s.lower(): i for i, s in enumerate(slugs)}
        rows = sorted(rows, key=lambda r: rank.get(str(r["slug"]).lower(), 99))
        if not rows:
            return None
        rid = int(rows[0]["id"])
        return rid if rid > 0 else None

    found = lookup()
    if found is not None:
        return found

    preferred = slugs[0]
    conn.execute(
        """
        INSERT INTO nuwa_roles (slug, name) VALUES (%s, %s)
        ON CONFLICT (slug) DO NOTHING
        """,
        (preferred, APP_ROLE_DISPLAY_NAME.get(app_role, preferred)),
    )
    created = lookup()
    if created is not None:
        return created
    raise SupabaseRestError(502, f"role_not_configured:{app_role}")


def _map_app_role_to_id(conn: Any, role: str) -> int:
    return _resolve_role_id(conn, role)


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value)[:10]
    return str(value)[:10]


def _user_api(row: dict[str, Any], *, company_name: str | None = None) -> dict[str, Any]:
    status = "active" if row.get("is_active", True) else "disabled"
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "name": row.get("full_name") or row.get("name") or row["email"],
        "role": _map_role_slug(row.get("role_slug")),
        "status": status,
        "clientId": int(row["client_id"]),
        "companyName": company_name or row.get("company_name"),
    }


def _usage_api(row: dict[str, Any] | None) -> dict[str, int]:
    if not row:
        return {"screenings": 0, "backgroundChecks": 0, "monitoring": 0, "targeted": 0}
    return {
        "screenings": int(row.get("screenings") or 0),
        "backgroundChecks": int(row.get("background_checks") or 0),
        "monitoring": int(row.get("monitoring") or 0),
        "targeted": int(row.get("targeted") or 0),
    }


def _client_api(row: dict[str, Any], users: list[dict[str, Any]] | None = None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row.get("name") or "",
        "rfc": row.get("rfc") or "",
        "legalRep": row.get("legal_rep") or "",
        "address": row.get("address") or "",
        "sector": row.get("sector"),
        "plan": row.get("plan") or "professional",
        "status": row.get("status") or "active",
        "tokenLimit": int(row.get("token_limit") or 2000),
        "tokensUsed": int(row.get("tokens_used") or 0),
        "billingContact": row.get("billing_contact") or "",
        "billingEmail": row.get("billing_email") or "",
        "paymentMethod": row.get("payment_method") or "",
        "nextInvoice": _iso_date(row.get("next_invoice")) or "---",
        "createdAt": _iso_date(row.get("created_at")) or "",
        "complianceOfficerUserId": row.get("compliance_officer_user_id"),
        "operatingCountries": _countries_from_row(row),
        "users": users or [],
        "usage": usage or _usage_api(None),
    }


CLIENT_SELECT = """
SELECT
  co.client_id AS id,
  COALESCE(cl.name, co.name) AS name,
  cl.rfc,
  cl.legal_rep,
  cl.address,
  cl.sector,
  COALESCE(cl.plan, 'professional') AS plan,
  COALESCE(cl.status, 'active') AS status,
  COALESCE(cl.token_limit, 2000) AS token_limit,
  COALESCE(cl.tokens_used, 0) AS tokens_used,
  cl.billing_contact,
  cl.billing_email,
  cl.payment_method,
  cl.next_invoice,
  cl.compliance_officer_user_id,
  COALESCE(cl.operating_countries, ARRAY['mexico']::text[]) AS operating_countries,
  COALESCE(cl.created_at, co.created_at) AS created_at,
  co.name AS company_name
FROM companies co
LEFT JOIN clients cl ON cl.id = co.client_id
"""


def _ensure_client_row(conn, client_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO clients (id, name, plan, status, token_limit, tokens_used)
        VALUES (%s, %s, 'professional', 'active', 2000, 0)
        ON CONFLICT (id) DO NOTHING
        """,
        (client_id, name),
    )
    conn.execute(
        "INSERT INTO client_token_usage (client_id) VALUES (%s) ON CONFLICT (client_id) DO NOTHING",
        (client_id,),
    )


def _fetch_client_users(conn, client_id: int, company_name: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT u.id, u.email, u.full_name, r.slug AS role_slug, u.is_active, u.client_id, co.name AS company_name
        FROM nuwa_users u
        JOIN nuwa_roles r ON r.id = u.role_id
        LEFT JOIN companies co ON co.client_id = u.client_id
        WHERE u.client_id = %s
        ORDER BY u.full_name ASC
        """,
        (client_id,),
    ).fetchall()
    return [_user_api(dict(r), company_name=company_name or r.get("company_name")) for r in rows]


def _fetch_usage(conn, client_id: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT screenings, background_checks, monitoring, targeted FROM client_token_usage WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    return _usage_api(dict(row) if row else None)


def clients_list(body: dict[str, Any]) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    status = body.get("status")
    if status and status != "all":
        conditions.append("COALESCE(cl.status, 'active') = %s")
        params.append(status)
    search = (body.get("search") or "").strip()
    if search:
        conditions.append("(co.name ILIKE %s OR cl.rfc ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = min(int(body.get("limit") or 200), 500)
    offset = max(int(body.get("offset") or 0), 0)

    with _conn() as conn:
        rows = conn.execute(
            f"{CLIENT_SELECT} {where} ORDER BY co.name ASC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        ).fetchall()
        clients: list[dict[str, Any]] = []
        for row in rows:
            r = dict(row)
            cid = int(r["id"])
            if not r.get("rfc"):
                _ensure_client_row(conn, cid, str(r["name"]))
            users = _fetch_client_users(conn, cid, str(r.get("company_name") or r["name"]))
            usage = _fetch_usage(conn, cid)
            clients.append(_client_api(r, users, usage))
        conn.commit()

    stats = {
        "totalClients": len(clients),
        "activeClients": sum(1 for c in clients if c["status"] in ("active", "trial")),
        "totalTokensAllocated": sum(c["tokenLimit"] for c in clients),
        "totalTokensConsumed": sum(c["tokensUsed"] for c in clients),
    }
    return {"success": True, "clients": clients, "stats": stats}


def clients_get(body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    with _conn() as conn:
        row = conn.execute(f"{CLIENT_SELECT} WHERE co.client_id = %s", (target,)).fetchone()
        if not row:
            raise SupabaseRestError(404, "Cliente no encontrado.")
        r = dict(row)
        _ensure_client_row(conn, target, str(r["name"]))
        users = _fetch_client_users(conn, target, str(r.get("company_name") or r["name"]))
        usage = _fetch_usage(conn, target)
        conn.commit()
    return {"success": True, "client": _client_api(r, users, usage), "users": users}


def clients_create(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body["name"]).strip()
    rfc = str(body.get("rfc") or "").strip().upper()
    if not name or not rfc:
        raise SupabaseRestError(400, "name y rfc requeridos.")
    countries = _countries_from_body(body) or ["mexico"]

    with _conn() as conn:
        next_id = conn.execute("SELECT COALESCE(MAX(client_id), 0) + 1 AS id FROM companies").fetchone()
        client_id = int(next_id["id"])
        conn.execute(
            "INSERT INTO companies (client_id, name, details) VALUES (%s, %s, %s::jsonb)",
            (client_id, name, '{"type": "tenant"}'),
        )
        cl = conn.execute(
            """
            INSERT INTO clients (
              id, name, rfc, legal_rep, address, sector, plan, token_limit,
              billing_contact, billing_email, payment_method, status, operating_countries
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s::text[])
            RETURNING *
            """,
            (
                client_id,
                name,
                rfc,
                body.get("legalRep"),
                body.get("address"),
                body.get("sector"),
                body.get("plan") or "professional",
                int(body.get("tokenLimit") or 2000),
                body.get("billingContact"),
                body.get("billingEmail"),
                body.get("paymentMethod"),
                countries,
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO client_token_usage (client_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (client_id,),
        )
        conn.commit()
    return {"success": True, "client": _client_api(dict(cl), [], _usage_api(None))}


def clients_update(body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    with _conn() as conn:
        if body.get("name"):
            conn.execute(
                "UPDATE companies SET name = %s, updated_at = NOW() WHERE client_id = %s",
                (body["name"], target),
            )
        _ensure_client_row(conn, target, str(body.get("name") or "Client"))
        countries = _countries_from_body(body)
        if countries:
            conn.execute(
                "UPDATE clients SET operating_countries = %s::text[], updated_at = NOW() WHERE id = %s",
                (countries, target),
            )
        patch_map = {
            "name": "name",
            "rfc": "rfc",
            "legalRep": "legal_rep",
            "address": "address",
            "sector": "sector",
            "plan": "plan",
            "tokenLimit": "token_limit",
            "billingContact": "billing_contact",
            "billingEmail": "billing_email",
            "paymentMethod": "payment_method",
            "nextInvoice": "next_invoice",
            "complianceOfficerUserId": "compliance_officer_user_id",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for api_key, col in patch_map.items():
            if api_key not in body:
                continue
            val = body[api_key]
            if api_key == "rfc" and isinstance(val, str):
                val = val.upper()
            sets.append(f"{col} = %s")
            vals.append(val)
        if sets:
            sets.append("updated_at = NOW()")
            vals.append(target)
            conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = %s", vals)
        row = conn.execute(f"{CLIENT_SELECT} WHERE co.client_id = %s", (target,)).fetchone()
        if not row:
            raise SupabaseRestError(404, "Cliente no encontrado.")
        r = dict(row)
        users = _fetch_client_users(conn, target, str(r.get("company_name") or r["name"]))
        usage = _fetch_usage(conn, target)
        conn.commit()
    return {"success": True, "client": _client_api(r, users, usage)}


def clients_suspend(body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    with _conn() as conn:
        _ensure_client_row(conn, target, "Client")
        conn.execute("UPDATE clients SET status = 'suspended', updated_at = NOW() WHERE id = %s", (target,))
        conn.execute(
            "UPDATE nuwa_users SET is_active = false, updated_at = NOW() WHERE client_id = %s",
            (target,),
        )
        conn.commit()
    return {"success": True}


def clients_reactivate(body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    with _conn() as conn:
        conn.execute("UPDATE clients SET status = 'active', updated_at = NOW() WHERE id = %s", (target,))
        conn.execute(
            "UPDATE nuwa_users SET is_active = true, updated_at = NOW() WHERE client_id = %s",
            (target,),
        )
        conn.commit()
    return {"success": True}


def clients_delete(body: dict[str, Any]) -> dict[str, Any]:
    return clients_suspend(body)


def clients_reset_token_usage(body: dict[str, Any]) -> dict[str, Any]:
    target = int(body["targetClientId"])
    with _conn() as conn:
        conn.execute("UPDATE clients SET tokens_used = 0, updated_at = NOW() WHERE id = %s", (target,))
        conn.commit()
    return {"success": True}


def admin_users_list_platform(body: dict[str, Any]) -> dict[str, Any]:
    conditions = ["u.client_id IS NOT NULL"]
    params: list[Any] = []
    role = body.get("role")
    if role and role != "all":
        slug = role
        if role == "master":
            slug = "super_admin"
        elif role in ("analyst", "viewer"):
            slug = "user"
        conditions.append("r.slug = %s")
        params.append(slug)
    # targetClientId is the company filter. Actor clientId is always present and must not narrow the list.
    target_client = body.get("targetClientId")
    if target_client is not None and str(target_client).strip() != "":
        conditions.append("u.client_id = %s")
        params.append(int(target_client))
    status = body.get("status")
    if status == "active":
        conditions.append("u.is_active = true")
    elif status == "disabled":
        conditions.append("u.is_active = false")
    search = (body.get("search") or "").strip()
    if search:
        conditions.append("(u.full_name ILIKE %s OR u.email ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    sql = f"""
    SELECT u.id, u.email, u.full_name, r.slug AS role_slug, u.is_active, u.client_id, co.name AS company_name
    FROM nuwa_users u
    JOIN nuwa_roles r ON r.id = u.role_id
    LEFT JOIN companies co ON co.client_id = u.client_id
    WHERE {' AND '.join(conditions)}
    ORDER BY u.full_name ASC
    LIMIT 500
    """
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    users = [_user_api(dict(r)) for r in rows]
    return {"success": True, "users": users}


def team_users_list(actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Read-only directory of the actor's own company. Any role may call it."""
    actor_client = int(actor["client_id"])
    raw_target = body.get("targetClientId")
    if raw_target is not None and str(raw_target).strip() != "":
        try:
            requested = int(raw_target)
        except (TypeError, ValueError):
            raise SupabaseRestError(400, "targetClientId inválido.")
        if requested != actor_client:
            raise SupabaseRestError(403, "Sin permiso para listar otra empresa.")
    return admin_users_list_platform(
        {
            "targetClientId": actor_client,
            "search": body.get("search"),
            "status": body.get("status"),
            "role": body.get("role"),
        }
    )


def admin_users_invite(body: dict[str, Any]) -> dict[str, Any]:
    email = str(body["email"]).strip().lower()
    name = str(body.get("name") or body.get("fullName") or "").strip()
    role = str(body.get("role") or "analyst")
    # Actor clientId is the master (often 1). The company that receives the user is targetClientId.
    raw_target = body.get("targetClientId")
    if raw_target is not None and str(raw_target).strip() != "":
        client_id = int(raw_target)
    else:
        client_id = int(body["clientId"])
    if "@" not in email or not name:
        raise SupabaseRestError(400, "email y name requeridos.")

    temp_password = _generate_temp_password()
    password_hash = hash_password(temp_password)

    with _conn() as conn:
        role_id = _map_app_role_to_id(conn, role)
        company = conn.execute(
            "SELECT name FROM companies WHERE client_id = %s",
            (client_id,),
        ).fetchone()
        company_name = company["name"] if company else None
        existing = conn.execute(
            "SELECT id FROM nuwa_users WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            (email,),
        ).fetchone()
        if existing:
            raise SupabaseRestError(409, "email_exists")
        row = conn.execute(
            """
            INSERT INTO nuwa_users (
              client_id, email, full_name, role_id, password_hash, is_active, must_change_password
            )
            VALUES (%s, %s, %s, %s, %s, true, true)
            RETURNING id, email, full_name, client_id, is_active
            """,
            (client_id, email, name, role_id, password_hash),
        ).fetchone()
        role_row = conn.execute("SELECT slug FROM nuwa_roles WHERE id = %s", (role_id,)).fetchone()
        conn.commit()

    user = _user_api(
        {**dict(row), "role_slug": role_row["slug"] if role_row else role, "status": "invited"},
        company_name=company_name,
    )
    user["status"] = "invited"
    return {"success": True, "user": user, "tempPassword": temp_password}


def admin_users_reset_password(body: dict[str, Any]) -> dict[str, Any]:
    uid = int(body["targetUserId"])
    temp_password = _generate_temp_password()
    password_hash = hash_password(temp_password)
    with _conn() as conn:
        row = conn.execute(
            """
            UPDATE nuwa_users
            SET password_hash = %s, updated_at = NOW(), is_active = true, must_change_password = true
            WHERE id = %s
            RETURNING id, email, full_name, client_id, is_active, role_id
            """,
            (password_hash, uid),
        ).fetchone()
        if not row:
            raise SupabaseRestError(404, "Usuario no encontrado.")
        role_row = conn.execute("SELECT slug FROM nuwa_roles WHERE id = %s", (row["role_id"],)).fetchone()
        company = conn.execute(
            "SELECT name FROM companies WHERE client_id = %s",
            (row["client_id"],),
        ).fetchone()
        conn.commit()
    user = _user_api({**dict(row), "role_slug": role_row["slug"] if role_row else "user"}, company_name=company["name"] if company else None)
    return {"success": True, "user": user, "tempPassword": temp_password}


def admin_users_resend_invite(body: dict[str, Any]) -> dict[str, Any]:
    return admin_users_reset_password(body)


def admin_users_update_platform(body: dict[str, Any]) -> dict[str, Any]:
    uid = int(body["targetUserId"])
    sets: list[str] = []
    vals: list[Any] = []
    role_raw = str(body["role"]) if "role" in body else None
    raw_target = body.get("targetClientId")
    reassign = int(raw_target) if raw_target is not None and str(raw_target).strip() != "" else None
    if "name" in body:
        sets.append("full_name = %s")
        vals.append(body["name"])
    if "status" in body:
        st = str(body["status"])
        sets.append("is_active = %s")
        vals.append(st in ("active", "invited"))
    if reassign is not None:
        sets.append("client_id = %s")
        vals.append(reassign)
    if not sets and role_raw is None:
        raise SupabaseRestError(400, "Nada que actualizar.")
    with _conn() as conn:
        if role_raw is not None:
            sets.append("role_id = %s")
            vals.append(_map_app_role_to_id(conn, role_raw))
        sets.append("updated_at = NOW()")
        vals.append(uid)
        row = conn.execute(
            f"UPDATE nuwa_users SET {', '.join(sets)} WHERE id = %s RETURNING id, email, full_name, client_id, is_active, role_id",
            vals,
        ).fetchone()
        if not row:
            raise SupabaseRestError(404, "Usuario no encontrado.")
        role_row = conn.execute("SELECT slug FROM nuwa_roles WHERE id = %s", (row["role_id"],)).fetchone()
        company = conn.execute(
            "SELECT name FROM companies WHERE client_id = %s",
            (row["client_id"],),
        ).fetchone()
        conn.commit()
    user = _user_api({**dict(row), "role_slug": role_row["slug"] if role_row else "user"}, company_name=company["name"] if company else None)
    return {"success": True, "user": user}


def _quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise SupabaseRestError(500, "invalid_ident")
    return f'"{name}"'


def _detach_user_foreign_keys(conn: Any, user_id: int, fallback_user_id: int | None) -> None:
    """Null or reassign FKs so DELETE nuwa_users is not blocked by RESTRICT."""
    rows = conn.execute(
        """
        SELECT tc.table_schema, tc.table_name, kcu.column_name, c.is_nullable
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        JOIN information_schema.columns c
          ON c.table_schema = tc.table_schema
         AND c.table_name = tc.table_name
         AND c.column_name = kcu.column_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'nuwa_users'
          AND ccu.column_name = 'id'
          AND tc.table_name <> 'nuwa_users'
        """,
    ).fetchall()
    fallback = fallback_user_id if fallback_user_id and fallback_user_id != user_id else None
    for row in rows:
        table = f"{_quote_ident(row['table_schema'])}.{_quote_ident(row['table_name'])}"
        col = _quote_ident(row["column_name"])
        conn.execute("SAVEPOINT detach_fk")
        try:
            if row["is_nullable"] == "YES":
                conn.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = %s", (user_id,))
            elif fallback is not None:
                conn.execute(
                    f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                    (fallback, user_id),
                )
            conn.execute("RELEASE SAVEPOINT detach_fk")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT detach_fk")


def admin_users_delete_platform(body: dict[str, Any], fallback_user_id: int | None = None) -> dict[str, Any]:
    uid = int(body["targetUserId"])
    with _conn() as conn:
        _detach_user_foreign_keys(conn, uid, fallback_user_id)
        try:
            row = conn.execute(
                "DELETE FROM nuwa_users WHERE id = %s RETURNING id",
                (uid,),
            ).fetchone()
        except psycopg.errors.ForeignKeyViolation as e:
            raise SupabaseRestError(409, "user_has_related_records") from e
        if not row:
            raise SupabaseRestError(404, "Usuario no encontrado.")
        conn.commit()
    return {"success": True, "deleted": True}
