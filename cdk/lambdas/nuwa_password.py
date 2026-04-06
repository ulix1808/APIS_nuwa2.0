"""Hash / verificación de contraseñas (sin dependencias nativas extra)."""

from __future__ import annotations

import hashlib
import hmac


def verify_password(plain: str, stored: str) -> bool:
    if not plain or not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        parts = stored.split("$", 2)
        if len(parts) != 3:
            return False
        _, salt, hexdigest = parts
        try:
            dk = hashlib.pbkdf2_hmac(
                "sha256", plain.encode("utf-8"), salt.encode("utf-8"), 100000
            )
        except Exception:
            return False
        return hmac.compare_digest(dk.hex(), hexdigest)
    return False


def hash_password(plain: str) -> str:
    import secrets

    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"
