# Backend Nuwa 2.0 — Documentos, entidades, búsqueda y análisis de vínculos

Referencia de lo implementado en el backend (Lambdas Python + Postgres/RDS + S3). Contrato HTTP detallado en **`openapi/openapi.yaml`**.

**API prod:** `https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod`  
**Autenticación:** `Authorization: Bearer {accessToken}` + `clientId` / `userId` numéricos en body (claims JWT `cid` / `sub`).

---

## Resumen por módulo

| Módulo | Lambda | Rutas principales | Estado |
|--------|--------|-------------------|--------|
| **Documentos** | `*-lambda-documents` | `/v1/documents/*`, `/v1/clients/storage/init` | Desplegado prod |
| **Entidades** | `*-lambda-entities` | `/v1/entities/*` | Desplegado prod |
| **Búsqueda** | `*-lambda-search` | `POST /v1/search` | Desplegado prod |
| **Ingest chunks** | `*-lambda-chunks` | `POST /v1/chunks/ingest` | Desplegado prod |
| **Reportes** | `*-lambda-reports` | `/v1/reports/*` | Desplegado prod (+ vínculo entidad) |
| **Match / vínculos** | Lógica compartida | `entity_helpers.find_matches` | Usado en entidades y documentos |

---

## 1. Carga de documentos (S3 + Postgres)

### Arquitectura

```
Front → presign (API) → PUT directo S3 → upload-complete → finalize (Grok JSON)
                                                              ↓
                    documents + document_entity_links + entities (document_mention)
                                                              ↓
                    sources privados + risk_entity_chunks (indexación)
```

- **Binarios:** bucket `nuwa2-us-east-1-prod-client-documents`, prefijo `clients/{clientId}/documents/{documentId}/…`
- **Metadata:** tabla `public.documents`
- **Vínculos parte↔entidad:** tabla `public.document_entity_links`
- **Perfil storage:** tabla `public.client_storage_profiles` (prefijo S3 por tenant)

### Código

| Archivo | Rol |
|---------|-----|
| `cdk/lambdas/handler_documents.py` | Router HTTP, auth JWT |
| `cdk/lambdas/nuwa_documents_pg.py` | CRUD documentos, finalize, links, indexación |
| `cdk/lambdas/nuwa_s3_documents.py` | Presigned PUT/GET, HeadObject |
| `cdk/lambdas/document_helpers.py` | MIME, keys S3, chunks para index |
| `cdk/lambdas/handler_admin.py` | Hook `storage/init` al crear compañía |

### Flujo API

1. **`POST /v1/clients/storage/init`** — una vez por `clientId`; crea prefijo S3 y fila en `client_storage_profiles`.
2. **`POST /v1/documents/presign`** — devuelve `uploadUrl`, `documentId`, headers requeridos.
3. **`PUT {uploadUrl}`** — navegador → S3 (no pasa por API).
4. **`POST /v1/documents/upload-complete`** — valida objeto en S3, estado `uploaded`.
5. **`POST /v1/documents/finalize`** — body con `extractedJson` (salida Grok en BFF):
   - Resuelve **partes** (`parties[]`) contra entidades existentes o crea nuevas (`category=document_mention`).
   - Inserta filas en **`document_entity_links`** (rol, `is_primary`, confidence, payload).
   - Opcional: crea **source privada** + **`risk_entity_chunks`** para búsqueda interna del documento.
6. **`POST /v1/documents/list|get|update|delete|download-url`** — gestión y descarga firmada.

### Finalize — campos relevantes del body

| Campo | Descripción |
|-------|-------------|
| `extractedJson` | JSON Grok: `parties`, `summary`, `documentType`, `addresses`, etc. |
| `primaryEntityId` | Entidad principal del documento (opcional, validada en tenant) |
| `autoCreateEntities` | Default `true` — crea entidades si no hay match fuerte |
| `autoIndex` | Default `true` — source/chunks privados para search |
| `extractedText` | Texto plano alternativo para indexación |
| `requestId` | Idempotencia en reintentos de finalize |

### Respuesta finalize (ejemplo)

```json
{
  "success": true,
  "documentId": "uuid",
  "status": "ready",
  "primaryEntityId": "uuid",
  "linksCreated": 3,
  "entitiesCreated": 1,
  "entitiesMatched": 2,
  "sourceId": 42
}
```

### CORS S3

Orígenes permitidos: `https://app.nuwa.space`, `http://app.nuwa.space`, localhost. Ver CDK `nuwa_api_stack.py` o bucket CORS en AWS.

### Migración

`supabase/migrations/20260531120000_client_documents.sql`

Detalle operativo: **`docs/DOCUMENTS_MODULE.md`**.

---

## 2. Entidades (PF / PM)

### Tabla `public.entities`

Entidades por tenant con `party_type` (`individual` | `organization`), nombres desglosados (PF: `first_name`+`last_name`; PM: `legal_name`), RFC/CURP, `category`, `relationship_role`, riesgo, screening.

**Categorías:** `screening`, `background_check`, `employee`, `director`, `vendor`, `client`, `associate`, `pep`, `representative`, `beneficial_owner`, **`document_mention`** (ocultas en listados por defecto).

### Endpoints

| Método API | Función |
|------------|---------|
| `POST /v1/entities/match` | Dedupe antes de alta — ver §4 |
| `POST /v1/entities/create` | Alta PF/PM con validación de campos |
| `POST /v1/entities/list` | Listado paginado; excluye `document_mention` salvo `includeDocumentMentions: true` |
| `POST /v1/entities/get` | Detalle por `entityId` |
| `POST /v1/entities/update` | Actualización parcial |
| `POST /v1/entities/delete` | Soft delete (`status=deleted`) |
| `POST /v1/entities/stats` | Agregados para widgets |
| `POST /v1/entities/monitoring/upsert` | Config monitoreo continuo |
| `POST /v1/entities/monitoring/list` | Listado con próxima ejecución |

### Código

| Archivo | Rol |
|---------|-----|
| `cdk/lambdas/handler_entities.py` | Router HTTP |
| `cdk/lambdas/nuwa_entities_pg.py` | SQL, list/get/create/update, monitoreo |
| `cdk/lambdas/entity_helpers.py` | Normalización nombres, RFC/CURP, match, riesgo |

### Vínculo con reportes

Al guardar un reporte con `entityId`, `handler_reports` llama **`touch_entity_after_report_pg`**:

- Actualiza `last_screening_at`, `last_report_folio`, `risk_level`.
- Si la entidad era **`document_mention`**, la promueve a **`screening`**.

Migración base: `supabase/migrations/20260516120000_entities_monitoring.sql`.

Guía front: **`docs/PROMPT_INTEGRACION_FRONT_ENTIDADES.md`**.

---

## 3. Búsqueda (`POST /v1/search`)

### Comportamiento

La Lambda **`handler_search`** invoca la RPC Postgres **`search_risk_entities`** sobre `public.risk_entity_chunks`.

Combina:

1. **`word_similarity`** (extensión `pg_trgm`) — nombres embebidos en texto largo, tolera typos leves.
2. **FTS** (`websearch_to_tsquery`) — tokens exactos en `chunk_text`.
3. **RFC** normalizado — match adicional si se envía `rfc` en el body.

### Request

```json
{
  "clientId": 1,
  "query": "NOMBRE A BUSCAR",
  "rfc": "XAXX010101000",
  "entityTypes": ["person", "organization"],
  "riskLevels": [0, 1, 2, 3],
  "limit": 20,
  "wordSimilarityThreshold": 0.38
}
```

`riskLevels` usa escala fuentes **0=bajo … 3=crítico** (migración `20260530120000_source_risk_level_0_3.sql`).

### Response (cada hit)

```json
{
  "chunkId": "uuid",
  "sourceId": 1,
  "riskLevel": 2,
  "entityType": "person",
  "score": 0.87,
  "snippet": "...<mark>Nombre</mark>...",
  "chunkText": "...",
  "visibility": "public"
}
```

Visibilidad: chunks **públicos** (fuentes globales + `client_id=1`) y **privados** (solo tenant). La RPC filtra por `client_id` del JWT.

### Indexación de documentos

Tras `finalize`, si `autoIndex=true`, se crea una **source privada** del cliente y chunks derivados de `document_helpers.build_index_chunks(extractedJson)` — las partes y resumen quedan buscables vía `/v1/search` para ese tenant.

### Ingest manual de chunks

`POST /v1/chunks/ingest` — carga chunks a una source existente (`replaceStrategy`: `all` | `append`). Ver **`docs/INGEST_CHUNKING.md`**.

### Código y SQL

| Archivo | Rol |
|---------|-----|
| `cdk/lambdas/handler_search.py` | Validación, mapeo respuesta |
| `cdk/lambdas/nuwa_pg_dispatch.py` / `nuwa_supabase.py` | Invocación RPC |
| `supabase/migrations/20260405120000_risk_entities_search.sql` | Tabla chunks + función search |

Teoría y umbrales: **`docs/API_AND_ARCHITECTURE.md`** §12.1.

---

## 4. Análisis de vínculos (match entidad ↔ documento)

Hay **dos capas** de “vínculos”:

### 4.1 Match de entidades (deduplicación)

**Endpoint:** `POST /v1/entities/match`  
**Motor:** `entity_helpers.find_matches()`

Compara un candidato (PF o PM) contra todas las entidades activas del tenant.

| Tipo match | Condición típica | Confidence |
|------------|------------------|------------|
| `exact_identifier` | RFC/CURP igual (normalizado) | 95–100 |
| `fuzzy_name` | Similitud de nombre ≥ 85% | variable |
| `word_overlap` | Palabras compartidas | hasta ~80 |

Parámetro `minConfidence` (default 60). **`hasStrongMatch`**: confidence ≥ 90 — usado en UI para sugerir entidad existente antes de crear duplicado.

Usado también en **`documents/finalize`** con `min_confidence=90` para decidir si vincular a entidad existente o crear `document_mention`.

### 4.2 Vínculos documento ↔ entidad

**Tabla:** `public.document_entity_links`

| Columna | Descripción |
|---------|-------------|
| `document_id` | FK → `documents` |
| `entity_id` | FK → `entities` |
| `role` | Rol de la parte en el documento (ej. arrendador, accionista) |
| `is_primary` | Parte principal del documento |
| `confidence` | Score del match al finalize |
| `mention_source` | Default `grok` |
| `mention_payload` | JSON crudo de la extracción |

**Unique:** `(document_id, entity_id)`.

Al **finalize**, por cada `party` en `extractedJson`:

1. Match por RFC/CURP exacto en candidatos del tenant.
2. Si no, `find_matches` con umbral 90.
3. Si match fuerte → vincula (`entitiesMatched`).
4. Si no y `autoCreateEntities` → `entities_create_pg` con `category=document_mention` (`entitiesCreated`).
5. `_upsert_link()` persiste el vínculo.

Entidades `document_mention` **no aparecen** en `entities/list` salvo `includeDocumentMentions: true`. Tras un screening (`reports/save` + `entityId`), pasan a `category=screening`.

### 4.3 Vínculo reporte ↔ entidad

`POST /v1/reports/save` acepta **`entityId`** (UUID). Actualiza historial de la entidad y consolida riesgo. Ver `touch_entity_after_report_pg` en `nuwa_entities_pg.py`.

---

## 5. Infraestructura CDK (prod)

| Recurso | Nombre / notas |
|---------|----------------|
| Lambda documentos | `nuwa2-us-east-1-prod-lambda-documents` |
| Bucket S3 | `nuwa2-us-east-1-prod-client-documents` |
| Env | `NUWA_DOCUMENTS_BUCKET`, `NUWA_DOCUMENTS_MAX_BYTES`, `NUWA_DOCUMENTS_PRESIGN_TTL` |
| CORS API | API Gateway `*` + headers Lambda (`nuwa_http.py`) |
| CORS S3 | `app.nuwa.space` + localhost |

Deploy: ver **`docs/CDK_AWS.md`** y `scripts/cdk_deploy_hint.sh`.

---

## 6. Tests

| Test | Qué cubre |
|------|-----------|
| `tests/test_document_helpers.py` | MIME, filenames, index chunks |
| `tests/test_openapi_yaml.py` | Validación OpenAPI (swagger-cli) |
| Tests entidades / search | Ver commits previos y smoke `scripts/smoke_api.sh` |

---

## 7. Documentos relacionados

| Documento | Contenido |
|-----------|-----------|
| `docs/DOCUMENTS_MODULE.md` | Smoke curl documentos |
| `docs/PROMPT_INTEGRACION_FRONT_ENTIDADES.md` | Integración front entidades |
| `docs/PROMPT_FRONT_V2_DEPLOY.md` | Deploy front en `app.nuwa.space/v2` |
| `docs/DATABASE_SCHEMA.md` | Esquema tablas |
| `docs/INGEST_CHUNKING.md` | Ingest CSV/PDF → chunks |
| `openapi/openapi.yaml` | Contrato HTTP completo |
