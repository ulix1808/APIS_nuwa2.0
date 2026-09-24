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

## Usuarios plataforma (`/v1/admin/users/*`)

### POST /v1/admin/users/list

**Dos modos:**

| Actor | Body | Comportamiento |
|-------|------|----------------|
| `super_admin` | Sin `targetClientId` | Lista plataforma (`search`, `role`, `clientId`, `status`) |
| `admin` / con `targetClientId` | Tenant | Lista usuarios de una compañía (legacy) |

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
  "clientId": 1
}
```

(`clientId` del tenant objetivo va en el campo homónimo del body de invite; el actor sigue llevando su `clientId`/`userId`.)

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

Con `super_admin` + `targetUserId`: actualiza `full_name`, `role` (app slug), `status` (`active` | `disabled` | `invited`).

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
