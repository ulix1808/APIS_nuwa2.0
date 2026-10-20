"""Ops canary (cdk/lambdas_ops): BFF /api/health/deps → CloudWatch metrics."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cdk" / "lambdas_ops"))

import handler_ops_canary as canary  # noqa: E402


def _value(metrics, name, dep=None):
    for m in metrics:
        if m["MetricName"] == name and (dep is None or m["Dimensions"][-1]["Value"] == dep):
            return m["Value"]
    return None


def test_app_down_publishes_only_app_up_zero():
    metrics = canary.metrics_from_health("prod", False, {"error": "timeout"})
    assert metrics == [{"MetricName": "AppUp", "Dimensions": [{"Name": "Env", "Value": "prod"}], "Value": 0, "Unit": "Count"}]


def test_each_dependency_queue_and_workers():
    body = {
        "deps": {"redisState": {"ok": True}, "scraper": {"ok": False, "detail": "HTTP 500"}, "mcp2": {"ok": True}},
        "queue": {"screeningQueued": 3, "screeningRunning": 1, "oldestQueuedAgeSec": 720},
        "workers": {"screening": 1},
    }
    m = canary.metrics_from_health("dev", True, body)
    assert _value(m, "AppUp") == 1
    assert _value(m, "DepUp", "scraper") == 0 and _value(m, "DepUp", "redisState") == 1 and _value(m, "DepUp", "mcp2") == 1
    assert _value(m, "ScreeningQueueOldestAgeSec") == 720.0 and _value(m, "ScreeningWorkers") == 1.0
    assert all(x["Dimensions"][0] == {"Name": "Env", "Value": "dev"} for x in m)


def test_a_503_still_means_the_app_answered(monkeypatch):
    import io
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b'{"deps":{"ocr":{"ok":false}}}'))

    monkeypatch.setattr(canary.urllib.request, "urlopen", boom)
    up, body = canary.fetch_health("https://app.nuwa.space/v2/api/health/deps", "s")
    assert up is True and body["deps"]["ocr"]["ok"] is False
