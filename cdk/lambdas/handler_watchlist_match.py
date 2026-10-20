"""
Lambda watchlist-match: daily early-warning match (EventBridge, 06:00 CDMX = 12:00 UTC).

Runs watchlist_match.run_match, records the run, mails each client once (SES, to all its active users) with
all its new alerts, and posts the alerts to the BFF only for the bell (the bell lives in the BFF's Redis).
The alerts are already in entity_alerts, so Alertas shows them even if the BFF is down.

Env WATCHLIST_CLIENT_IDS="8,12" limits the run to those clients (the dev stack: the QA client only).
Event: {"full": true} rematches everything; {"clientIds": [8]}; {"notify": false} skips the mails;
{"trigger": "manual"} labels the run.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

import watchlist_db as db
import watchlist_mail as mail
import watchlist_match as wm
from nuwa_obs_log import log_phase


def _client_ids(event: dict[str, Any]) -> list[int] | None:
    env_ids = [int(x) for x in (os.environ.get("WATCHLIST_CLIENT_IDS") or "").replace(" ", "").split(",") if x.isdigit()]
    asked = [int(x) for x in event.get("clientIds") or [] if str(x).isdigit()]
    if env_ids and asked:
        return [c for c in asked if c in env_ids] or env_ids
    return asked or env_ids or None


def _post_bff(payload: dict[str, Any]) -> int | None:
    base = (os.environ.get("MONITORING_BFF_URL") or "").rstrip("/")
    secret = os.environ.get("MONITORING_WORKER_SECRET") or ""
    if not base or not secret:
        log_phase("watchlist_match", "notify skipped: no BFF url/secret")
        return None
    req = urllib.request.Request(
        f"{base}/api/internal/watchlist-alert",
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "x-monitoring-worker-secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except Exception as e:  # noqa: BLE001
        log_phase("watchlist_match", f"notify failed: {e}")
        return None


def run(event: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    conn = db.connect(autocommit=True)
    run_id = db.start_run(conn, "match", None, str(event.get("trigger") or "schedule"))
    try:
        client_ids = _client_ids(event)
        since = None if event.get("full") else wm.last_match_since(conn)
        out = wm.run_match(conn, run_id, client_ids=client_ids, since=since, full=bool(event.get("full")))
        stats = out["stats"]
        stats["clientFilter"] = client_ids
        mails: dict[str, Any] = {}
        bells: dict[str, Any] = {}
        if event.get("notify", True):
            for client_id, alerts in wm.group_by_client(out["alerts"]).items():
                recipients = db.client_user_emails(conn, client_id)
                if not recipients:
                    mails[str(client_id)] = "no_recipients"
                else:
                    sent = mail.mail_matches(recipients, alerts)
                    mails[str(client_id)] = len(recipients) if sent else "failed"
                # The BFF only pushes the bell; the mail is already sent ("mailed").
                bells[str(client_id)] = _post_bff(
                    {"event": "matches", "clientId": client_id, "runId": run_id, "mailed": True, "alerts": alerts}
                )
        stats["mails"] = mails
        stats["bells"] = bells
        stats["seconds"] = round(time.time() - started, 1)
        db.finish_run(conn, run_id, "ok", counts=stats)
        log_phase("watchlist_match", f"run {run_id} ok {stats}")
        return {"ok": True, "runId": run_id, "status": "ok", "counts": stats, "alerts": out["alerts"]}
    except Exception as e:  # noqa: BLE001
        message = f"{type(e).__name__}: {e}"
        log_phase("watchlist_match", f"run {run_id} failed: {message}")
        recipients: list[str] = []
        try:
            db.finish_run(conn, run_id, "failed", error=message)
            recipients = db.super_admin_emails(conn)
        except Exception as e2:  # noqa: BLE001
            log_phase("watchlist_match", f"run {run_id} failure bookkeeping: {e2}")
        mail.mail_failure("match_failed", message, recipients)
        return {"ok": False, "runId": run_id, "status": "failed", "error": message}
    finally:
        conn.close()


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    return run(event or {})
