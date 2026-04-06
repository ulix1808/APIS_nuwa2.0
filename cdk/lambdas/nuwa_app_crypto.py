"""Secreto de aplicación (JWT + Fernet) desde Secrets Manager JSON o env local."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from nuwa_obs_log import log_await, log_done, log_phase


class AppCryptoConfigError(Exception):
    pass


def _coerce_app_crypto_data(data: dict[str, Any]) -> dict[str, Any]:
    jwt_secret = str(data.get("jwt_signing_secret") or data.get("jwt_secret") or "").strip()
    fernet_key = data.get("fernet_key")
    fk = str(fernet_key).strip() if fernet_key is not None else ""
    if len(jwt_secret) < 32:
        raise AppCryptoConfigError("jwt_signing_secret debe tener al menos 32 caracteres.")
    if not fk:
        raise AppCryptoConfigError("fernet_key es requerido (Fernet URL-safe base64).")
    try:
        Fernet(fk.encode("ascii"))
    except Exception as e:
        raise AppCryptoConfigError(f"fernet_key inválida: {e}") from e
    return {"jwt_signing_secret": jwt_secret, "fernet_key": fk}


@lru_cache(maxsize=1)
def get_app_crypto_config() -> dict[str, Any]:
    local = os.environ.get("NUWA_APP_CRYPTO_CONFIG_JSON", "").strip()
    if local:
        log_phase("app_crypto_config", "source=NUWA_APP_CRYPTO_CONFIG_JSON")
        try:
            data: dict[str, Any] = json.loads(local)
        except json.JSONDecodeError as e:
            raise AppCryptoConfigError(f"NUWA_APP_CRYPTO_CONFIG_JSON no es JSON válido: {e}") from e
        if not isinstance(data, dict) or not data:
            raise AppCryptoConfigError("NUWA_APP_CRYPTO_CONFIG_JSON debe ser un objeto JSON.")
        out = _coerce_app_crypto_data(data)
        log_phase("app_crypto_config", "ok (local)")
        return out

    arn = os.environ.get("NUWA_APP_CRYPTO_SECRET_ARN", "").strip()
    if not arn:
        raise AppCryptoConfigError(
            "Falta NUWA_APP_CRYPTO_SECRET_ARN o NUWA_APP_CRYPTO_CONFIG_JSON."
        )
    import boto3

    log_await("secretsmanager", "GetSecretValue", arn)
    sm = boto3.client("secretsmanager")
    sec = sm.get_secret_value(SecretId=arn)
    log_done("secretsmanager", "GetSecretValue", "app-crypto")
    raw = (sec.get("SecretString") or "").strip()
    if not raw:
        raise AppCryptoConfigError("El secreto app-crypto está vacío.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AppCryptoConfigError(f"app-crypto no es JSON válido: {e}") from e
    out = _coerce_app_crypto_data(data)
    log_phase("app_crypto_config", "ok (secretsmanager)")
    return out


def encrypt_apigw_secret(plain: str) -> str:
    if not plain:
        return ""
    fk = get_app_crypto_config()["fernet_key"]
    token = Fernet(fk.encode("ascii")).encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def decrypt_apigw_secret(stored: str) -> str:
    """Descifra valor Fernet; si falla, devuelve el string tal cual (legado texto plano)."""
    if not stored:
        return ""
    fk = get_app_crypto_config()["fernet_key"]
    try:
        return Fernet(fk.encode("ascii")).decrypt(stored.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return stored
