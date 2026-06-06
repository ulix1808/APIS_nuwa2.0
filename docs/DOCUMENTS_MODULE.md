# Módulo de documentos (S3 + Postgres)

Documentos internos del cliente: binarios en **S3**, metadata en **`public.documents`**, menciones en **`document_entity_links`**.

**Documentación ampliada (documentos + entidades + search + vínculos):** [`BACKEND_FEATURES.md`](./BACKEND_FEATURES.md).

## Variables de entorno (Lambda)

| Variable | Descripción |
|----------|-------------|
| `NUWA_DOCUMENTS_BUCKET` | Nombre del bucket (CDK output `ClientDocumentsBucketName`) |
| `NUWA_DOCUMENTS_MAX_BYTES` | Tamaño máximo upload (default 52428800) |
| `NUWA_DOCUMENTS_PRESIGN_TTL` | Segundos URL firmada (default 900) |

## Flujo con frontend

```
POST /v1/clients/storage/init
POST /v1/documents/presign
PUT  {uploadUrl}  (browser → S3)
POST /v1/documents/upload-complete
POST /v1/documents/finalize  { extractedJson from Grok BFF }
POST /v1/documents/list
```

## Entidades ocultas

Auto-creadas con `category=document_mention`. `POST /v1/entities/list` las excluye salvo `includeDocumentMentions: true`. Tras `POST /v1/reports/save` con `entityId`, se promueven a `screening`.

## Migración

`supabase/migrations/20260531120000_client_documents.sql`

## Smoke (curl)

```bash
export BASE="https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod"
export TOKEN="..."
curl -sS -X POST "$BASE/v1/clients/storage/init" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"clientId":1,"userId":1}'
```

## Deploy

Requiere `cdk deploy` con stack actual (Lambda `*-lambda-documents` y bucket `*-client-documents`).

Lambdas en VPC necesitan **S3 Gateway Endpoint** o NAT para presigned/HeadObject.

CORS S3 prod: `https://app.nuwa.space`, `http://app.nuwa.space`, localhost.
