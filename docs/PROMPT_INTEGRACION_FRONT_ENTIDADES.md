# Prompt de integración — Frontend Nuwa (Entidades + Monitoreo)

Usa este documento como especificación para migrar el front de Supabase local (`/api/entities/*`) al backend Nuwa (`/v1/entities/*` y cambios en `/v1/reports/save`).

## Contexto

- Base API: variable `NUWA_PROD_API_BASE` (ej. API Gateway prod).
- Autenticación: `Authorization: Bearer {accessToken}` del login Nuwa.
- Tenancy: siempre enviar **`clientId`** y **`userId` numéricos** (claims `cid` y `sub` del JWT), no el UUID legacy de Supabase.
- Sesión UI: seguir usando `localStorage.nuwa_session` con `numericClientId`, `numericUserId`, `accessToken`.

## Cambios por ruta Next (proxy)

| Ruta Next actual | Nueva llamada upstream |
|------------------|------------------------|
| `POST /api/entities/match` | `POST {NUWA}/v1/entities/match` |
| `POST /api/entities` (create) | `POST {NUWA}/v1/entities/create` |
| `GET /api/entities` | `POST {NUWA}/v1/entities/list` |
| `GET /api/entities/[id]` | `POST {NUWA}/v1/entities/get` |
| `PATCH /api/entities/[id]` | `POST {NUWA}/v1/entities/update` |
| `DELETE /api/entities/[id]` | `POST {NUWA}/v1/entities/delete` |
| *(nuevo)* stats widgets | `POST {NUWA}/v1/entities/stats` |
| `POST /api/monitoring` | `POST {NUWA}/v1/entities/monitoring/upsert` |
| `GET /api/monitoring` | `POST {NUWA}/v1/entities/monitoring/list` |
| `POST /api/screening/save-report` | Sin cambio de ruta Next; **añadir `entityId`** al body hacia `/v1/reports/save` |

## Catálogos Rol / Relación (PF ≠ PM)

Unificar etiqueta en los tres modos: **"Rol / Relación"**.

```ts
export const ROLE_OPTIONS_PF = [
  { value: "Accionista", label: "Accionista" },
  { value: "Apoderado", label: "Apoderado Legal" },
  { value: "Rep. Legal", label: "Representante Legal" },
  { value: "Consejero", label: "Consejero" },
  { value: "Beneficiario Final", label: "Beneficiario Final" },
  { value: "Comisario", label: "Comisario" },
  { value: "Otro", label: "Otro" },
] as const

export const ROLE_OPTIONS_PM = [
  { value: "Cliente", label: "Cliente" },
  { value: "Proveedor", label: "Proveedor" },
  { value: "Contraparte", label: "Contraparte" },
  { value: "Subsidiaria", label: "Subsidiaria" },
  { value: "Matriz", label: "Matriz" },
  { value: "Otro", label: "Otro" },
] as const
```

**Correcciones en `screening-search.tsx`:**
1. PM (persona moral): label `Tipo de Relacion` → `Rol / Relacion`; opciones = `ROLE_OPTIONS_PM`.
2. Múltiples sujetos: dropdown `Rol` → `Rol / Relacion`; opciones = `subject.tipo === "organization" ? ROLE_OPTIONS_PM : ROLE_OPTIONS_PF` (hoy usa siempre `ROLE_OPTIONS` — incorrecto para PM).

Persistir el valor en API como `relationshipRole` (create/update entidad) y en reporte como `groupRole` + `metadatos.relationshipRole`.

---

## Campos obligatorios por tipo (Persona física / Persona moral)

El backend **no** usa un solo `name` genérico. Debes enviar la estructura del formulario de screening.

### Persona moral (`partyType: "organization"`)

| Campo UI | Propiedad API | Columna DB |
|----------|---------------|------------|
| Razón Social / Denominación | `legalName` | `legal_name` |
| RFC (opcional) | `rfc` | `rfc` |
| Tipo | `partyType` + `partyTypeLabel: "Persona moral"` | `party_type` + `party_type_label` |

### Persona física (`partyType: "individual"`)

| Campo UI | Propiedad API | Columna DB |
|----------|---------------|------------|
| Nombre(s) | `firstName` | `first_name` |
| Apellido(s) | `lastName` | `last_name` |
| RFC / CURP (opcional, un input) | clasificar → `rfc` y/o `curp` | `rfc` / `curp` |
| Tipo | `partyType` + `partyTypeLabel: "Persona física"` | `party_type` + `party_type_label` |

Usa `classifyIdentifier()` (`lib/identifier-classifier.ts`) antes de create/match:

- CURP (18) → enviar `curp`
- RFC PF (13) → enviar `rfc`
- Si es desconocido pero hay texto, enviar como `rfc` provisional o omitir según validación

**Match (30 días):** solo devuelve entidades con consulta/reporte en los últimos 30 días.

**Duplicados:** create con RFC o CURP ya usado en el tenant → HTTP **409** (`DUPLICATE_RFC` / `DUPLICATE_CURP`).

**Monitoreo:** deshabilitar checkbox hasta tener `entityId` (tras create o elegir en modal). No llamar `monitoring/upsert` sin entidad.

### Ejemplo proxy match — Persona moral

```ts
import { classifyIdentifier } from "@/lib/identifier-classifier"

function buildEntityMatchBody(params: ScreeningParams, clientId: number, userId: number) {
  if (params.type === "organization") {
    return {
      clientId,
      userId,
      partyType: "organization",
      partyTypeLabel: "Persona moral",
      legalName: (params.orgName || "").trim(),
      rfc: (params.regNumber || "").trim() || undefined,
      minConfidence: 70,
    }
  }
  const id = (params.idNumber || "").trim()
  const classified = id ? classifyIdentifier(id) : null
  return {
    clientId,
    userId,
    partyType: "individual",
    partyTypeLabel: "Persona física",
    firstName: (params.firstName || "").trim(),
    lastName: (params.lastName || "").trim(),
    rfc: classified?.type.startsWith("rfc") ? classified.normalized : undefined,
    curp: classified?.type === "curp" ? classified.normalized : undefined,
    minConfidence: 70,
  }
}
```

Reemplazar en `_search-page-impl.tsx` el body actual de match que solo envía `name` + `rfc`/`curp` genéricos.

## Screening múltiple (Múltiples Sujetos)

### Decisiones cerradas (producto)

| Tema | Regla |
|------|--------|
| Entidades | **Un `entityId` por cada sujeto** (PF o PM del renglón) |
| PM padre | Si "Asociar a una empresa": crear/vincular entidad PM → `parentEntityId` en **todos** los miembros |
| Sin empresa padre | Correlación del lote con `groupId` + **`groupName`** (campo "Nombre del Grupo") |
| Con empresa padre | UI **oculta** "Nombre del Grupo"; correlación por **`parentEntityId`** (`groupName` opcional = razón social PM) |
| Reportes | **Un folio por sujeto** + **uno extra** si la PM se incluye en verificación |
| Naming | Unificar **`groupName`** en front y back (**eliminar `groupLabel`**) — decisión cerrada |
| Reportes | **Un folio por sujeto** + **uno por la PM** si `screenParent` |
| Sin empresa padre | `groupId` + `groupName` obligatorios para correlacionar el lote |
| Con empresa padre | `parentEntityId` en cada miembro; sin campo nombre de grupo en UI |

### UI (`searchType === "multiple"`)

| Elemento | Comportamiento |
|----------|----------------|
| **Asociar a una empresa** | PM: `parentName`, `parentRfc`; checkbox "Incluir empresa en la verificación" |
| **Nombre del Grupo** | Solo si `hasParent === false` → obligatorio para identificar el lote |
| **Sujeto N** | `nombre` = nombre completo; toggle **PF/PM**; **Rol / Relación**; `rfc_curp` |
| Estrella | Sujeto principal; si no hay `groupName`, puede copiar el nombre del principal |

### Renombrar `groupLabel` → `groupName` (refactor front)

Archivos a actualizar (buscar `groupLabel`):

- `lib/api-client.ts`, `lib/report-store.ts`, `lib/screening-jobs-store.ts`
- `app/searches/_search-page-impl.tsx`, `_apply-grok-analysis.ts`
- `components/screening/screening-results.tsx`, `_screening-loading-impl.tsx`
- `app/api/screening/*`, `app/api/db/reports/*`

```ts
// Antes
groupLabel: resolvedGroupLabel

// Después
groupName: resolvedGroupName
```

En `screening-search.tsx`, renombrar `resolvedGroupLabel` → `resolvedGroupName`:

```ts
const resolvedGroupName = groupName.trim()
  || (leadEntityIdx !== null ? subjects[leadEntityIdx]?.nombre.trim() : "")
  || (hasParent ? parentName.trim() : "")
  || ""
```

### Parseo de nombre completo (al llamar APIs)

```ts
function parseFullNameForApi(nombre: string, tipo: "individual" | "organization") {
  const trimmed = nombre.trim()
  if (tipo === "organization") {
    return { partyType: "organization" as const, partyTypeLabel: "Persona moral" as const, legalName: trimmed, fullName: trimmed }
  }
  const parts = trimmed.split(/\s+/).filter(Boolean)
  const firstName = parts[0] || ""
  const lastName = parts.slice(1).join(" ")
  return {
    partyType: "individual" as const,
    partyTypeLabel: "Persona física" as const,
    firstName,
    lastName,
    fullName: trimmed,
  }
}
```

### Flujo de entidades y reportes en grupo

**Fase A — PM padre (si `hasParent`)**

1. `match` + `create` entidad PM → `parentEntityId`.
2. Si `screenParent`: verificar PM → `save` con `entityId = parentEntityId`, `groupRole: "parent"`, sin `parentEntityId` en ese reporte.

**Fase B — Por cada sujeto del lote**

1. `match` + `create` con `fullName`, `partyType` del toggle PF/PM, `relationshipRole`, `parentEntityId` (si hay padre), `groupId` compartido.
2. Ejecutar pipeline de screening del sujeto.
3. **`save-report` → un folio por sujeto:**

```ts
await saveToNuwa({
  clientId, userId,
  entityId: subjectEntityId,
  parentEntityId: parentEntityId ?? undefined,
  groupId,
  groupName: hasParent ? parentName.trim() : resolvedGroupName,
  groupRole: subject.rol,
  relationshipRole: subject.rol,
  report: {
    folio: `${groupId}-${index}`,
    entidad: subject.nombre.trim(),
    tipoConsulta: subject.tipo === "individual" ? "Persona física" : "Persona moral",
    metadatos: {
      entityId: subjectEntityId,
      parentEntityId,
      groupId,
      groupName: hasParent ? parentName.trim() : resolvedGroupName,
      groupRole: subject.rol,
      relationshipRole: subject.rol,
      consultaModo: "multiple",
    },
  },
  searchContext: {
    consultaModo: "multiple",
    groupId,
    groupName: hasParent ? parentName.trim() : resolvedGroupName,
    parentEntityId,
    partyType: subject.tipo,
  },
})
```

**`buildNuwaReportPayload` + proxy `save-report`:** leer `groupId`, `groupName`, `groupRole`, `parentEntityId`, `entityId`, `relationshipRole` del body y pasarlos en la raíz del JSON a `POST /v1/reports/save`.

### Monitoreo en grupo

- Por sujeto: solo con `entityId` de ese sujeto ya creado.
- PM padre: monitoreo propio solo si se verificó y tiene `entityId`.

---

## Flujo `/searches` (screening)

1. **Antes de `/search`:** `POST /v1/entities/match` con cuerpo estructurado (arriba).
2. **Modal `EntityMatchModal`:**
   - "Usar existente" → guardar `linkedEntityId` en estado.
   - "Nueva entidad" → `POST /v1/entities/create` con los mismos campos + `partyTypeLabel`; manejar 409.
3. Ejecutar búsqueda (`/v1/search` vía `chunk-search` o `search-engine` según flujo).
4. Al guardar reporte (`reportsApi.save` / `save-report`):
   ```ts
   {
     clientId: numericClientId,
     userId: numericUserId,
     entityId: linkedEntityId, // UUID obligatorio si hubo alta o selección
     report: { ... buildNuwaReportPayload(), metadatos: { ... , entityId: linkedEntityId } },
     searchContext: {
       entityId: linkedEntityId,
       partyType: params.type,
       query: searchQuery,
       rfc: params.regNumber || params.idNumber,
     },
   }
   ```
5. **Monitoreo continuo:** solo si `monitoringEnabled` **y** `linkedEntityId` ya existe (crear/vincular entidad antes):
   ```ts
   POST /v1/entities/monitoring/upsert
   {
     clientId, userId,
     entityId,
     enabled: true,
     frequency: monitoringFrequency, // weekly|monthly|semi-annual|annual
     sources: monitoringSources,    // ["compliance","media"]
   }
   ```
   Dejar de depender de `monitoringStore` local como fuente de verdad cuando la API responda 200.

## Pantalla `/entities`

### Listado

- Reemplazar Supabase `listEntities` por proxy a `POST /v1/entities/list`.
- Mapear respuesta API → tipo `Entity` de `entities-table.tsx`:

| API (camelCase) | UI |
|-----------------|-----|
| `entityId` | `id` |
| `name` | `name` |
| `partyType` | `entityType` (`individual` \| `organization`) |
| `partyTypeLabel` | mostrar "Persona física" / "Persona moral" |
| `legalName` / `firstName`+`lastName` | columnas de detalle |
| `category` | `type` en pestañas (screening, employee, …) — **no** es PF/PM |
| `riskLevel` | `riskLevel` |
| `status` | `status` |
| `country` | `country` |
| `rfc` / `curp` | `identifier` |
| `lastScreeningAt` | `lastScreened` |
| `reportCount` | `reportCount` |

**Corregir bug actual:** dejar de hardcodear `type: "screening"` en `page.tsx`; usar `category` del API.

### Stats (`EntitiesStats`)

- Una sola llamada: `POST /v1/entities/stats` al montar la página.
- Mapear:
  - `totalEntities` → Total Entidades
  - `inReviewCount` → "X en revisión"
  - `individualsCount` / `individualsHighRisk` → widget Individuos
  - `organizationsCount` / `organizationsHighRisk` → widget Organizaciones
  - `highRiskTotal` / `highRiskPercent` → widget Riesgo Alto

### Detalle `/entities/[id]`

- `POST /v1/entities/get` con `{ clientId, userId, entityId }`.
- Mostrar `reports[]` del payload (folio, fecha, nivelRiesgo).
- Editar monitoreo: `monitoring` sub-objeto en get o upsert dedicado.

### Eliminar

- Confirmación UI → `POST /v1/entities/delete` con `entityId`.
- Ocultar filas con `status === 'deleted'` en list (el list API no las devuelve por defecto).

## Tipos TypeScript sugeridos (shared)

```ts
export type PartyType = "individual" | "organization"
export type EntityCategory =
  | "screening" | "background_check" | "employee" | "director"
  | "vendor" | "client" | "associate" | "pep" | "representative" | "beneficial_owner"
export type MonitoringFrequency = "weekly" | "monthly" | "semi-annual" | "annual"
export type MonitoringSource = "compliance" | "media"

export interface EntityMatchRequest {
  clientId: number
  userId: number
  partyType: PartyType
  partyTypeLabel: "Persona física" | "Persona moral"
  legalName?: string
  fullName?: string
  rfc?: string
  firstName?: string
  lastName?: string
  curp?: string
  minConfidence?: number
}

export interface SaveReportNuwaPayload {
  clientId: number
  userId: number
  entityId: string
  parentEntityId?: string
  groupId?: string
  groupName?: string
  groupRole?: string
  relationshipRole?: string
  report: Record<string, unknown>
  searchContext?: Record<string, unknown>
}

export interface EntitySummary {
  entityId: string
  name: string
  partyType: PartyType
  partyTypeLabel: "Persona física" | "Persona moral"
  category: EntityCategory
  relationshipRole?: string
  parentEntityId?: string
  fullName?: string
  rfc?: string
  curp?: string
  country?: string
  riskLevel?: string
  status: string
  reportCount: number
  lastScreeningAt?: string
  monitoring?: {
    enabled: boolean
    frequency?: MonitoringFrequency
    sources?: MonitoringSource[]
    nextRunAt?: string
  }
}
```

## Fuentes de monitoreo (UI existente)

Mantener IDs en front:

- `compliance` → etiqueta "Sanciones, PEPs y Regulatorio"
- `media` → "Medios Adversos"

No renombrar a `opensanctions` en payloads hacia Nuwa (el backend normaliza si recibe alias legacy).

## Criterios de aceptación front

- [ ] Match pre-search usa Nuwa con campos PF/PM estructurados (o `fullName` en múltiple).
- [ ] Todo reporte incluye `entityId`; múltiple = un folio por sujeto (+ PM si aplica).
- [ ] `groupLabel` eliminado; solo `groupName` hacia APIs y stores.
- [ ] Sin empresa padre: `groupName` enviado en cada save del lote.
- [ ] Con empresa padre: `parentEntityId` en entidades hijas y reportes de miembros.
- [ ] Rol / Relación: catálogo PF vs PM en los tres modos de consulta.
- [ ] `/entities` + stats desde Nuwa; pestañas por `category`.
- [ ] Monitoreo solo con `entityId` previo.
- [ ] `numericClientId`/`numericUserId` obligatorios tras login.

## OpenAPI

Contrato formal: `openapi/openapi.yaml` tag **Entities** y cambios en **Reports** (`entityId`).

## Pestañas `/entities` vs tipo PF/PM

- **PF/PM** → `partyType` + `partyTypeLabel` (viene del formulario de consulta).
- **Pestañas** (Screening, Empleados, Directores, …) → `category`; por defecto `screening` al crear desde consulta; editable en `/entities` update.

## Relaciones (columna tabla)

MVP: mostrar `0` o contador cuando exista API fase 2. El backend analizará cruces con documentos del `clientId` y consultas de otros usuarios.

## Decisiones cerradas (backend + producto)

- Match: ventana **30 días** (por entidad candidata).
- Create: **409** por RFC/CURP duplicado.
- Monitoreo: requiere **entityId** previo.
- Grupo: **Opción A** — un `entityId` por miembro; `parentEntityId` si hay PM padre.
- Reportes múltiples: **un folio por sujeto** + folio PM si `screenParent`.
- Naming: **`groupName`** único (no `groupLabel`).
