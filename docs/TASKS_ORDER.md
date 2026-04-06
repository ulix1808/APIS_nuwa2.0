# Orden sugerido de trabajo (Nuwa 2.0 APIs)

1. **Supabase / Postgres** — Migraciones en orden: `sources` → `risk_entity_chunks` + RPC → `rbac` + `reports` → `apigw_key_id` / `apigw_key_secret` → `nuwa_user_nuwa_space` (ver lista en `docs/RDS_LAMBDA.md` o `docs/API_AND_ARCHITECTURE.md` § 13).
2. **Secretos AWS** — SSM URL + `supabase-service-role-key` + **`app-crypto`** (`jwt_signing_secret`, `fernet_key`); en modo RDS, `nuwa2/<env>/database`.
3. **Desplegar CDK** — `./scripts/bundle_lambda_deps.sh` si usas dependencias nativas; API Gateway + Lambdas; rotar valores del secreto `app-crypto` en prod.
4. **Probar auth** — `POST /v1/auth/login` → `Authorization: Bearer` en search/admin/reportes; ver diagramas en `docs/API_AND_ARCHITECTURE.md` § 1.
5. **Implementar lógica real** en `handler_sources`, `handler_chunks` (stubs); `handler_search` ya acotado por JWT.
6. **Reportes y admin** — Usuario seed según migración RBAC; cambiar contraseñas en prod.
7. **Índices JSON** — Tras estabilizar `report_json`, GIN / columnas generadas si hace falta.
8. **Evolución auth** — Refresh tokens, authorizer en gateway, Cognito opcional (hoy JWT propio HS256).
9. **Pruebas** — `pip install -r requirements-dev.txt && pytest`; mocks JWT/PostgREST según crezca `tests/`.
