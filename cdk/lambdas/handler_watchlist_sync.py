"""
Lambda watchlist-sync: daily refresh of our OFAC copy (EventBridge, 22:00 CDMX).

1. Downloads SDN_ADVANCED.XML and CONS_ADVANCED.XML from the OFAC Sanctions List Service.
2. If both files are byte-identical to the last good sync (sha256), records an 'unchanged' run and stops:
   OFAC does not publish every day.
3. Parses both files, merges them by FixedRef and applies the delta in one transaction (watchlist_db).
4. A failure is recorded in watchlist_runs and mailed to the super admins through the BFF; the last good
   version of the list stays in place.

Event: {"force": true} reapplies even if the files did not change; {"trigger": "manual"} labels the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any

import watchlist_db as db
import watchlist_ofac as ofac
from nuwa_obs_log import log_phase

LIST_CODE = "ofac"
DOWNLOAD_TIMEOUT_S = 120
TMP_DIR = "/tmp"


def _download(name: str) -> dict[str, Any]:
    url = f"{os.environ.get('WATCHLIST_OFAC_BASE_URL', ofac.OFAC_EXPORTS_BASE).rstrip('/')}/{name}"
    path = os.path.join(TMP_DIR, name)
    digest = hashlib.sha256()
    size = 0
    req = urllib.request.Request(url, headers={"User-Agent": "Nuwa-Watchlist/1.0"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp, open(path, "wb") as out:
        last_modified = resp.headers.get("Last-Modified") or ""
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    if size < 1024:
        raise RuntimeError(f"{name}: descarga vacía o incompleta ({size} bytes)")
    return {"path": path, "sha256": digest.hexdigest(), "bytes": size, "lastModified": last_modified}


def _notify_failure(message: str, recipients: list[str]) -> None:
    base = (os.environ.get("MONITORING_BFF_URL") or "").rstrip("/")
    secret = os.environ.get("MONITORING_WORKER_SECRET") or ""
    if not base or not secret:
        log_phase("watchlist_sync", "notify skipped: no BFF url/secret")
        return
    body = json.dumps(
        {"event": "sync_failed", "listCode": LIST_CODE, "message": message[:1500], "recipients": recipients}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/internal/watchlist-alert",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-monitoring-worker-secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log_phase("watchlist_sync", f"notify -> {r.status}")
    except Exception as e:  # noqa: BLE001
        log_phase("watchlist_sync", f"notify failed: {e}")


def run_sync(*, force: bool = False, trigger: str = "schedule") -> dict[str, Any]:
    started = time.time()
    conn = db.connect(autocommit=True)
    run_id = db.start_run(conn, "sync", LIST_CODE, trigger)
    files: dict[str, Any] = {}
    try:
        downloads = {name: _download(name) for name in ofac.OFAC_FILES}
        files = {n: {k: v for k, v in d.items() if k != "path"} for n, d in downloads.items()}
        last = db.last_good_sync(conn, LIST_CODE)
        prev_files = (last or {}).get("files") or {}
        same = all(prev_files.get(n, {}).get("sha256") == f["sha256"] for n, f in files.items())
        if same and not force:
            db.finish_run(
                conn, run_id, "unchanged", files=files, published_on=_iso((last or {}).get("published_on")),
                counts={"seconds": round(time.time() - started, 1)},
            )
            log_phase("watchlist_sync", f"run {run_id} unchanged")
            return {"ok": True, "runId": run_id, "status": "unchanged"}

        metas: dict[str, Any] = {}
        parsed: list[list[dict[str, Any]]] = []
        for name, d in downloads.items():
            meta, records = ofac.parse_advanced_xml(d["path"])
            metas[name] = meta
            files[name]["dateOfIssue"] = meta.get("dateOfIssue")
            files[name]["parties"] = meta.get("parties")
            parsed.append(records)
        records = ofac.merge_records(*parsed)
        counts = db.apply_sync(conn, LIST_CODE, records, run_id)
        counts["seconds"] = round(time.time() - started, 1)
        published = max((m.get("dateOfIssue") or "" for m in metas.values()), default="") or None
        db.finish_run(conn, run_id, "ok", counts=counts, files=files, published_on=published)
        log_phase("watchlist_sync", f"run {run_id} ok {counts}")
        return {"ok": True, "runId": run_id, "status": "ok", "counts": counts, "publishedOn": published}
    except Exception as e:  # noqa: BLE001
        message = f"{type(e).__name__}: {e}"
        log_phase("watchlist_sync", f"run {run_id} failed: {message}")
        recipients: list[str] = []
        try:
            db.finish_run(conn, run_id, "failed", files=files, error=message)
            recipients = db.super_admin_emails(conn)
        except Exception as e2:  # noqa: BLE001 — the run row and the mail must not fail on each other
            log_phase("watchlist_sync", f"run {run_id} failure bookkeeping: {e2}")
        _notify_failure(message, recipients)
        return {"ok": False, "runId": run_id, "status": "failed", "error": message}
    finally:
        for name in ofac.OFAC_FILES:
            try:
                os.remove(os.path.join(TMP_DIR, name))
            except OSError:
                pass
        conn.close()


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (v or None)


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    return run_sync(force=bool(event.get("force")), trigger=str(event.get("trigger") or "schedule"))
