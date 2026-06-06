# Esquema PostgreSQL (Nuwa 2.0 APIs)

Referencia de **tablas y columnas** tal como se definen en `supabase/migrations/`. Para aplicar el esquema en RDS u otro Postgres, sigue el orden de los archivos SQL y `docs/RDS_LAMBDA.md`.

**Convenciones:** `timestamptz` para instantes; JSON editorial en `reports.report_json`; catálogo de fuentes en `sources`; texto indexable de búsqueda en `risk_entity_chunks.chunk_text`.

---

## Resumen de tablas

| Tabla | Propósito breve |
|-------|------------------|
| `companies` | Tenant / compañía (`client_id` único). |
| `nuwa_roles` | Roles RBAC (`super_admin`, `admin`, `user`). |
| `nuwa_users` | Usuarios de aplicación (login, FK a compañía y rol). |
| `reports` | Reportes post-búsqueda (JSON + columnas derivadas). |
| `risk_entity_chunks` | Chunks indexables por fuente (búsqueda / ingest). |
| `source_categories` | Taxonomía de categorías para el catálogo de fuentes. |
| `sources` | Catálogo de fuentes (SAT, etc.). |
| `entities` | Entidades PF/PM por tenant (`party_type`, `legal_name` / `first_name`+`last_name`, RFC/CURP). |
| `entity_monitoring` | Configuración de monitoreo continuo por entidad. |
| `entity_monitoring_runs` | Log de ejecuciones del scheduler (futuro). |
| `entity_alerts` | Alertas por cambio de riesgo o nuevos hallazgos (futuro). |
| `documents` | Metadata de documentos internos del cliente (S3 + extracción). |
| `document_entity_links` | Vínculo parte/entidad extraída ↔ entidad en `entities`. |
| `client_storage_profiles` | Prefijo S3 y cuotas por tenant para documentos. |

**Extensiones:** `pg_trgm` (búsqueda difusa sobre `chunk_text`).  
**RLS:** activado en varias tablas desde migraciones; en RDS con rol `postgres`/service las políticas pueden no limitar al mismo modo que en Supabase — ver notas en migraciones.

---

## `public.companies`

Compañía = tenant. `client_id` es el identificador de negocio usado en JWT y APIs.

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | bigserial | PK. |
| `client_id` | integer | **UNIQUE**, identificador tenant. |
| `name` | text | Nombre visible. |
| `details` | jsonb | Default `{}`. Metadatos libres. |
| `apigw_key_id` | text | Nullable. Id de API Key en API Gateway (ver migración). |
| `apigw_key_secret` | text | Nullable. Secreto para `x-api-key` del tenant (tratar como sensible; en despliegues actuales puede ir cifrado vía app-crypto). |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_companies_updated_at`. |

---

## `public.nuwa_roles`

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | smallserial | PK. |
| `slug` | text | **UNIQUE** (`super_admin`, `admin`, `user`). |
| `name` | text | Etiqueta legible. |

Seed inicial en migración RBAC.

---

## `public.nuwa_users`

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | bigserial | PK. |
| `client_id` | integer | FK → `companies(client_id)` **ON DELETE RESTRICT**. |
| `email` | text | **UNIQUE (`client_id`, `email`)**. |
| `password_hash` | text | Hash pbkdf2 (ver `nuwa_password` / migraciones seed). |
| `full_name` | text | |
| `role_id` | smallint | FK → `nuwa_roles(id)`. |
| `is_active` | boolean | Default `true`. |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_nuwa_users_updated_at`. |

---

## `public.reports`

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | uuid | PK, default `gen_random_uuid()`. |
| `folio` | text | Junto con `client_id` forma **UNIQUE**. |
| `client_id` | integer | Tenant. |
| `created_by_user_id` | bigint | FK → `nuwa_users(id)` **ON DELETE RESTRICT**. |
| `report_json` | jsonb | Default `{}`. Payload editorial / JSON completo del reporte. |
| `search_context` | jsonb | Default `{}`. Contexto de búsqueda asociado. |
| `title` | text | Nullable. |
| `status` | text | Default `'active'`; **CHECK** `active` \| `archived` \| `deleted`. |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_reports_updated_at`. |

**Columnas derivadas** (migración derived columns; facilitan listados sin parsear siempre `report_json`):

| Columna | Tipo |
|---------|------|
| `entidad` | text |
| `tipo_consulta` | text |
| `fecha` | date |
| `hora` | text |
| `nivel_riesgo` | text |
| `nivel_riesgo_numerico` | smallint |
| `total_listas_original` | integer |
| `total_listas_activas` | integer |
| `total_descartadas` | integer |
| `es_actualizacion` | boolean |
| `total_listas` | integer |
| `total_menciones` | integer |
| `grok_resumen` | text |
| `grok_falsos_positivos` | integer |
| `grok_confirmados` | integer |
| `entity_id` | uuid | FK → `entities` (migración entidades). |
| `parent_entity_id` | uuid | PM padre en screening múltiple. |
| `group_id` | text | Batch grupal (ej. `GRP-…`). |
| `group_name` | text | Nombre del Grupo. |
| `group_role` | text | Rol del sujeto en el grupo. |

Índices útiles: `client_id`, `created_by_user_id`, `folio`, `status`, `created_at`, GIN sobre `report_json`, etc. (ver SQL).

**Eliminado:** la columna legacy `report_save_state` (migración drop legacy).

---

## `public.sources`

Catálogo de fuentes. Visible por API `/v1/sources/*`.

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | bigserial | PK. |
| `name` | text | |
| `risk_level` | smallint | **CHECK** entre 0 y 3 (0=bajo, 1=medio, 2=alto, 3=crítico). |
| `visibility` | text | **`public`** \| **`private`**. |
| `client_id` | integer | Tenant dueño / contexto. |
| `created_by_user_id` | integer | Usuario que creó la fila. |
| `metadata` | jsonb | Default `{}`. |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_sources_updated_at`. |
| `source_category_id` | bigint | Nullable. FK → `source_categories(id)` (migración categorías). API exige categoría activa en altas nuevas. |

---

## `public.source_categories`

Taxonomía para fuentes (slug estable).

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | bigserial | PK. |
| `slug` | text | **UNIQUE**. |
| `name_es` | text | |
| `name_en` | text | Nullable. |
| `is_active` | boolean | Default `true`. |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_source_categories_updated_at`. |

Seed de categorías en la migración homónima.

---

## `public.risk_entity_chunks`

Una fila = un trozo de texto indexable asociado a una fuente. **No** existe columna `chunks`; el payload va en **`chunk_text`**.

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | uuid | PK, default `gen_random_uuid()`. |
| `client_id` | integer | Alineado con la fuente en ingest. |
| `risk_level` | smallint | **CHECK** 0–3 (misma escala que `sources`). |
| `source_id` | bigint | FK → `sources(id)` **ON DELETE CASCADE** (al borrar fuente se borran chunks). |
| `entity_type` | text | P. ej. `company`; default lógico en API `entity`. |
| `chunk_text` | text | Texto indexado (a menudo JSON serializado de una fila SAT). |
| `visibility` | text | Default `'private'`; **`public`** \| **`private`**. |
| `created_at` | timestamptz | Default `now()`. |
| `updated_at` | timestamptz | Trigger `trg_risk_entity_chunks_updated_at`. |
| `fts` | tsvector | **GENERATED STORED** desde `chunk_text` (`simple` config). |

Índices GIN en `fts` y `chunk_text` (trgm); btree en `source_id`, `client_id`, `entity_type`, `risk_level`.

---

## `public.documents`

Metadata de documentos internos; binario en S3 (`s3_key`). Migración `20260531120000_client_documents.sql`.

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | uuid | PK. |
| `client_id` | integer | Tenant. |
| `uploaded_by_user_id` | integer | Usuario que subió. |
| `original_filename` | text | Nombre original. |
| `mime_type` | text | Validado en presign. |
| `size_bytes` | bigint | Tamaño en S3. |
| `s3_key` | text | Ruta en bucket `clients/{clientId}/documents/{id}/…` |
| `status` | text | `pending` → `uploaded` → `processing` → `ready` / `deleted` |
| `document_type` | text | Tipo inferido (contrato, acta, etc.). |
| `document_date` | date | Fecha del documento si se extrajo. |
| `summary` | text | Resumen Grok. |
| `extracted_json` | jsonb | JSON completo de extracción + metadatos finalize. |
| `primary_entity_id` | uuid | FK nullable → `entities`. |
| `source_id` | bigint | FK nullable → source privada de indexación. |
| `created_at` / `updated_at` | timestamptz | Trigger `trg_documents_updated_at`. |

---

## `public.document_entity_links`

Vínculo entre un documento y entidades mencionadas/extraídas.

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | uuid | PK. |
| `client_id` | integer | Tenant. |
| `document_id` | uuid | FK → `documents`. |
| `entity_id` | uuid | FK → `entities`. |
| `role` | text | Rol de la parte en el documento. |
| `is_primary` | boolean | Parte principal. |
| `confidence` | numeric | Score del match al finalize. |
| `mention_source` | text | Default `grok`. |
| `mention_payload` | jsonb | Payload crudo de la extracción. |
| `created_at` | timestamptz | |

**UNIQUE** `(document_id, entity_id)`.

---

## `public.client_storage_profiles`

Perfil de almacenamiento S3 por tenant (creado con `POST /v1/clients/storage/init`).

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | uuid | PK. |
| `client_id` | integer | **UNIQUE** por tenant. |
| `s3_prefix` | text | Ej. `clients/1/`. |
| `max_storage_bytes` | bigint | Cuota opcional. |
| `created_at` / `updated_at` | timestamptz | |

---

## Funciones SQL relacionadas

- **`public.search_risk_entities(...)`** — definida en `20260405120000_risk_entities_search.sql`; búsqueda difusa / filtros sobre chunks visibles para el tenant.

---

## Mantenimiento del documento

Este archivo resume el estado **después de aplicar todas las migraciones** en orden. Si añades migraciones nuevas, actualiza esta referencia o enlázalas aquí.

**Ver también:** `docs/BACKEND_FEATURES.md`, `docs/RDS_LAMBDA.md`, `docs/API_AND_ARCHITECTURE.md`, `openapi/openapi.yaml`, `docs/INGEST_CHUNKING.md`, `docs/DOCUMENTS_MODULE.md`.
