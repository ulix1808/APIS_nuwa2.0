"""Unit tests — nuwa_monitoring_worker shared auth."""

from __future__ import annotations

import os
from unittest import mock

from nuwa_monitoring_worker import monitoring_worker_claims, monitoring_worker_secret_ok


def test_monitoring_worker_claims_shape() -> None:
    claims = monitoring_worker_claims()
    assert claims["role"] == "monitoring_worker"
    assert claims["worker"] is True


@mock.patch.dict(os.environ, {"MONITORING_WORKER_SECRET": "s3cret"})
def test_monitoring_worker_secret_ok_header() -> None:
    event = {"headers": {"x-monitoring-worker-secret": "s3cret"}}
    assert monitoring_worker_secret_ok(event) is True


@mock.patch.dict(os.environ, {"MONITORING_WORKER_SECRET": "s3cret"})
def test_monitoring_worker_secret_rejects_jwt_bearer() -> None:
    event = {"headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"}}
    assert monitoring_worker_secret_ok(event) is False


@mock.patch.dict(os.environ, {}, clear=True)
def test_monitoring_worker_secret_missing_env() -> None:
    event = {"headers": {"x-monitoring-worker-secret": "anything"}}
    assert monitoring_worker_secret_ok(event) is False
