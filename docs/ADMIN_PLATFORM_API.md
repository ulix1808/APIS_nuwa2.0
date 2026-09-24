# Admin Panel — API plataforma (v1)

Endpoints para el Panel Admin Nuwa (`/v2/admin` en el BFF). Implementados en **`handler_admin.py`** + **`nuwa_admin_platform_pg.py`**. Desplegados en la Lambda **`nuwa2-*-lambda-admin`**.

**Auth:** `Authorization: Bearer <JWT>` + body con `clientId` / `userId` alineados al token (`sub`, `cid`).  
**Rol:** solo **`super_admin`** (claim + `nuwa_roles.slug` en BD).

El BFF envía el correo (SES); estas rutas **no** llaman a SES.

---

## Tablas RDS

| Tabla | Uso |
|-------|-----|
| `companies` | Tenant (`client_id`, `name`) |
| `clients` | Metadatos admin (plan, tokens, RFC, status) |
| `client_token_usage` | Desglose de consumo |
| `nuwa_users` / `nuwa_roles` | Usuarios y RBAC |

Migración BFF (aplicar en RDS): `b_CvPklPEoxLx/scripts/023_admin_clients_platform.sql`

---

## Clientes (`/v1/clients/*`)

Todas requieren `super_admin`.

### POST /v1/clients/list

Body:

```json
{
  "clientId": 1,
  "userId": 1,
  "search": "nuwa",
  "status": "active",
  "limit": 200,
  "offset": 0
}
```

Response:

```json
{
  "success": true,
  "clients": [{ "id": 1, "name": "Nuwa", "tokenLimit": 2000, "operatingCountries": ["mexico"], "users": [], "usage": {} }],
  "stats": {
    "totalClients": 2,
    "activeClients": 2,
    "totalTokensAllocated": 4000,
    "totalTokensConsumed": 0
  }
}
```

### POST /v1/clients/get

Body: `{ "clientId", "userId", "targetClientId" }` → `{ "success", "client", "users" }`

### POST /v1/clients/create

Body: `name`, `rfc` (requeridos), opcionales `legalRep`, `address`, `sector`, `plan`, `tokenLimit`, `billingContact`, `billingEmail`, `paymentMethod`, `operatingCountries`.

`operatingCountries` es un arreglo de `mexico`, `colombia`, `costarica`, `guatemala` (uno o más, sin repetir). Si no viene, queda `["mexico"]`. La columna es `clients.operating_countries` (`TEXT[]`, default `ARRAY['mexico']`, script BFF `028_client_operating_countries.sql`). `get`, `list` y `update` devuelven el mismo campo. En `update`, omitirlo no lo cambia; un valor inválido responde 400.

Crea fila en `companies` + `clients` + `client_token_usage`. El BFF debe llamar después a `POST /v1/clients/storage/init`.

### POST /v1/clients/update

Body: `targetClientId` + campos parciales (`name`, `rfc`, `tokenLimit`, …).

### POST /v1/clients/suspend | /reactivate | /delete

Body: `{ "targetClientId" }`. Suspend desactiva `nuwa_users.is_active` del tenant.

### POST /v1/clients/tokens/reset-usage

Body: `{ "targetClientId" }` → pone `clients.tokens_used = 0`.

---

## Tokens del tenant (`/v1/tokens/*`)

Cualquier usuario con JWT. El saldo es el de `clientId` del actor. `super_admin` puede pasar `targetClientId` para otra empresa. El cobro es idempotente por `client_id` + `refFolio`.

### POST /v1/tokens/balance

Body: `clientId`, `userId` del actor. Respuesta: `{ "success", "limit", "used", "remaining" }`.

### POST /v1/tokens/ledger

Body: lo anterior y opcional `limit` (1–200, default 50). Respuesta: `{ "success", "items": [{ "id", "cost", "action", "label", "timestamp", "refFolio"? }] }`.

### POST /v1/tokens/consume

Body: `cost` (entero > 0), `action` (`screening` | `background_check` | `monitoring` | `rescreen` | `other`), opcional `label` y `refFolio`.

- 200 `{ "success": true, "ok": true, "alreadyCharged": false, "limit", "used", "remaining" }` si descontó.
- 200 con `alreadyCharged: true` si ese `refFolio` ya estaba en `token_ledger` (no vuelve a descontar).
- 409 `{ "success": false, "code": "insufficient_tokens", "limit", "used", "remaining" }` si no alcanza.
- 400 `invalid_cost` o `invalid_action`.

---

## Usuarios plataforma (`/v1/admin/users/*`)

### POST /v1/admin/users/list

**Dos modos:**

| Actor | Body | Comportamiento |
|-------|------|----------------|
| `super_admin` | Sin `targetClientId` | Todos los usuarios. El `clientId` del actor no filtra. |
| `super_admin` | Con `targetClientId` | Solo esa empresa. Misma forma `{ users }`. |
| otro rol | | Lista legacy de su compañía |

Response plataforma: `{ "success": true, "users": [{ "id", "email", "name", "role", "status", "clientId", "companyName" }] }`

Roles en respuesta (app): `master`, `admin`, `compliance_officer`, `analyst`, `viewer` mapeados desde `nuwa_roles.slug`.
Invite/update resuelven `role_id` por slug en BD (no IDs fijos); migraciones `20260406120000_*` + `20260915000000_nuwa_roles_extended.sql`.

### POST /v1/admin/users/invite

Body:

```json
{
  "clientId": 1,
  "userId": 1,
  "email": "uli@nuwa.space",
  "name": "Uli",
  "role": "analyst",
  "targetClientId": 2
}
```

`clientId` / `userId` son el actor (el master). La empresa del usuario nuevo es `targetClientId`. Si no viene, se usa el `clientId` del actor. El alta deja `must_change_password = true`.

Response **201**:

```json
{
  "success": true,
  "user": { "id": 42, "email": "...", "status": "invited", "role": "analyst" },
  "tempPassword": "…"
}
```

Contraseña hasheada con **`pbkdf2_sha256`** (`nuwa_password.hash_password`). El BFF envía el mail; **no loguear** `tempPassword` en CloudWatch.

Errores: `409 email_exists`, `403` si no es super_admin.

### POST /v1/admin/users/resend-invite

Body: `{ "targetUserId" }` → regenera contraseña (mismo contrato que reset).

### POST /v1/admin/users/reset-password

Body: `{ "targetUserId" }` → `{ "success", "user", "tempPassword" }`.

### POST /v1/admin/users/update

Con `super_admin` + `targetUserId`: actualiza `full_name`, `role` (app slug), `status` (`active` | `disabled` | `invited`) y, si viene `targetClientId`, mueve `nuwa_users.client_id` a esa empresa.

### POST /v1/admin/users/delete

`super_admin`: borra la fila (`DELETE`). Antes suelta o reasigna las llaves foráneas que apuntan a ese usuario; el respaldo es el `userId` del actor. Otro rol solo desactiva (`is_active = false`).

---

## Directorio del tenant (`POST /v1/team/users/list`)

Cualquier usuario con JWT (analista, oficial, admin, viewer) lista los usuarios de **su** empresa. Sirve para compartir un reporte y escalar. No lista otra compañía: si `targetClientId` viene y no es la del actor, responde 403.

`v1/admin/users/list` sigue siendo de administración. Un analista ahí recibe 403.

Body: `clientId` y `userId` del actor. Opcionales `search`, `status`, `role`.

Response: el mismo `{ "success": true, "users": [...] }` que el listado de plataforma (sin hash de contraseña).

---

## Apagar `must_change_password` (`POST /v1/auth/password/change`)

Lambda **auth**. Requiere `Authorization: Bearer`. El usuario es el `sub` del token; el body no elige otra cuenta.

Body: `{ "currentPassword", "newPassword" }`. `newPassword` mínimo 8 caracteres.

Actualiza `password_hash` y deja `must_change_password = false`. El login no hace este cambio.

Response **200:** `{ "success": true, "mustChangePassword": false }`.

401 si la contraseña actual no coincide. 400 si falta o es corta.

---

## Pruebas

```bash
# Unit tests (sin AWS)
cd APIs && python -m pytest tests/test_admin_platform_pg.py tests/test_handler_admin_platform.py -q

# Smoke contra prod (JWT super_admin)
export NUWA_API_BASE=https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
export NUWA_EMAIL=nuwa@nuwa.space NUWA_PASSWORD=...
./scripts/smoke_api.sh
```

OpenAPI: `openapi/openapi.yaml` (tag **AdminPlatform**).

Contrato BFF: `b_CvPklPEoxLx/docs/ADMIN_PANEL_API_SPEC.md`.
