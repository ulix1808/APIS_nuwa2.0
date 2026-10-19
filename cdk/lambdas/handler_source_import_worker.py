"""
Background worker for source imports.

Two events:
- {"importId": "..."}: process one import. Downloads the file from S3, reads it, builds the records
  and applies them in one transaction (source_import_db.apply_records). Progress and the result go to
  public.source_imports, which the admin page polls.
- {"autoUpdate": true} (monthly EventBridge rule): for every source with metadata.autoUpdate = "true"
  and a downloadUrl, download the file; if it changed since the last automatic load, store it in S3
  and queue an incremental, strict import with the source's saved column mapping.

Strict imports (the automatic ones) stop instead of loading when the file no longer matches the saved
mapping (a mapped column is missing, a new column appeared) or has lost characters (�). A failed or
blocked automatic import is reported to the BFF, which mails every super admin; the admin bell shows it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import boto3

from nuwa_obs_log import log_phase
import source_import_core as core
import source_import_db as db

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
SYSTEM_USER_ID = 0


def _bucket() -> str:
    return (os.environ.get("NUWA_DOCUMENTS_BUCKET") or "").strip()


def _extra_columns(headers: list[str], mapping: dict[str, str]) -> list[str]:
    """File columns the saved mapping does not mention at all (the source added a column)."""
    known = {core.header_match_key(h) for h in mapping}
    out = []
    for h in headers:
        fixed = core.repair_text(h, "mac")  # the mapping was saved with repaired headers
        if core.header_match_key(h) not in known and core.header_match_key(fixed) not in known:
            out.append(h)
    return out


def _mexico_today() -> str:
    """YYYY-MM-DD in Mexico City (the admin shows dates there)."""
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("America/Mexico_City")).date().isoformat()
    except Exception:  # noqa: BLE001 — no tz database: Mexico City has no DST since 2022
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).date().isoformat()


def notify_bff(imp: dict[str, Any], *, status: str, message: str) -> None:
    """Tell the BFF an automatic import failed or was blocked (it mails super admins). Best effort."""
    base = (os.environ.get("MONITORING_BFF_URL") or "").rstrip("/")
    secret = (os.environ.get("MONITORING_WORKER_SECRET") or "").strip()
    if not base or not secret:
        log_phase("source_import", "notify skipped: MONITORING_BFF_URL / MONITORING_WORKER_SECRET missing")
        return
    body = json.dumps(
        {
            "importId": str(imp.get("id")),
            "sourceId": int(imp.get("source_id") or 0),
            "fileName": imp.get("file_name"),
            "status": status,
            "message": message[:1500],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/internal/source-import-alert",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-monitoring-worker-secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log_phase("source_import", f"notify {status} -> {r.status}")
    except Exception as e:  # noqa: BLE001
        log_phase("source_import", f"notify failed: {e}")


def run_import(import_id: str) -> dict[str, Any]:
    status_conn = db.connect(autocommit=True)
    started = time.time()
    imp: dict[str, Any] | None = None

    def progress(message: str, **extra: Any) -> None:
        log_phase("source_import", f"{import_id} {message}")
        db.set_status(status_conn, import_id, "processing", progress={"message": message, **extra})

    try:
        imp = db.claim_import(status_conn, import_id)
        if imp is None:
            return {"ok": False, "error": "not queued (already taken or finished)"}
        cfg = imp["config"] or {}
        strict = bool(cfg.get("strict"))
        progress("Descargando archivo")
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=imp["s3_key"])
        data = obj["Body"].read()

        progress("Leyendo archivo", bytes=len(data))
        name = imp["file_name"]
        header_row = int(cfg.get("headerRowIndex") or 0)
        if name.lower().endswith(".json"):
            headers, rows = core.read_json_records(data)
        else:
            matrix = core.read_matrix(data, name, sheet_name=cfg.get("sheetName"))
            headers, rows = core.table_from_matrix(matrix, header_row)
        if not rows:
            raise core.SourceFileError("No hay filas de datos debajo de la cabecera.")

        progress("Preparando registros", rowsRead=len(rows))
        mapping = {str(k): str(v) for k, v in (cfg.get("columnMapping") or {}).items()}
        key_field = (cfg.get("keyField") or "").strip() or None
        built = core.build_records(headers, rows, mapping, key_field)
        if not built.records:
            raise core.SourceFileError("Ninguna fila tiene datos en las columnas asignadas.")
        if strict:
            problems = []
            if built.unmatched_mapping:
                problems.append("faltan columnas que la carga anterior usaba: " + ", ".join(built.unmatched_mapping))
            extra = _extra_columns(headers, mapping)
            if extra:
                problems.append("el archivo trae columnas nuevas: " + ", ".join(extra))
            if built.lost_character_cells:
                problems.append(f"{built.lost_character_cells} registros con caracteres perdidos (�)")
            if problems:
                raise core.SourceFileError(
                    "Carga automática detenida para revisión: " + "; ".join(problems)
                    + ". Revise el archivo y cárguelo a mano con el mapeo correcto."
                )

        # autocommit: apply_records' transaction() is then a real transaction that commits.
        conn = db.connect(autocommit=True)
        try:
            source = db.fetch_source(conn, int(imp["source_id"]))
            if source is None:
                raise core.SourceFileError("La fuente ya no existe.")
            meta_updates = {
                "fileName": name,
                "columnMapping": json.dumps(mapping, ensure_ascii=False),
                "headerRowIndex": str(header_row),
            }
            if cfg.get("sourceDate"):
                meta_updates["Source Date"] = str(cfg["sourceDate"])
            if cfg.get("downloadSha256"):
                meta_updates["lastDownloadSha256"] = str(cfg["downloadSha256"])
            # A completed upload is also a review of the source (Editar fuente → Fecha de revisión).
            today = _mexico_today()
            meta_updates["reviewedAt"] = today
            if cfg.get("auto") and not cfg.get("sourceDate"):
                meta_updates["Source Date"] = today[:7]  # the month the source was downloaded
            counts = db.apply_records(
                conn,
                source=source,
                import_id=import_id,
                mode=imp["mode"],
                records=built.records,
                key_field=key_field,
                risk_level=int(cfg.get("riskLevel") or source["risk_level"]),
                visibility=str(cfg.get("visibility") or source["visibility"] or "public"),
                entity_type=str(cfg.get("entityType") or "company")[:200],
                metadata_updates=meta_updates,
                progress=lambda m: progress(m, rowsRead=len(rows)),
            )
        finally:
            conn.close()

        result = {
            **counts,
            "rows": built.rows_in_file,
            "records": len(built.records),
            "duplicatesInFile": built.duplicates_in_file,
            "emptyRows": built.empty_rows,
            "repairedCells": built.repaired_cells,
            "textDamage": built.damage,
            "lostCharacterCells": built.lost_character_cells,
            "unmatchedColumns": built.unmatched_mapping,
            "seconds": round(time.time() - started, 1),
        }
        db.set_status(status_conn, import_id, "completed", result=result, progress={"message": "Listo"})
        log_phase("source_import", f"{import_id} completed {result}")
        return {"ok": True, "result": result}
    except Exception as e:  # noqa: BLE001 — every failure is reported on the import row
        msg = str(e) if isinstance(e, core.SourceFileError) else f"{type(e).__name__}: {e}"
        log_phase("source_import", f"{import_id} failed: {msg}\n{traceback.format_exc()}")
        try:
            db.set_status(status_conn, import_id, "failed", error=msg[:2000], progress={"message": "Falló"})
        except Exception:  # noqa: BLE001
            pass
        if imp is not None and (imp.get("config") or {}).get("auto"):
            notify_bff(imp, status="failed", message=msg)
        return {"ok": False, "error": msg}
    finally:
        status_conn.close()


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; NuwaSourceUpdate/1.0)"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise core.SourceFileError("El archivo de la fuente supera 1 GB.")
    if not data.strip():
        raise core.SourceFileError("La fuente devolvió un archivo vacío.")
    return data


def _bff_call(method: str, path: str, timeout: int = 30) -> tuple[int, bytes, dict[str, str]]:
    base = (os.environ.get("MONITORING_BFF_URL") or "").rstrip("/")
    secret = (os.environ.get("MONITORING_WORKER_SECRET") or "").strip()
    if not base or not secret:
        raise core.SourceFileError("Falta MONITORING_BFF_URL / MONITORING_WORKER_SECRET para el scraper.")
    req = urllib.request.Request(f"{base}{path}", method=method, data=b"" if method == "POST" else None,
                                 headers={"x-monitoring-worker-secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _scrape(name: str, budget_sec: int = 11 * 60) -> tuple[bytes, str]:
    """Browser-based source: start a run in the fuentes-scraper container (through the BFF) and wait."""
    status, body, _ = _bff_call("POST", f"/api/internal/source-scrape/{name}")
    if status not in (200, 202):
        raise core.SourceFileError(f"No se pudo iniciar el scraper {name}: {status} {body[:200]!r}")
    deadline = time.time() + budget_sec
    while time.time() < deadline:
        time.sleep(15)
        status, body, _ = _bff_call("GET", f"/api/internal/source-scrape/{name}")
        run = (json.loads(body or b"{}") or {}).get("run") or {}
        if run.get("status") == "failed":
            raise core.SourceFileError(f"El scraper {name} falló: {run.get('error') or 'sin detalle'}")
        if run.get("status") == "completed":
            status, data, headers = _bff_call("GET", f"/api/internal/source-scrape/{name}/file", timeout=120)
            if status != 200 or not data.strip():
                raise core.SourceFileError(f"El scraper {name} terminó pero no se pudo leer su archivo ({status}).")
            return data, headers.get("x-file-name") or run.get("fileName") or f"{name}.csv"
    raise core.SourceFileError(f"El scraper {name} no terminó en {budget_sec // 60} minutos.")


AUTO_SOURCES_SQL = """select id, client_id, name, risk_level, visibility, metadata from public.sources
   where coalesce(metadata->>'autoUpdate','') = 'true'
     and (coalesce(metadata->>'downloadUrl','') <> '' or coalesce(metadata->>'scraper','') <> '')"""


def _function_name(context: Any) -> str:
    return context.function_name if context else os.environ["AWS_LAMBDA_FUNCTION_NAME"]


def auto_update_source(conn, src: dict[str, Any], context: Any) -> dict[str, Any]:
    """Download (or scrape) one source and queue its strict incremental import."""
    meta = src["metadata"] or {}
    sid = int(src["id"])
    url = str(meta.get("downloadUrl") or "").strip()
    scraper = str(meta.get("scraper") or "").strip()
    file_name = f"{scraper}.csv" if scraper else (os.path.basename(urllib.parse.urlparse(url).path) or f"fuente_{sid}.csv")
    import_id = str(uuid.uuid4())
    s3_key = f"clients/{int(src['client_id'])}/source-imports/{sid}/{import_id}/{file_name}"
    try:
        mapping = json.loads(meta.get("columnMapping") or "{}")
    except json.JSONDecodeError:
        mapping = {}
    config = {
        "auto": True,
        "strict": True,
        "headerRowIndex": int(meta.get("headerRowIndex") or 0),
        "columnMapping": mapping,
        "keyField": "",
        "riskLevel": int(src["risk_level"]),
        "downloadUrl": url,
        **({"scraper": scraper} if scraper else {}),
    }
    try:
        if scraper:
            data, file_name = _scrape(scraper)
            s3_key = f"clients/{int(src['client_id'])}/source-imports/{sid}/{import_id}/{file_name}"
        else:
            data = _download(url)
        sha = hashlib.sha256(data).hexdigest()
        if sha == meta.get("lastDownloadSha256"):
            return {"sourceId": sid, "skipped": "sin cambios desde la última carga automática"}
        config["downloadSha256"] = sha
        boto3.client("s3").put_object(Bucket=_bucket(), Key=s3_key, Body=data)
        db.create_import(
            conn, id=import_id, source_id=sid, client_id=int(src["client_id"]), user_id=SYSTEM_USER_ID,
            mode="incremental", s3_key=s3_key, file_name=file_name, file_size=len(data),
            content_type="text/csv" if file_name.lower().endswith(".csv") else "application/octet-stream",
            config=config,
        )
        db.set_status(conn, import_id, "queued", progress={"message": "En cola (actualización automática)"})
        if not mapping:
            raise core.SourceFileError("La fuente no tiene mapeo de columnas guardado: cárguela una vez a mano.")
        boto3.client("lambda").invoke(
            FunctionName=_function_name(context),
            InvocationType="Event",
            Payload=json.dumps({"importId": import_id}).encode("utf-8"),
        )
        return {"sourceId": sid, "importId": import_id, "bytes": len(data)}
    except Exception as e:  # noqa: BLE001 — one source failing does not stop the others
        origin = f"el scraper {scraper}" if scraper else url
        msg = str(e) if isinstance(e, core.SourceFileError) else f"No se pudo descargar {origin}: {type(e).__name__}: {e}"
        log_phase("source_import", f"auto source={sid} failed: {msg}")
        existing = db.get_import(conn, import_id)
        if existing is None:
            existing = db.create_import(
                conn, id=import_id, source_id=sid, client_id=int(src["client_id"]), user_id=SYSTEM_USER_ID,
                mode="incremental", s3_key=s3_key, file_name=file_name, file_size=None,
                content_type=None, config=config,
            )
        db.set_status(conn, import_id, "failed", error=msg[:2000], progress={"message": "Falló"})
        notify_bff(existing, status="failed", message=msg)
        return {"sourceId": sid, "error": msg}


def run_auto_updates(context: Any = None) -> dict[str, Any]:
    """Monthly: direct downloads run here; each scraper source gets its own invocation (they take minutes)."""
    conn = db.connect(autocommit=True)
    out: list[dict[str, Any]] = []
    try:
        for src in conn.execute(AUTO_SOURCES_SQL + " order by id").fetchall():
            if str((src["metadata"] or {}).get("scraper") or "").strip():
                boto3.client("lambda").invoke(
                    FunctionName=_function_name(context),
                    InvocationType="Event",
                    Payload=json.dumps({"autoUpdateSource": int(src["id"])}).encode("utf-8"),
                )
                out.append({"sourceId": int(src["id"]), "scraper": "invocación propia"})
            else:
                out.append(auto_update_source(conn, src, context))
    finally:
        conn.close()
    log_phase("source_import", f"auto-update run: {out}")
    return {"ok": True, "sources": out}


def run_auto_update_one(source_id: int, context: Any = None) -> dict[str, Any]:
    conn = db.connect(autocommit=True)
    try:
        src = conn.execute(AUTO_SOURCES_SQL + " and id = %s", (source_id,)).fetchone()
        if src is None:
            return {"ok": False, "error": f"fuente {source_id} sin actualización automática"}
        return {"ok": True, **auto_update_source(conn, src, context)}
    finally:
        conn.close()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    event = event or {}
    if event.get("autoUpdate"):
        return run_auto_updates(context)
    if event.get("autoUpdateSource"):
        return run_auto_update_one(int(event["autoUpdateSource"]), context)
    import_id = str(event.get("importId") or "").strip()
    if not import_id:
        return {"ok": False, "error": "importId requerido"}
    return run_import(import_id)
