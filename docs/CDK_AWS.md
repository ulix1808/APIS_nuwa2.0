# CDK y despliegue en AWS (Nuwa APIs)

Guía operativa para **sintetizar**, **desplegar** y **evitar errores** con el stack **CDK Python** (`cdk/app.py` → `Nuwa2ApiStack-<environment>`).

Documentación relacionada:

- **`cdk/README.md`** — visión del stack, CORS, rutas, CI.
- **`docs/RDS_LAMBDA.md`** — Postgres en RDS, secreto JSON, VPC, endpoints.
- **`docs/DEPLOYED_API_TESTS.md`** — pruebas tras deploy (curl y **`scripts/smoke_api.sh`**).
- **`docs/API_AND_ARCHITECTURE.md`** — arquitectura y seguridad.
- **`scripts/cdk_deploy_hint.sh`** — resumen de cambios con git y bloque listo para copiar (`deploy` + perfil AWS); detalle en §6.

---

## 1. Prerrequisitos

| Requisito | Notas |
|-----------|--------|
| **Python 3.9+** | El `app.py` usa `from __future__ import annotations` para compatibilidad. En local, `cdk/.venv` con `pip install -r cdk/requirements.txt`. |
| **Node.js 18+ o 22** | CDK/JSII avisan con Node 25; usa 22 LTS o `export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1`. |
| **AWS CLI v2** | `aws sts get-caller-identity` debe funcionar. Con SSO: `aws sso login --profile TU_PERFIL`. |
| **Bootstrap CDK** | Una vez por cuenta/región: `npx aws-cdk@2.170.0 bootstrap aws://CUENTA/us-east-1`. |
| **Versión CDK** | Alineada con `cdk/requirements.txt`: **`aws-cdk-lib==2.170.0`** y **`npx aws-cdk@2.170.0`**. |

---

## 2. Primera configuración local

Desde la raíz del monorepo (`APIs/`):

```bash
cd cdk
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Variables típicas antes de `synth` / `deploy`:

```bash
# Solo si usas un perfil nombrado en ~/.aws/config (nombre real, no un placeholder):
# export AWS_PROFILE=nuwa-prod
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1
```

---

## 3. Contexto CDK (`-c`): qué significa cada flag

Se pasan a `npx aws-cdk ... -c clave=valor`. Los lee `cdk/app.py`.

| Contexto | Ejemplo | Efecto |
|----------|---------|--------|
| `environment` | `prod` | Nombre del stack: `Nuwa2ApiStack-prod`, prefijos `nuwa2-us-east-1-prod`, secretos `nuwa2/prod/...`. Default: `prod`. |
| `supabaseUrl` | `https://xxx.supabase.co` | Placeholder inicial; la URL “viva” suele ir en SSM `/nuwa2/<env>/supabase/url`. |
| `useDatabase` | `true` | Modo **Postgres directo** (`psycopg`): inyecta `NUWA_DATABASE_SECRET_ARN`, etc. |
| `reuseAllExternalSecrets` | `true` | Equivale a reutilizar secretos **database**, **supabase** y **app-crypto** ya creados (`from_secret_name_v2`). |
| `reuseDatabaseSecret` / `reuseSupabaseSecret` / `reuseAppCryptoSecret` | `true` | Igual que arriba pero por separado. |
| **`rdsVpcId`** | `vpc-0dc24fcb6dec4f5db` | VPC donde vive RDS y donde se colocan las Lambdas (lookup). |
| **`lambdaSubnetIds`** | `subnet-a,subnet-b` | Subnets para las Lambdas (mejor **2 AZ**). **Poner entre comillas** en shell: `-c "lambdaSubnetIds=subnet-1,subnet-2"`. |
| **`rdsSecurityGroupId`** | `sg-02664d9e7ccd46830` | SG de la instancia RDS; el stack añade **entrada 5432** desde el SG de las Lambdas. |

### Regla crítica: `useDatabase` + VPC

Si `useDatabase=true` y defines **cualquiera** de `rdsVpcId`, `lambdaSubnetIds` o `rdsSecurityGroupId`, **los tres son obligatorios**. Si falta alguno, **`cdk synth` falla** con un `ValueError` explícito.

Si `useDatabase=true` y **no** pasas ninguno de los tres, el stack se sintetiza **sin** VPC en las Lambdas (modo que no sirve para RDS privado y **no** crea interface endpoints).

**No despliegues** `useDatabase=true` **sin** los tres parámetros de red si ya desplegaste antes con VPC: la plantilla **quitaría** VPC endpoints, SG y configuración de red, y CloudFormation **borraría** esos recursos.

---

## 4. Dependencias de Lambda (Linux)

Las Lambdas usan **Python 3.12** en **x86_64**. Paquetes nativos (`psycopg-binary`, etc.) deben ser ruedas **manylinux**.

Desde la **raíz del repo** (no desde `cdk/`):

```bash
./scripts/bundle_lambda_deps.sh
```

Cuándo ejecutarlo:

- Antes del deploy si cambiaste **`cdk/lambdas/requirements.txt`** o quieres forzar reinstalación.
- No es obligatorio en cada deploy si solo cambias IAM o recursos CDK sin tocar código/ruedas.

---

## 5. Comando recomendado: `deploy` con RDS + VPC

Sustituye IDs por los de tu cuenta. **Ruta del `cd`**: raíz `APIs/`.

```bash
cd /ruta/al/repo/APIs

./scripts/bundle_lambda_deps.sh

cd cdk
source .venv/bin/activate
export AWS_PROFILE=nuwa-prod
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

npx aws-cdk@2.170.0 deploy --all --app "python3 app.py" \
  -c environment=prod \
  -c useDatabase=true \
  -c reuseAllExternalSecrets=true \
  -c rdsVpcId=vpc-0dc24fcb6dec4f5db \
  -c rdsSecurityGroupId=sg-02664d9e7ccd46830 \
  -c "lambdaSubnetIds=subnet-092397141c9ed4e58,subnet-0d78b173be4b53f2d"
```

**No** uses `...` al final del comando. Si copias un ejemplo multilínea, asegúrate de que cada `\` sea el **último carácter** de la línea (sin espacios detrás).

Opcional: URL Supabase inicial:

```text
-c supabaseUrl=https://tu-proyecto.supabase.co
```

---

## 6. Script `cdk_deploy_hint.sh`

Desde la **raíz del repo** (`APIs/`):

```bash
./scripts/cdk_deploy_hint.sh
```

**Qué hace**

1. **Git:** lista archivos cambiados (working tree + staged frente a `HEAD`) y resume si conviene correr **`./scripts/bundle_lambda_deps.sh`** (p. ej. si cambió `cdk/lambdas/requirements.txt`) o si basta con **`cdk deploy`**.
2. **Bloque copiable:** imprime el mismo orden de comandos que la §5: `export AWS_PROFILE=nuwa-prod` (por defecto), `cd cdk`, `source .venv/bin/activate`, `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION` y `npx aws-cdk@2.170.0 deploy ...` con los `-c` de VPC/RDS alineados con **`scripts/cdk.deploy.env.example`**.

**Opciones**

| Flag | Efecto |
|------|--------|
| `--since origin/main` | Compara `git diff REF...HEAD` en lugar de cambios locales vs `HEAD`. |
| `--force-bundle` | Incluye siempre la línea `./scripts/bundle_lambda_deps.sh` aunque no haya tocado `requirements.txt`. |

**Configuración local (opcional)**

- Copia **`scripts/cdk.deploy.env.example`** → **`scripts/cdk.deploy.env`** (ignorado por git) y ajusta `NUWA_*`, VPC, subnets, **`NUWA_AWS_PROFILE`** si no usas `nuwa-prod`.
- Sin repo git, el script igual muestra el bloque de deploy y avisa que no puede clasificar cambios.

**Copiar el bloque:** cada línea del apartado “Copia y pega” debe ir en una línea nueva en la terminal; los `\` al final de línea no deben tener espacios después. Si pegas todo en una sola línea, el shell no interpretará bien el comando.

---

## 7. Validar antes de desplegar (`synth`)

Misma lista de `-c` que en `deploy`:

```bash
cd cdk && source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1

npx aws-cdk@2.170.0 synth --app "python3 app.py" \
  -c environment=prod \
  -c useDatabase=true \
  -c reuseAllExternalSecrets=true \
  -c rdsVpcId=vpc-0dc24fcb6dec4f5db \
  -c rdsSecurityGroupId=sg-02664d9e7ccd46830 \
  -c "lambdaSubnetIds=subnet-092397141c9ed4e58,subnet-0d78b173be4b53f2d"
```

- **`Vpc.from_lookup`** necesita **credenciales AWS** válidas en synth; puede cachearse en **`cdk/cdk.context.json`**.
- Para comprobar que el template incluye VPC endpoints:  
  `grep -c NuwaLambdaVpce cdk.out/Nuwa2ApiStack-prod.template.json`  
  (debería ser **> 0** con VPC activa).

---

## 8. Qué crea el stack (resumen)

- **API Gateway** REST, stage `prod`.
- **6 Lambdas**: sources, chunks, search, reports, admin, auth.
- **SSM** + **Secrets Manager** (según flags de reutilización).
- Con **`lambda_vpc_for_rds`**:
  - SG de Lambdas hacia RDS + regla **ingress 5432** en el SG de RDS.
  - **Interface VPC endpoints** (Secrets Manager, KMS, CloudWatch Logs, SSM, API Gateway) + SG de endpoints con **443** desde el SG de Lambdas.
  - `allowPublicSubnet=true` si las subnets son públicas (default VPC); las Lambdas en subnet pública **no** salen por IGW a Internet: los endpoints cubren las APIs de AWS.

Detalle de red y secretos: **`docs/RDS_LAMBDA.md`**.

---

## 9. Tiempos de deploy

- Cambios solo de **código** en Lambda: suele ser **rápido**.
- Cambios de **VPC / SG / endpoints** o **muchas Lambdas en VPC**: **15–30+ minutos** es habitual (ENIs, endpoints).
- La CLI puede parecer “quieta”; revisa **CloudFormation → Events** si dudas.

---

## 10. Tras un deploy exitoso

1. Outputs del stack: **`ApiBaseUrl`**, ARNs de secretos, etc.
2. Pruebas: **`docs/DEPLOYED_API_TESTS.md`**.
3. Login: `POST {ApiBaseUrl}v1/auth/login` (sin `x-api-key`); resto de rutas con JWT y/o API key según contrato.

---

## 11. Problemas frecuentes y soluciones

### Synth / deploy

| Síntoma | Causa probable | Qué hacer |
|---------|----------------|-----------|
| `TypeError: unsupported operand type(s) for \|` en `app.py` | Python &lt; 3.10 sin `from __future__ import annotations` | Ya corregido en repo; usa código actual. |
| `Token has expired` (SSO) | Sesión AWS caducada | `aws sso login --profile ...` |
| `You must either specify ... --all` | Línea de comando truncada o `...` literal | Comando completo, sin `...`. |
| `no such file or directory: ./scripts/bundle_lambda_deps.sh` | Estás dentro de `cdk/` | Ejecuta el script desde la **raíz** del repo. |
| `ValueError` VPC incompleta | Falta uno de los tres `-c` de red con `useDatabase` | Pasa `rdsVpcId`, `lambdaSubnetIds`, `rdsSecurityGroupId`. |
| `AccessDenied` `GetSecretValue` **app-crypto** (identity-based) | La variable de entorno suele llevar **ARN parcial** (`...:secret:nuwa2/prod/app-crypto` **sin** `-XXXXXX`). IAM **no** empareja `.../app-crypto-*` con ese recurso (falta el guión antes del comodín). | **`cdk deploy`** con el stack actual (política que incluye explícitamente el ARN parcial + `-*`). O en IAM, añade `Resource` exacto `arn:...:secret:nuwa2/prod/app-crypto` al rol de la Lambda. |

### Runtime Lambda (login / API)

| Síntoma | Causa probable | Qué hacer |
|---------|----------------|-----------|
| `Endpoint request timed out` (~29 s) | Lambda en VPC sin ruta a Secrets Manager / logs | NAT o **VPC interface endpoints** (el stack los crea con VPC; no quites `-c` de VPC). |
| `AccessDenied` `GetSecretValue` **app-crypto** | IAM / ARN de secreto con sufijo | El stack incluye refuerzo IAM; redeploy. Nombre del secreto: `nuwa2/<env>/app-crypto`. |
| `Internal server error` (API GW) | Excepción no capturada en Lambda | CloudWatch del log group de la función; en auth hay manejo de `ClientError` y errores genéricos con cuerpo JSON. |

### CloudFormation

| Síntoma | Causa probable | Qué hacer |
|---------|----------------|-----------|
| Se **eliminan** VPC endpoints / SG “de repente” | Deploy **sin** los `-c` de VPC con `useDatabase=true` | Siempre el mismo bloque de `-c` de red. |
| `DELETE_FAILED` **SecurityGroup** “dependent object” | ENIs de Lambda aún usando el SG viejo tras reemplazo | Esperar liberación de ENIs (a veces **20–45 min**); `describe-network-interfaces` filtrando por `group-id`; luego **reintentar** update o limpiar cuando no haya dependencias. |

---

## 12. Fijar contexto en `cdk.json` (opcional)

Para no olvidar VPC en cada comando, puedes añadir en **`cdk/cdk.json`** → `"context"`:

```json
"rdsVpcId": "vpc-...",
"lambdaSubnetIds": "subnet-...,subnet-...",
"rdsSecurityGroupId": "sg-..."
```

Así `deploy` puede acortarse; **documenta** que esos valores son sensibles al entorno (prod vs dev).

---

## 13. CI (GitHub Actions)

Workflow: **`.github/workflows/cdk-deploy.yml`**. Secret típico OIDC: **`AWS_ROLE_TO_ASSUME`**.

El pipeline debe pasar **los mismos** `-c` que en local (sobre todo VPC si usas RDS).

---

## 14. Archivos clave en el repo

| Archivo | Rol |
|---------|-----|
| `cdk/app.py` | App CDK, contexto, instancia del stack. |
| `cdk/nuwa2/nuwa_api_stack.py` | API GW, Lambdas, IAM, VPC, endpoints, secretos. |
| `cdk/lambdas/*.py` | Código de las funciones. |
| `scripts/bundle_lambda_deps.sh` | Ruedas Linux para Lambdas. |
| `scripts/cdk_deploy_hint.sh` | Ayuda: diff git + bloque `deploy` con `AWS_PROFILE` y `-c` (ver §6). |
| `scripts/cdk.deploy.env.example` | Plantilla de variables para el hint (`NUWA_*`, perfil, VPC). |
| `cdk/cdk.context.json` | Cache de lookups (VPC); no suele commitearse con datos sensibles; revisa política del equipo. |

---

## 15. Resumen de buenas prácticas

1. **Mismo** bloque de `-c` en cada deploy si usas **RDS + VPC**.
2. **`lambdaSubnetIds` entre comillas** en zsh/bash.
3. **`AWS_PROFILE`** y **`aws sso login`** antes de synth/deploy si usas SSO.
4. **`synth`** con los mismos `-c` antes de **`deploy`** cuando cambies contexto.
5. No mezclar deploy **con** VPC y **sin** VPC en el mismo entorno sin saber que CloudFormation **borrará** lo que ya no esté en la plantilla.
6. **`./scripts/cdk_deploy_hint.sh`** antes de deploy para ver qué cambió en git y copiar un bloque coherente (§6).
