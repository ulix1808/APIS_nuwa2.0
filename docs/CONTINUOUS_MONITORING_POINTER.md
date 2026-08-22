# Monitoreo continuo — puntero al spec front

La arquitectura completa (tablas, APIs, BFF Redis queue, **EventBridge en madrugada `America/Mexico_City`**) vive en el repo Nuwa 2.0 front:

`b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_ARCHITECTURE_20260822.md`  
Pruebas: `b_CvPklPEoxLx/docs/CONTINUOUS_MONITORING_TESTS_20260822.md`

**Auth servicio:** header `x-monitoring-worker-secret` = `MONITORING_WORKER_SECRET` (due, run-*, alerts/create, BFF enqueue).

**Multi-tenant:** cada item due/job lleva `clientId` + `entityId`; writes validan pertenencia.
