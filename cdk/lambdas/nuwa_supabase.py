"""Cliente mínimo PostgREST (Supabase) con urllib — sin pip extra."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from nuwa_config import get_supabase_config, is_database_mode
from nuwa_errors import SupabaseRestError
from nuwa_obs_log import log_await, log_done


def _headers() -> dict[str, str]:
    cfg = get_supabase_config()
    key = cfg["service_role_key"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def rest_request(
    method: str,
    path: str,
    *,
    query: str | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> tuple[int, str]:
    cfg = get_supabase_config()
    base = cfg["url"].rstrip("/")
    url = f"{base}/rest/v1/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method.upper())
    try:
        log_await("https", "PostgREST", f"{method.upper()} {url[:200]}")
        with urllib.request.urlopen(req, timeout=25) as resp:
            code = resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
        log_done("https", "PostgREST", f"status={code}")
        return code, text
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise SupabaseRestError(e.code, raw) from e


def rest_json(
    method: str,
    path: str,
    *,
    query: str | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> Any:
    if is_database_mode():
        from nuwa_pg_dispatch import rest_json_pg

        return rest_json_pg(method, path, query=query, body=body)
    _status, text = rest_request(method, path, query=query, body=body)
    if not text.strip():
        return None
    return json.loads(text)


def fetch_user_with_role(*, user_id: int) -> dict[str, Any] | None:
    if is_database_mode():
        from nuwa_pg_dispatch import fetch_user_with_role_pg

        return fetch_user_with_role_pg(user_id=user_id)
    users = rest_json(
        "GET",
        "nuwa_users",
        query=f"id=eq.{user_id}&select=id,client_id,email,full_name,role_id,is_active",
    )
    if not users:
        return None
    u = users[0] if isinstance(users, list) else users
    if not u.get("is_active", True):
        return None
    roles = rest_json("GET", "nuwa_roles", query=f"id=eq.{u['role_id']}&select=slug,name")
    if not roles:
        return None
    r = roles[0] if isinstance(roles, list) else roles
    return {
        "id": int(u["id"]),
        "client_id": int(u["client_id"]),
        "email": u["email"],
        "full_name": u.get("full_name", ""),
        "role_slug": r["slug"],
    }


def invoke_search_risk_entities(
    *,
    client_id: int,
    query: str = "",
    rfc: str | None = None,
    entity_types: list[str] | None = None,
    risk_levels: list[int] | None = None,
    limit: int = 20,
    word_similarity_threshold: float = 0.38,
) -> list[dict[str, Any]]:
    if is_database_mode():
        from nuwa_pg_dispatch import search_risk_entities_pg

        return search_risk_entities_pg(
            client_id=client_id,
            query=query,
            rfc=rfc,
            entity_types=entity_types,
            risk_levels=risk_levels,
            limit=limit,
            word_similarity_threshold=word_similarity_threshold,
        )
    et = entity_types if entity_types else None
    rl = risk_levels if risk_levels else None
    payload: dict[str, Any] = {
        "p_client_id": client_id,
        "p_query": query or "",
        "p_rfc": rfc,
        "p_entity_types": et,
        "p_risk_levels": rl,
        "p_limit": limit,
        "p_word_similarity_threshold": word_similarity_threshold,
    }
    _status, text = rest_request("POST", "rpc/search_risk_entities", body=payload)
    if not text.strip():
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]
