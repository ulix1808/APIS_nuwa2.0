"""Unit tests — JWT access token TTL default (72h)."""

from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest import mock


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_nuwa_jwt():
    for name in (
        "jwt",
        "cryptography",
        "cryptography.fernet",
        "cryptography.hazmat",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.asymmetric",
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
        "cryptography.exceptions",
    ):
        if name not in sys.modules:
            _install_stub(name)
    _install_stub(
        "nuwa_app_crypto",
        AppCryptoConfigError=Exception,
        get_app_crypto_config=lambda: {"jwt_signing_secret": "x" * 40},
    )
    _install_stub("nuwa_obs_log", log_phase=lambda *a, **k: None)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))
    sys.modules.pop("nuwa_jwt", None)
    import nuwa_jwt

    return nuwa_jwt


def test_ttl_default_is_72h() -> None:
    nj = _load_nuwa_jwt()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NUWA_JWT_TTL_SECONDS", None)
        assert nj._ttl_seconds() == 259200


def test_ttl_env_override() -> None:
    nj = _load_nuwa_jwt()
    with mock.patch.dict(os.environ, {"NUWA_JWT_TTL_SECONDS": "3600"}):
        assert nj._ttl_seconds() == 3600


def test_ttl_env_floor_300() -> None:
    nj = _load_nuwa_jwt()
    with mock.patch.dict(os.environ, {"NUWA_JWT_TTL_SECONDS": "60"}):
        assert nj._ttl_seconds() == 300


def test_ttl_invalid_env_falls_back_72h() -> None:
    nj = _load_nuwa_jwt()
    with mock.patch.dict(os.environ, {"NUWA_JWT_TTL_SECONDS": "nope"}):
        assert nj._ttl_seconds() == 259200
