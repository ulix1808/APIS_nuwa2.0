"""
Lambda ops-canary (every 5 min): reads the BFF's GET /api/health/deps and publishes CloudWatch metrics that the
NuwaOpsStack alarms watch (mail to the super admins through SNS). docs/reingenieria-escala.md §6.

Namespace Nuwa/Ops, dimension Env:
  AppUp                        1 when the BFF answered at all (through the ALB), else 0
  DepUp (Env, Dep)             1/0 per dependency the BFF reports (redisState, redisCache, scraper, ocr, mcp, mcp2…)
  ScreeningQueueOldestAgeSec   age of the oldest queued screening batch
  ScreeningQueued / ScreeningRunning / ScreeningWorkers

Env: OPS_HEALTH_URL (https://app.nuwa.space/v2/api/health/deps), OPS_ENV (prod|dev), MONITORING_WORKER_SECRET.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

NAMESPACE = "Nuwa/Ops"


def fetch_health(url: str, secret: str, timeout_s: float = 20) -> tuple[bool, dict[str, Any]]:
    """(app answered, body). A 503 still means the app is up: it is reporting a dependency down."""
    req = urllib.request.Request(url, headers={"x-monitoring-worker-secret": secret, "User-Agent": "nuwa-ops-canary"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return True, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 503:
            try:
                return True, json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                return True, {}
        return False, {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"{type(e).__name__}: {e}"}


def metrics_from_health(env: str, app_up: bool, body: dict[str, Any]) -> list[dict[str, Any]]:
    dim_env = [{"Name": "Env", "Value": env}]
    out: list[dict[str, Any]] = [{"MetricName": "AppUp", "Dimensions": dim_env, "Value": 1 if app_up else 0, "Unit": "Count"}]
    if not app_up:
        return out
    for name, dep in sorted((body.get("deps") or {}).items()):
        out.append(
            {
                "MetricName": "DepUp",
                "Dimensions": dim_env + [{"Name": "Dep", "Value": str(name)}],
                "Value": 1 if (dep or {}).get("ok") else 0,
                "Unit": "Count",
            }
        )
    queue = body.get("queue") or {}
    workers = body.get("workers") or {}
    for metric, value, unit in (
        ("ScreeningQueueOldestAgeSec", queue.get("oldestQueuedAgeSec"), "Seconds"),
        ("ScreeningQueued", queue.get("screeningQueued"), "Count"),
        ("ScreeningRunning", queue.get("screeningRunning"), "Count"),
        ("ScreeningWorkers", workers.get("screening"), "Count"),
    ):
        if isinstance(value, (int, float)):
            out.append({"MetricName": metric, "Dimensions": dim_env, "Value": float(value), "Unit": unit})
    return out


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    env = os.environ.get("OPS_ENV", "prod")
    app_up, body = fetch_health(os.environ["OPS_HEALTH_URL"], os.environ.get("MONITORING_WORKER_SECRET", ""))
    metrics = metrics_from_health(env, app_up, body)
    import boto3  # lazy: tests run without it

    cw = boto3.client("cloudwatch")
    for i in range(0, len(metrics), 20):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metrics[i : i + 20])
    down = [m["Dimensions"][-1]["Value"] for m in metrics if m["MetricName"] == "DepUp" and m["Value"] == 0]
    print(json.dumps({"appUp": app_up, "down": down, "error": body.get("error"), "metrics": len(metrics)}))
    return {"appUp": app_up, "down": down}
