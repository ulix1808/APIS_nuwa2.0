# Monitoreo continuo — puntero al spec front

La arquitectura completa (tablas, APIs, BFF Redis queue, **EventBridge en madrugada `America/Mexico_City`**) vive en el repo Nuwa 2.0 front:

`b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_ARCHITECTURE_20260822.md`  
Pruebas: `b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_TESTS_20260822.md`

**Auth servicio:** header `x-monitoring-worker-secret` = `MONITORING_WORKER_SECRET` (due, run-*, alerts/create, BFF enqueue, **reports get/save/update**).

**Multi-tenant:** cada item due/job lleva `clientId` + `entityId`; writes validan pertenencia.

**Reportes (worker):** `GET/POST /v1/reports/get` solo por `clientId`+`folio`; `POST /v1/reports/save` exige `entityId` del tenant; `POST /v1/reports/update` exige `clientId` + ownership del folio. Lógica compartida en `cdk/lambdas/nuwa_monitoring_worker.py`.
