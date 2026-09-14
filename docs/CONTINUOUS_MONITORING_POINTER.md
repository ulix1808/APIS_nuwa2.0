# Monitoreo continuo — puntero al spec front

La arquitectura completa (tablas, APIs, BFF Redis queue, **EventBridge cada 2 h UTC**) vive en el repo Nuwa 2.0 front:

`b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_ARCHITECTURE_20260822.md`  
Pruebas: `b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_TESTS_20260822.md`

**Auth servicio:** header `x-monitoring-worker-secret` = `MONITORING_WORKER_SECRET` (due, run-*, alerts/create, BFF enqueue, **reports get/save/update**).

**Multi-tenant:** cada item due/job lleva `clientId` + `entityId`; writes validan pertenencia.

**Reportes (worker):** `GET/POST /v1/reports/get` solo por `clientId`+`folio`; `POST /v1/reports/save` exige `entityId` del tenant; `POST /v1/reports/update` exige `clientId` + ownership del folio. Lógica en `nuwa_monitoring_worker.py`; env `MONITORING_WORKER_SECRET` en Lambdas **entities** y **reports**.

**Scheduler:** Lambda `monitoring-due-tick` (cron `0 */2 * * ? *` UTC). Tras `run-finish` con `error`/`skipped`, `next_run_at = now` para reintentar en el siguiente tick. Alertas pueden usar `alertType=run_failed` (migración `20260914000000_entity_alerts_run_failed.sql`).
