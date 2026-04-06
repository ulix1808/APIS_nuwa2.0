# API desplegada — pruebas manuales (smoke)

Región **us-east-1**. La URL canónica sale del output de CloudFormation **`ApiBaseUrl`** del stack **`Nuwa2ApiStack-<environment>`** (stage **`prod`**).

## URL base actual (referencia)

| Entorno | Base URL (ejemplo despliegue) |
|--------|-------------------------------|
| prod   | `https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod/` |

Tras un **redeploy** que cree otro API Gateway, el host puede cambiar: vuelve a leer **`ApiBaseUrl`** en la consola CloudFormation o con:

```bash
aws cloudformation describe-stacks \
  --stack-name Nuwa2ApiStack-prod \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" \
  --output text
```

## Antes de dar por “aprobado” el login

1. **Secrets Manager `nuwa2/prod/database`** — JSON válido: `host`, `port`, `dbname`, `user`, `password`, `sslmode` (p. ej. `require`). La instancia RDS debe ser **alcanzable desde las Lambdas** (si la Lambda no está en VPC, RDS suele necesitar acceso público + SG con permiso desde rangos de Lambda, o mover Lambda a VPC).
2. **`nuwa2/prod/app-crypto`** — `jwt_signing_secret` (≥32 caracteres) y `fernet_key` (Fernet) propios; no placeholders.
3. **Migraciones** aplicadas en esa base (tablas `nuwa_users`, `companies`, etc.).
4. Usuario admin: tras migración **`20260406160000_nuwa_user_nuwa_space.sql`** → **`nuwa@nuwa.space`**, **`clientId` 1**, **`userId` 1**, contraseña inicial de prueba **`nuw4sp4c3`** (solo dev/QA; **rotar en producción**). Sin esa migración sigue válido el seed RBAC: `admin@nuwa.local` / `ChangeMe!`.

## Smoke automatizado — `scripts/smoke_api.sh`

Script de bash en la **raíz del repo** (`APIs/scripts/smoke_api.sh`). Ejecutarlo **desde `APIs/`** (ruta `./scripts/smoke_api.sh`).

### Requisitos

- **curl**
- **python3** (construye JSON del login de forma segura y parsea respuestas; no hace falta `jq`)
- Permiso de ejecución: `chmod +x scripts/smoke_api.sh` (si hiciera falta)
- API desplegada con el **CDK actual del repo** (incluye **`POST`** en `/v1/reports/get`). Si no has desplegado tras ese cambio, ese paso del smoke puede responder **403** (*Missing Authentication Token*).

### Comportamiento por defecto (solo lectura en negocio)

1. `POST /v1/auth/login` (salvo que pases token; ver tabla).
2. Con `Authorization: Bearer <accessToken>`:
   - `POST /v1/reports/get?clientId=<cid>&limit=5` con body `{}` (mismo comportamiento que `GET`; el smoke usa **POST** para evitar entornos donde `Authorization` no llega en `GET`)
   - `POST /v1/sources/list`
   - `POST /v1/search` (body con `query: "nuwa"`, `limit: 5`)
   - `POST /v1/admin/companies/list`
   - `POST /v1/admin/roles/list`
   - `POST /v1/admin/users/list`

Esas rutas **no insertan ni actualizan** datos de producto en Postgres (solo consultas). Pueden generar logs/métricas en AWS como cualquier llamada a la API.

Si algún paso devuelve un código HTTP fuera de **2xx**, el script termina con error y muestra el cuerpo recortado de la respuesta.

### Uso con login

Define la base **sin barra final** (mismo criterio que `BASE` en los ejemplos con curl).

```bash
cd /ruta/a/APIs
export NUWA_API_BASE="https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod"
export NUWA_EMAIL="nuwa@nuwa.space"
export NUWA_PASSWORD="..."   # no commitear; preferir variable en sesión
./scripts/smoke_api.sh
```

Si el mismo correo existe en varias compañías, opcional:

```bash
export NUWA_CLIENT_ID=1
./scripts/smoke_api.sh
```

(`NUWA_CLIENT_ID` se envía en el body del login como `clientId`.)

### Uso con token ya obtenido (sin contraseña en el script)

Útil para repetir pruebas sin exponer el password en el entorno:

```bash
export NUWA_API_BASE="https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod"
export NUWA_ACCESS_TOKEN="eyJ..."
export NUWA_USER_ID=1          # claim JWT `sub`
export NUWA_CLIENT_ID_BODY=1   # claim JWT `cid` (compañía)
./scripts/smoke_api.sh
```

### Modo escritura (`--write`)

```bash
./scripts/smoke_api.sh --write
```

Además del flujo anterior, crea datos de prueba en BD: una fuente pública `smoke-source-<timestamp>`, un chunk vía `POST /v1/chunks/ingest` y un reporte mínimo con `POST /v1/reports/save` (folio `SMOKE-<timestamp>`). **No usar en producción** salvo que aceptes basura en catálogo/reportes.

### Variables de entorno (resumen)

| Variable | Obligatoria | Descripción |
|----------|-------------|-------------|
| `NUWA_API_BASE` | Sí | URL base del API Gateway + stage (ej. `.../prod`), sin `/` final |
| `NUWA_EMAIL` | Si no hay token | Email del login |
| `NUWA_PASSWORD` | Si no hay token | Contraseña del login |
| `NUWA_CLIENT_ID` | No | `clientId` en el body del login (desambiguación) |
| `NUWA_ACCESS_TOKEN` | Alternativa a login | JWT `accessToken` |
| `NUWA_USER_ID` | Con token | Debe coincidir con `sub` del JWT |
| `NUWA_CLIENT_ID_BODY` | Con token | Debe coincidir con `cid` del JWT |
| `NUWA_SMOKE_USE_PROXY` | No | `1` = no usar `--noproxy '*'` (solo si debes enrutar por `HTTP(S)_PROXY`) |

### Proxy HTTP y 401 solo en GET

Si el **login devuelve 200** pero **`GET /v1/reports/get`** responde **401** con *“Se requiere Authorization: Bearer”*, suele ser un **proxy** o cliente que **no reenvía `Authorization` en GET**. El smoke usa **`POST /v1/reports/get?...`** con body `{}` (mismo contrato que GET; requiere **stack CDK desplegado** con ese método). El script sigue usando **`curl --noproxy '*'`** por defecto. Si debes usar proxy:

```bash
NUWA_SMOKE_USE_PROXY=1 ./scripts/smoke_api.sh
```

### Ayuda en terminal

```bash
./scripts/smoke_api.sh --help
```

Muestra un resumen de uso (comentarios de cabecera del script).

## Comprobación rápida con curl

Sustituye `BASE` si tu `ApiBaseUrl` es distinta.

```bash
BASE="https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod"
```

### 1. Login (sin `Authorization`)

**Éxito esperado:** HTTP **200** y JSON con `accessToken`, `user`, `company`, etc.

```bash
curl -sS -X POST "$BASE/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"nuwa@nuwa.space","password":"nuw4sp4c3","clientId":1}'
```

(Si no aplicaste `20260406160000_nuwa_user_nuwa_space.sql`, usa `admin@nuwa.local` / `ChangeMe!`.)

Para encadenar varias rutas con un solo comando, usa el smoke automatizado descrito arriba: **`scripts/smoke_api.sh`**.

### 2. Ruta protegida (JWT) manual

Copia `accessToken` de la respuesta anterior.

```bash
TOKEN="PEGAR_accessToken"

curl -sS "$BASE/v1/reports/get?clientId=1&limit=5" \
  -H "Authorization: Bearer $TOKEN"
```

## Si obtienes HTTP 502

- Revisa **CloudWatch Logs** del grupo de la Lambda **`nuwa2-us-east-1-prod-lambda-auth`** (u otra según la ruta).
- Causas frecuentes: fallo al leer secretos, timeout a RDS, TLS, credenciales incorrectas, tablas inexistentes.

## Criterios de aprobación (QA)

| # | Prueba | Criterio |
|---|--------|----------|
| 1 | `POST /v1/auth/login` con seed | 200 y `accessToken` presente |
| 2 | `GET /v1/reports/get?clientId=1` con Bearer | 200 o 200 con lista vacía (no 401/502) |
| 3 | Login con contraseña incorrecta | 401 u error de negocio documentado |
| 4 | `./scripts/smoke_api.sh` (sin `--write`) | Termina OK; todos los pasos 2xx |

## Contrato OpenAPI

Especificación: `openapi/openapi.yaml` (mismos paths bajo la base URL).

## Desarrollo local sin AWS

Ver **`docs/LOCAL_DEV.md`** (Postgres en Docker + variables `NUWA_DATABASE_CONFIG_JSON` / `NUWA_APP_CRYPTO_CONFIG_JSON`).
