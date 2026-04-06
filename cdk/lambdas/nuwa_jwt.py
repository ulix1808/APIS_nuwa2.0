"""Access tokens JWT (HS256) para llamadas API tras login."""

from __future__ import annotations

import os
import time
from typing import Any

import jwt

from nuwa_app_crypto import AppCryptoConfigError, get_app_crypto_config
from nuwa_obs_log import log_phase

_ISS = "nuwa2"
_AUD = "nuwa2-api"
_ALG = "HS256"


def _ttl_seconds() -> int:
    try:
        return max(300, int(os.environ.get("NUWA_JWT_TTL_SECONDS", "28800")))
    except (TypeError, ValueError):
        return 28800


def mint_access_token(
    *,
    user_id: int,
    client_id: int,
    role_slug: str,
    email: str | None = None,
) -> tuple[str, int]:
    """Devuelve (token, expires_in_segundos)."""
    log_phase("jwt_mint", "loading app_crypto for signing secret")
    secret = get_app_crypto_config()["jwt_signing_secret"]
    log_phase("jwt_mint", "signing")
    now = int(time.time())
    exp = now + _ttl_seconds()
    # PyJWT ≥2 valida verify_sub por defecto: "sub" debe ser str (RFC 7519); si es int, decode falla.
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "cid": client_id,
        "role": role_slug,
        "iat": now,
        "exp": exp,
        "iss": _ISS,
        "aud": _AUD,
    }
    if email:
        payload["email"] = email
    token = jwt.encode(payload, secret, algorithm=_ALG)
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token, exp - now


def verify_access_token(token: str) -> dict[str, Any] | None:
    try:
        secret = get_app_crypto_config()["jwt_signing_secret"]
    except AppCryptoConfigError:
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALG],
            audience=_AUD,
            issuer=_ISS,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None
    if "cid" not in payload or "role" not in payload:
        return None
    return payload


def _authorization_header_from_event(event: dict[str, Any]) -> str:
    """Authorization: … tal como llega desde API Gateway (headers o multiValueHeaders, cualquier casing)."""
    h = event.get("headers")
    if isinstance(h, dict):
        for key, val in h.items():
            if key and str(key).lower() == "authorization" and isinstance(val, str) and val.strip():
                return val.strip()
    mvh = event.get("multiValueHeaders")
    if isinstance(mvh, dict):
        for key, vals in mvh.items():
            if not key or not isinstance(vals, list) or not vals:
                continue
            if str(key).lower() != "authorization":
                continue
            v0 = vals[0]
            if isinstance(v0, str) and v0.strip():
                return v0.strip()
    return ""


def authorization_header_value(event: dict[str, Any]) -> str:
    """Valor crudo del header Authorization (o cadena vacía)."""
    return _authorization_header_from_event(event)


def jwt_claims_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    auth = _authorization_header_from_event(event)
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:].strip()
    if not raw:
        return None
    return verify_access_token(raw)


def jwt_int(claims: dict[str, Any], key: str) -> int:
    v = claims.get(key)
    if v is None:
        raise ValueError(key)
    return int(v)
