# Fuente Legal CJF — opción 2 (API Nuwa → Postgres)

## Estado datos (2026-08-26)

- **Backup previo:** `APIs/backups/nuwa2_pre_cjf_20260826T133031Z.dump` (63 MB, gitignored)
- **Tablas:** `cjf_documents` = 27 778 · `cjf_mentions` = 52 306
- **RPC:** `search_cjf_mentions` verificado (ej. `hsbc mexico`)

## Endpoints (tras deploy CDK)

| Método | Path | Uso |
|--------|------|-----|
| POST | `/v1/legal/cjf/search` | Búsqueda PF/PM en menciones CJF |
| POST | `/v1/legal/cjf/ingest` | Upsert batch de documentos JSON |
| POST | `/v1/legal/cjf/stats` | Conteos |

Auth: JWT Bearer + `clientId` (igual que `/v1/search`).

## Migración + carga

```bash
cd /Users/ulix/Documents/Code/nuwa2.0/APIs
export PGPASSWORD='...'
# Solo CJF (apply_migrations completo puede fallar en constraints ya existentes):
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260826000000_cjf_legal.sql
python3 scripts/ingest_cjf_json.py /Users/ulix/Downloads/jsoncompacts
```

## Deploy Lambda Legal

```bash
./scripts/bundle_lambda_deps.sh
# cdk deploy (mismo flujo prod) — publica /v1/legal/cjf/*
```

## BFF

`POST /api/legal/cjf-search` → Nuwa `/v1/legal/cjf/search` (fallback local `data/cjf/`).
Screening: sección Legal en reporte + Grok analiza menciones CJF sin sumar riesgo de listas.

## Restaurar backup si hace falta

```bash
export PGPASSWORD='...'
pg_restore --clean --if-exists --no-owner --no-acl -d nuwa2 \
  backups/nuwa2_pre_cjf_20260826T133031Z.dump
```
