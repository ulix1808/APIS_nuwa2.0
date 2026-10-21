"""
Lambda ops-canary (every 5 min): reads the BFF's GET /api/health/deps and publishes CloudWatch metrics that the
NuwaOpsStack alarms watch (mail to the super admins through SNS). docs/reingenieria-escala.md §6.

Namespace Nuwa/Ops, dimension Env:
  AppUp                        1 when the BFF answered at all (through the ALB), else 0
  DepUp (Env, Dep)             1/0 per dependency the BFF reports (redisState, redisCache, scraper, ocr, mcp, mcp2…)
  ScreeningQueueOldestAgeSec   age of the oldest queued screening batch
  ScreeningQueued / ScreeningRunning / ScreeningWorkers
  SerperBalance / SerperRateLimit  credits left and queries/s of the Serper account (GET /account, no credit used)

Env: OPS_HEALTH_URL (https://app.nuwa.space/v2/api/health/deps), OPS_ENV (prod|dev), MONITORING_WORKER_SECRET,
NUWA_SERPER_SECRET_ARN (optional: without it there are no Serper metrics).
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


def serper_key_from_secret(raw: str) -> str:
    """Same formats the Serper Lambda reads: a JSON object (api_key / SERPER_API_KEY / value) or the bare key."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("api_key") or data.get("SERPER_API_KEY") or data.get("value") or "").strip()
    except json.JSONDecodeError:
        pass
    return raw.strip()


def fetch_serper_account(api_key: str, timeout_s: float = 10) -> dict[str, Any] | None:
    req = urllib.request.Request(
        "https://google.serper.dev/account", headers={"X-API-KEY": api_key, "User-Agent": "nuwa-ops-canary"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"serperAccountError": f"{type(e).__name__}: {e}"}))
        return None


def metrics_from_serper(env: str, account: dict[str, Any] | None) -> list[dict[str, Any]]:
    dim_env = [{"Name": "Env", "Value": env}]
    out: list[dict[str, Any]] = []
    for metric, key in (("SerperBalance", "balance"), ("SerperRateLimit", "rateLimit")):
        value = (account or {}).get(key)
        if isinstance(value, (int, float)):
            out.append({"MetricName": metric, "Dimensions": dim_env, "Value": float(value), "Unit": "Count"})
    return out


def serper_metrics(env: str) -> list[dict[str, Any]]:
    arn = (os.environ.get("NUWA_SERPER_SECRET_ARN") or "").strip()
    if not arn:
        return []
    import boto3

    raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString") or ""
    key = serper_key_from_secret(raw)
    return metrics_from_serper(env, fetch_serper_account(key)) if key else []


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    env = os.environ.get("OPS_ENV", "prod")
    app_up, body = fetch_health(os.environ["OPS_HEALTH_URL"], os.environ.get("MONITORING_WORKER_SECRET", ""))
    metrics = metrics_from_health(env, app_up, body)
    try:
        metrics += serper_metrics(env)
    except Exception as e:  # noqa: BLE001 — Serper metrics never break the health metrics
        print(json.dumps({"serperMetricsError": f"{type(e).__name__}: {e}"}))
    import boto3  # lazy: tests run without it

    cw = boto3.client("cloudwatch")
    for i in range(0, len(metrics), 20):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metrics[i : i + 20])
    down = [m["Dimensions"][-1]["Value"] for m in metrics if m["MetricName"] == "DepUp" and m["Value"] == 0]
    serper = {m["MetricName"]: m["Value"] for m in metrics if m["MetricName"].startswith("Serper")}
    print(json.dumps({"appUp": app_up, "down": down, "error": body.get("error"), "metrics": len(metrics), **serper}))
    return {"appUp": app_up, "down": down}
