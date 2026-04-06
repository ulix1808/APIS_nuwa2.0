# Nuwa 2.0 — AWS CDK (Python)

Guía ampliada de despliegue, contextos `-c`, VPC/RDS y errores frecuentes: **`../docs/CDK_AWS.md`**.

Despliega en **us-east-1**:

- **API Gateway** REST (`nuwa2-us-east-1-<env>-api`, stage `prod`)
- **6 Lambdas** Python 3.12: sources, chunks ingest, search, reports, admin, auth (login sin API Key)
- **Usage plan** + API keys (plataforma y por tenant, M2M/cuotas AWS)
- **Secreto `app-crypto`** (JWT HS256 + Fernet para cifrar `apigw_key_secret` en BD)
- **JWT** en `Authorization: Bearer` para rutas de negocio (`api_key_required=false` en esos métodos)
- **SSM Parameter** `/nuwa2/<env>/supabase/url` — URL del proyecto (editable sin redeploy de código)
- **Secrets Manager** `nuwa2/<env>/supabase-service-role-key` — JWT `service_role` (pegar desde Supabase → Settings → API)

No hace falta hostname/puerto de Postgres en la Lambda si usas la **API REST de Supabase** con ese JWT. Si más adelante usas conexión directa a Postgres, añade otro secreto (connection string) y lectura en `nuwa_config.py`.

## Prerrequisitos locales

- Python **3.9+** (3.12 recomendado; alineado con GitHub Actions)
- Node.js **18+** (solo para el CLI `cdk` vía `npx`)
- Cuenta AWS y credenciales (`aws configure` o variables de entorno)
- Una vez por cuenta/región: **bootstrap**

```bash
cd cdk
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

npx aws-cdk@2.170.0 bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1
```

## Sintetizar plantilla (sin desplegar)

```bash
cd cdk
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=123456789012
export CDK_DEFAULT_REGION=us-east-1
npx aws-cdk@2.170.0 synth --app "python3 app.py"
```

## Desplegar

```bash
cd cdk
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

# Opcional: URL inicial (si no, queda el placeholder y la editas en SSM)
npx aws-cdk@2.170.0 deploy --all --app "python3 app.py" -c environment=prod -c supabaseUrl=https://xxxx.supabase.co
```

**PostgreSQL en RDS (modo directo `psycopg`):** antes del deploy ejecuta `./scripts/bundle_lambda_deps.sh` (ruedas Linux para Lambda). Despliega con **`-c useDatabase=true`** para inyectar `NUWA_DATABASE_SECRET_ARN` y rellena el secreto `nuwa2/<env>/database` (JSON). Detalle: `docs/RDS_LAMBDA.md`.

```bash
# ejemplo
./scripts/bundle_lambda_deps.sh
cd cdk && npx aws-cdk@2.170.0 deploy --all --app "python3 app.py" -c environment=prod -c useDatabase=true
```

Tras el deploy:

1. **Probar la API:** guía con URL base, curl y criterios de aprobación → **`docs/DEPLOYED_API_TESTS.md`**. El output **`ApiBaseUrl`** es la fuente de verdad si cambia el host.
2. **SSM** → parámetro `/nuwa2/prod/supabase/url` → confirma la URL HTTPS del proyecto.
3. **Secrets Manager** → secreto `nuwa2/prod/supabase-service-role-key` → *Store a new secret value* → pega el JWT `service_role` (texto plano) o JSON `{"service_role_key":"eyJ..."}`.
4. **Secrets Manager** → `nuwa2/<env>/app-crypto` → sustituye el JSON por valores propios: `jwt_signing_secret` (≥32 chars) y `fernet_key` (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
5. **Login** `POST .../v1/auth/login` → usa `accessToken` como `Authorization: Bearer ...` en el resto de llamadas. Operaciones admin requieren usuario `super_admin` con ese JWT.

## URL base, rutas y CORS (front en otro dominio)

Hay **un solo API Gateway REST** y **una URL base** por entorno (output CloudFormation **`ApiBaseUrl`**, p. ej. `https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod/`). El stage es **`prod`**.

- **Enrutamiento:** API Gateway enruta por **path + método HTTP**. Cada ruta (`/v1/sources/list`, `/v1/search`, `/v1/reports/get`, …) está asociada a **una Lambda** concreta en CDK (`nuwa_api_stack.py`). El front **no** llama a las Lambdas directamente: solo al host del API GW con el path correcto.
- **Misma base URL:** todas las operaciones son `ApiBaseUrl + path`, por ejemplo `POST .../prod/v1/search`, `GET .../prod/v1/reports/get?...`.
- **CORS:** el **preflight `OPTIONS`** lo atiende API Gateway (`default_cors_preflight_options`: orígenes `*`, métodos y cabeceras incluyendo `X-Api-Key`). Con integración **Lambda proxy**, el navegador también necesita cabeceras CORS en la **respuesta real** de cada método; las Lambdas las devuelven vía `nuwa_http.py` (`Access-Control-Allow-Origin: *`, etc.). Contrato detallado de paths: **`openapi/openapi.yaml`** y **`docs/API_AND_ARCHITECTURE.md`** (§ API / búsqueda).
- **Credenciales:** login sin Bearer; después **`Authorization: Bearer <accessToken>`** en sources/chunks/search/reports/admin. Rotar `app-crypto` en producción.

## Versiones CDK

`requirements.txt` fija **`aws-cdk-lib==2.170.0`**. Usa el mismo major en CI: `npx aws-cdk@2.170.0`. Si subes la librería, sube también el CLI en `npx` y en `.github/workflows/cdk-deploy.yml`.

## GitHub Actions

Workflow: `.github/workflows/cdk-deploy.yml`.

Secret obligatorio con OIDC: **`AWS_ROLE_TO_ASSUME`** (ARN del rol IAM).

Sin OIDC: sustituye el paso *AWS credentials* por `aws-access-key-id` / `aws-secret-access-key` (no recomendado a largo plazo).

**No pegues** access keys en el chat ni las commitees; usa secrets del repositorio.

## Qué falta implementar en código

- Handlers en `cdk/lambdas/handler_*.py`: llamadas HTTP a Supabase (PostgREST) o cliente oficial.
- Reglas de negocio (admin `clientId=1` siempre público, etc.) en la Lambda de sources.

## OpenAPI

Contrato HTTP: `../openapi/openapi.yaml` (rutas alineadas con este stack).

## Seguridad (JWT, RBAC, diagramas)

Descripción amplia, modelo de amenazas y diagramas Mermaid: **`../docs/API_AND_ARCHITECTURE.md`** (sección 1 y subsecciones de diagramas).
