"""
Configuración en runtime (sin secretos en código).

**PostgreSQL (RDS u otro):**
- NUWA_DATABASE_SECRET_ARN: ARN de Secrets Manager con JSON
  {"host","port","dbname","user","password","sslmode"?}.
- Desarrollo local (sin AWS): NUWA_DATABASE_CONFIG_JSON con el mismo JSON (tiene prioridad sobre el ARN).

**Supabase (PostgREST)** — si no hay NUWA_DATABASE_SECRET_ARN:
- SUPABASE_URL_PARAMETER + SUPABASE_SECRET_ARN (JWT service_role).
- Opcional: SUPABASE_URL en env.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from nuwa_obs_log import log_await, log_done, log_phase


class SupabaseConfigError(Exception):
    pass


class DatabaseConfigError(Exception):
    pass


def is_database_mode() -> bool:
    return bool(os.environ.get("NUWA_DATABASE_SECRET_ARN", "").strip()) or bool(
        os.environ.get("NUWA_DATABASE_CONFIG_JSON", "").strip()
    )


def _coerce_database_config(data: dict[str, Any]) -> dict[str, Any]:
    host = str(data.get("host") or "").strip()
    user = str(data.get("user") or data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    dbname = str(data.get("dbname") or data.get("database") or "").strip()
    if not host or not user or not password or not dbname:
        raise DatabaseConfigError(
            "La config DB requiere host, user, password y dbname (o database)."
        )
    port = int(data.get("port") or 5432)
    sslmode = str(data.get("sslmode") or "require").strip()
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "sslmode": sslmode,
    }


@lru_cache(maxsize=1)
def get_database_config() -> dict[str, Any]:
    local = os.environ.get("NUWA_DATABASE_CONFIG_JSON", "").strip()
    if local:
        log_phase("database_config", "source=NUWA_DATABASE_CONFIG_JSON")
        try:
            data = json.loads(local)
        except json.JSONDecodeError as e:
            raise DatabaseConfigError(f"NUWA_DATABASE_CONFIG_JSON no es JSON válido: {e}") from e
        if not isinstance(data, dict) or not data:
            raise DatabaseConfigError("NUWA_DATABASE_CONFIG_JSON debe ser un objeto JSON.")
        cfg = _coerce_database_config(data)
        log_phase("database_config", f"ok host={cfg['host']} port={cfg['port']} db={cfg['dbname']}")
        return cfg

    arn = os.environ.get("NUWA_DATABASE_SECRET_ARN", "").strip()
    if not arn:
        raise DatabaseConfigError("Falta NUWA_DATABASE_SECRET_ARN o NUWA_DATABASE_CONFIG_JSON.")
    import boto3

    log_await("secretsmanager", "GetSecretValue", arn)
    sm = boto3.client("secretsmanager")
    sec = sm.get_secret_value(SecretId=arn)
    log_done("secretsmanager", "GetSecretValue", "database secret")
    raw = (sec.get("SecretString") or "").strip()
    if not raw:
        raise DatabaseConfigError("El secreto de base de datos está vacío.")
    try:
        data = json.loads(raw) if raw.startswith("{") else {}
    except json.JSONDecodeError as e:
        raise DatabaseConfigError(f"Secreto DB no es JSON válido: {e}") from e
    if not data:
        raise DatabaseConfigError("El secreto DB debe ser un objeto JSON.")
    cfg = _coerce_database_config(data)
    log_phase("database_config", f"ok host={cfg['host']} port={cfg['port']} db={cfg['dbname']}")
    return cfg


def ensure_data_backend() -> None:
    """Comprueba que haya configuración de Postgres (ARN) o de Supabase."""
    if is_database_mode():
        log_phase("ensure_data_backend", "database_mode")
        get_database_config()
    else:
        log_phase("ensure_data_backend", "supabase_mode")
        get_supabase_config()
    log_phase("ensure_data_backend", "ok")


@lru_cache(maxsize=1)
def get_supabase_config() -> dict[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip()
    param_name = os.environ.get("SUPABASE_URL_PARAMETER", "").strip()
    secret_arn = os.environ.get("SUPABASE_SECRET_ARN", "").strip()

    if not url and not param_name:
        raise SupabaseConfigError(
            "Falta SUPABASE_URL o SUPABASE_URL_PARAMETER en la Lambda."
        )
    if not secret_arn:
        raise SupabaseConfigError("Falta SUPABASE_SECRET_ARN en la Lambda.")

    if not url:
        import boto3

        log_await("ssm", "GetParameter", param_name)
        ssm = boto3.client("ssm")
        resp = ssm.get_parameter(Name=param_name)
        log_done("ssm", "GetParameter", param_name)
        url = resp["Parameter"]["Value"].strip()

    import boto3

    log_await("secretsmanager", "GetSecretValue", secret_arn)
    sm = boto3.client("secretsmanager")
    sec = sm.get_secret_value(SecretId=secret_arn)
    log_done("secretsmanager", "GetSecretValue", "supabase secret")
    raw = sec.get("SecretString") or ""
    raw = raw.strip()
    if not raw:
        raise SupabaseConfigError(
            "El secreto de Supabase está vacío. Configura el valor en AWS Secrets Manager."
        )
    # Permite guardar JSON {"key":"..."} o el JWT como string plano
    if raw.startswith("{"):
        try:
            data: dict[str, Any] = json.loads(raw)
            key = str(data.get("service_role_key") or data.get("key") or "").strip()
        except json.JSONDecodeError:
            key = raw
    else:
        key = raw

    if not key:
        raise SupabaseConfigError("No se pudo leer service_role_key del secreto.")

    log_phase("supabase_config", f"ok url_netloc={urlparse(url).netloc or url[:80]}")
    return {"url": url.rstrip("/"), "service_role_key": key}


def clear_config_cache() -> None:
    get_supabase_config.cache_clear()
    get_database_config.cache_clear()
