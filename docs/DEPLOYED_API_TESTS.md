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

### 2. Ruta protegida (JWT)

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

## Contrato OpenAPI

Especificación: `openapi/openapi.yaml` (mismos paths bajo la base URL).

## Desarrollo local sin AWS

Ver **`docs/LOCAL_DEV.md`** (Postgres en Docker + variables `NUWA_DATABASE_CONFIG_JSON` / `NUWA_APP_CRYPTO_CONFIG_JSON`).
