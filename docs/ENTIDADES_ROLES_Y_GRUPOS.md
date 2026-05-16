# Roles, screening múltiple y grupos

Complemento de `PLAN_ENTIDADES.md` con lo validado en el front (`screening-search.tsx`, `_search-page-impl.tsx`).

## 1. Catálogos Rol / Relación (distintos por PF y PM)

| partyType | Etiqueta UI | Valores (`relationshipRole`) |
|-----------|-------------|------------------------------|
| `individual` | **Rol / Relación** | `Accionista`, `Apoderado`, `Rep. Legal`, `Consejero`, `Beneficiario Final`, `Comisario`, `Otro` |
| `organization` | **Rol / Relación** (hoy dice "Tipo de Relación" en PM — unificar label) | `Cliente`, `Proveedor`, `Contraparte`, `Subsidiaria`, `Matriz`, `Otro` |

**Front (corrección UI):**
- PM: cambiar label `Tipo de Relacion` → `Rol / Relacion`.
- Múltiples sujetos: el dropdown `Rol` debe usar `ROLE_OPTIONS` si `tipo === PF` y `ORG_ROLE_OPTIONS` si `tipo === PM` (hoy siempre usa `ROLE_OPTIONS` — bug).

**Backend:** validar `relationshipRole` contra el catálogo según `partyType` (400 si no aplica).

---

## 2. Modos de consulta (Step 1)

| Modo | Campos identidad | Match / create |
|------|------------------|----------------|
| Persona física | `firstName`, `lastName`, `idNumber` → rfc/curp | Estructurado |
| Persona moral | `legalName`, `rfc` | Estructurado |
| **Múltiples sujetos** | Por fila: `nombre` (completo), toggle **PF/PM**, `rfc_curp`, `rol` | Ver §3 |

---

## 3. Screening múltiple

### 3.1 Identificación del batch (decisión producto)

| Modo UI | Campo visible | Correlación en API/DB |
|---------|---------------|----------------------|
| **Sin** "Asociar a una empresa" | **Nombre del Grupo** → `groupName` | `groupId` + `groupName` en cada reporte/miembro; `parentEntityId` null |
| **Con** "Asociar a una empresa" | No hay input de nombre de grupo (se oculta en UI) | `parentEntityId` = entidad PM padre; `groupId` compartido; `groupName` opcional (= razón social PM) |

Cada **miembro** tiene su propio `entityId`. Si hay PM padre, todos los miembros llevan `parent_entity_id` apuntando a esa entidad.

### 3.2 Asociar a una empresa

- Toggle + razón social + RFC de la PM.
- Opción **Incluir empresa en la verificación** (`screenParent`).
- La PM padre debe tener (o crearse) `entityId` (`partyType=organization`).
- Los sujetos del grupo referencian `parentEntityId` = entity de esa PM.

### 3.3 Por sujeto (fila)

| Campo UI | Uso |
|----------|-----|
| Nombre completo | PF: parsear a first/last (primer token / resto) o guardar `full_name`. PM: `legal_name` = texto completo. |
| Tipo PF/PM | Define catálogo de rol y campos de match. |
| Rol | `relationshipRole` en entidad y `groupRole` en reporte del sujeto. |
| RFC/CURP | Clasificar y guardar en `rfc` o `curp`. |

### 3.4 Reportes en screening múltiple

- **Un folio/reporte por cada sujeto** verificado (cada uno con su `entityId`).
- **Un folio adicional** para la PM padre si "Incluir empresa en la verificación" está activo (`groupRole: parent`).

Campos en `POST /v1/reports/save` (nombre unificado **`groupName`**, no `groupLabel`):

- `groupId` — ej. `GRP-1740…`
- `groupName` — nombre del grupo o razón social PM
- `groupRole` — rol del miembro o `parent` para la empresa
- `parentEntityId`, `entityId`, `relationshipRole`

El front debe **renombrar** `groupLabel` → `groupName` en tipos, store y proxies.

---

## 4. Cambios API y esquema

### 4.1 `POST /v1/reports/save` (modificar)

Raíz del body (además de `entityId`):

| Campo | Descripción |
|-------|-------------|
| `parentEntityId` | UUID de la PM cuando el grupo está asociado a empresa |
| `groupId` | Id lógico del batch (`GRP-…`) |
| `groupName` | Nombre del Grupo (sin PM padre) o razón social PM (opcional con padre) |
| `groupRole` | Rol del sujeto en el grupo / relación |

En `report.metadatos` (duplicado para UI que solo lee JSON):

```json
{
  "entityId": "uuid",
  "parentEntityId": "uuid",
  "groupId": "GRP-…",
  "groupName": "Caso Rodriguez",
  "groupRole": "Accionista",
  "relationshipRole": "Accionista"
}
```

Columnas SQL en `reports`: `entity_id`, `parent_entity_id`, `group_id`, `group_name`, `group_role`.

`search_context` incluye el mismo bloque + `consultaModo: "multiple" | "individual" | "organization"`.

### 4.2 `POST /v1/entities/match` (modificar)

Aceptar alternativa **`fullName`** (múltiples sujetos):

- Si `partyType=organization` → tratar como `legalName`.
- Si `partyType=individual` → normalizar `fullName`; opcional split interno para fuzzy.

Sigue aplicando ventana **30 días**.

### 4.3 `POST /v1/entities/create` (modificar)

| Campo nuevo | Uso |
|-------------|-----|
| `fullName` | Múltiples sujetos (si no vienen first/last o legalName) |
| `relationshipRole` | Valor del dropdown Rol |
| `parentEntityId` | PM padre del grupo |
| `groupId` | Correlación con batch (metadata) |

### 4.4 `POST /v1/entities/list` / `get` (modificar)

Devolver `relationshipRole`, `parentEntityId`, `fullName`, y en get opcional `childEntities[]` / `groupReports[]` (fase 2).

### 4.5 Nuevas rutas (opcional MVP+)

| Ruta | Uso |
|------|-----|
| `POST /v1/entities/group/create` | Crear PM padre + N miembros en una transacción (fase 2) |
| `GET` vía list filtrado `parentEntityId` | Listar sujetos de una empresa |

MVP: crear entidades una a una desde el front con el mismo `parentEntityId` y `groupId`.

### 4.6 Sin cambio de contrato

- `POST /v1/search` — sigue por query/RFC; el front ya arma `firstName`/`orgName` desde `nombre` completo.

---

## 5. Modelo de vinculación (cerrado)

**Opción A — un `entityId` por sujeto + PM padre opcional**

- Cada PF/PM del lote: fila en `entities` + su reporte.
- Con empresa asociada: `entities.parent_entity_id` → PM; reportes con `entity_id` del sujeto y `parent_entity_id` de la PM.
- Sin empresa: solo `groupId` + `groupName` para agrupar reportes en UI/auditoría.

---

## 6. Relaciones (columna tabla /entities)

No confundir con `relationshipRole` del formulario.

La columna **Relaciones** del listado = análisis futuro: documentos del `clientId`, consultas de otros `userId`, miembros de grupo. API dedicada en fase 2 (`POST /v1/entities/relations`).
