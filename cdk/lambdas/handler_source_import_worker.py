"""
Background worker for source imports (invoked asynchronously with {"importId": "..."}).

Downloads the file from S3, reads it, builds the records and applies them in one transaction
(source_import_db.apply_records). Progress and the result go to public.source_imports, which the
admin page polls. Any error marks the import failed with its message (async retries are off).
"""

from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

import boto3

from nuwa_obs_log import log_phase
import source_import_core as core
import source_import_db as db


def _bucket() -> str:
    return (os.environ.get("NUWA_DOCUMENTS_BUCKET") or "").strip()


def run_import(import_id: str) -> dict[str, Any]:
    status_conn = db.connect(autocommit=True)
    started = time.time()

    def progress(message: str, **extra: Any) -> None:
        log_phase("source_import", f"{import_id} {message}")
        db.set_status(status_conn, import_id, "processing", progress={"message": message, **extra})

    try:
        imp = db.claim_import(status_conn, import_id)
        if imp is None:
            return {"ok": False, "error": "not queued (already taken or finished)"}
        cfg = imp["config"] or {}
        progress("Descargando archivo")
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=imp["s3_key"])
        data = obj["Body"].read()

        progress("Leyendo archivo", bytes=len(data))
        name = imp["file_name"]
        if name.lower().endswith(".json"):
            headers, rows = core.read_json_records(data)
        else:
            matrix = core.read_matrix(data, name, sheet_name=cfg.get("sheetName"))
            headers, rows = core.table_from_matrix(matrix, int(cfg.get("headerRowIndex") or 0))
        if not rows:
            raise core.SourceFileError("No hay filas de datos debajo de la cabecera.")

        progress("Preparando registros", rowsRead=len(rows))
        mapping = {str(k): str(v) for k, v in (cfg.get("columnMapping") or {}).items()}
        key_field = (cfg.get("keyField") or "").strip() or None
        built = core.build_records(headers, rows, mapping, key_field)
        if not built.records:
            raise core.SourceFileError("Ninguna fila tiene datos en las columnas asignadas.")

        conn = db.connect()
        try:
            source = db.fetch_source(conn, int(imp["source_id"]))
            if source is None:
                raise core.SourceFileError("La fuente ya no existe.")
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
                metadata_updates={
                    "fileName": name,
                    "columnMapping": json.dumps(mapping, ensure_ascii=False),
                    **({"Source Date": str(cfg["sourceDate"])} if cfg.get("sourceDate") else {}),
                },
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
        return {"ok": False, "error": msg}
    finally:
        status_conn.close()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    import_id = str((event or {}).get("importId") or "").strip()
    if not import_id:
        return {"ok": False, "error": "importId requerido"}
    return run_import(import_id)
