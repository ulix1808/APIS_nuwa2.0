"""Unit tests for monitoring-due-tick Lambda (mocked HTTP)."""

from __future__ import annotations

import json
import os
from unittest import mock

import handler_monitoring_tick as tick


def test_tick_empty_due_u_l01() -> None:
    def fake_post(url, body, headers, timeout=20.0):
        if url.endswith("/due"):
            return 200, {"items": [], "total": 0}
        raise AssertionError(f"unexpected url {url}")

    with mock.patch.dict(
        os.environ,
        {
            "NUWA_API_BASE": "https://api.example/prod",
            "MONITORING_BFF_URL": "https://app.example/v2",
            "MONITORING_WORKER_SECRET": "s3cret",
            "MONITORING_DUE_LIMIT": "10",
        },
    ):
        with mock.patch.object(tick, "_post_json", side_effect=fake_post):
            out = tick.handler({}, None)
    assert out["due_count"] == 0
    assert out["enqueued"] == 0
    assert out["failed"] == 0


def test_tick_enqueues_with_secret_u_l02() -> None:
    calls: list[tuple[str, dict, dict]] = []

    def fake_post(url, body, headers, timeout=20.0):
        calls.append((url, body, headers))
        if url.endswith("/due"):
            return 200, {
                "items": [
                    {"clientId": 1, "entityId": "e1", "monitoringId": "m1"},
                    {"clientId": 2, "entityId": "e2", "monitoringId": "m2"},
                ]
            }
        return 202, {"success": True, "jobId": "j1"}

    with mock.patch.dict(
        os.environ,
        {
            "NUWA_API_BASE": "https://api.example/prod",
            "MONITORING_BFF_URL": "https://app.example/v2",
            "MONITORING_WORKER_SECRET": "s3cret",
        },
    ):
        with mock.patch.object(tick, "_post_json", side_effect=fake_post):
            out = tick.handler({}, None)
    assert out["due_count"] == 2
    assert out["enqueued"] == 2
    assert out["failed"] == 0
    enqueue_calls = [c for c in calls if "enqueue-rescreen" in c[0]]
    assert len(enqueue_calls) == 2
    assert enqueue_calls[0][2]["x-monitoring-worker-secret"] == "s3cret"
    assert enqueue_calls[0][1]["clientId"] == 1
    assert enqueue_calls[1][1]["clientId"] == 2


def test_tick_continues_on_bff_5xx_u_l03() -> None:
    n = {"i": 0}

    def fake_post(url, body, headers, timeout=20.0):
        if url.endswith("/due"):
            return 200, {
                "items": [
                    {"clientId": 1, "entityId": "e1", "monitoringId": "m1"},
                    {"clientId": 1, "entityId": "e2", "monitoringId": "m2"},
                ]
            }
        n["i"] += 1
        if n["i"] == 1:
            return 500, {"message": "boom"}
        return 202, {"ok": True}

    with mock.patch.dict(
        os.environ,
        {
            "NUWA_API_BASE": "https://api.example/prod",
            "MONITORING_BFF_URL": "https://app.example/v2",
            "MONITORING_WORKER_SECRET": "s3cret",
        },
    ):
        with mock.patch.object(tick, "_post_json", side_effect=fake_post):
            out = tick.handler({}, None)
    assert out["enqueued"] == 1
    assert out["failed"] == 1


def test_tick_due_401_u_l04() -> None:
    def fake_post(url, body, headers, timeout=20.0):
        return 401, {"message": "no"}

    with mock.patch.dict(
        os.environ,
        {
            "NUWA_API_BASE": "https://api.example/prod",
            "MONITORING_BFF_URL": "https://app.example/v2",
            "MONITORING_WORKER_SECRET": "s3cret",
        },
    ):
        with mock.patch.object(tick, "_post_json", side_effect=fake_post):
            out = tick.handler({}, None)
    assert out["ok"] is False
    assert out["error"] == "due_failed"
