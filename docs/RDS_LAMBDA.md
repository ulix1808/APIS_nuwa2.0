# PostgreSQL en RDS + Lambdas Nuwa

## Seguridad

- **No pegues contraseñas en el repositorio ni en tickets.** Rota la contraseña maestra de RDS si se expuso en un chat.
- El valor real vive solo en **AWS Secrets Manager** (`nuwa2/<env>/database`).
- **JWT y cifrado de API keys en BD** no usan el secreto de Postgres: van en **`nuwa2/<env>/app-crypto`** (`jwt_signing_secret` + `fernet_key`). Flujos, diagramas y RBAC: **`docs/API_AND_ARCHITECTURE.md`** (§ 1 y diagramas Mermaid).

## 1. Esquema en RDS

1. Crea la instancia RDS (PostgreSQL 17 u otra versión con `pg_trgm` y `gen_random_uuid` / `pgcrypto`).
2. Base inicial: **`nuwa2`** (o la que uses en el JSON del secreto).
3. Desde tu máquina (o bastión), aplica las migraciones en orden:

```bash
export PGHOST="tu-endpoint.rds.amazonaws.com"
export PGPORT=5432
export PGUSER=postgres
export PGDATABASE=nuwa2
export PGPASSWORD="..."  # mejor: psql con .pgpass
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260405110000_sources_catalog.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260405120000_risk_entities_search.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406120000_rbac_companies_users_reports.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406130100_reports_derived_columns.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406130200_reports_drop_legacy_save_state.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406140000_companies_apigw_key_id.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406150000_companies_apigw_key_secret.sql
psql -v ON_ERROR_STOP=1 -f supabase/migrations/20260406160000_nuwa_user_nuwa_space.sql
```

4. El usuario **`postgres`** en RDS tiene privilegios suficientes para saltarse RLS en la práctica; si usas otro rol, dale **`BYPASSRLS`** o define políticas (las migraciones activan RLS sin políticas en el repo).

## 2. Red: que Lambda llegue a RDS

- Si RDS **no es público**, coloca las Lambdas en la **misma VPC**, subnets y security group que permitan **entrada 5432** desde el SG de la Lambda.
- Si RDS es **público**, el security group de RDS debe permitir **entrada 5432** desde los rangos que uses (solo IP de oficina no sirve para Lambda). Lo habitual es VPC + SG Lambda → SG RDS.

### Desde la consola (IDs para CDK)

En **RDS → tu instancia → Connectivity & security**: **VPC**, **Subnets** del subnet group de la DB, y el **VPC security group** enlazado a la instancia. En **VPC → Subnets**, elige **subnets privadas con salida** (p. ej. con NAT) para las Lambdas: sin NAT o sin endpoints de interfaz, fallarán **Secrets Manager**, **SSM** y a veces **CloudWatch Logs**.

Con `useDatabase=true`, si pasas **cualquiera** de estos contextos debes pasar **los tres** (synth falla si falta alguno):

```bash
-c rdsVpcId=vpc-xxxxxxxx
-c lambdaSubnetIds=subnet-aaa,subnet-bbb
-c rdsSecurityGroupId=sg-xxxxxxxx
```

El stack crea un SG para las Lambdas y añade una regla de **entrada** en el SG de RDS (puerto **5432** desde ese SG).

**Subnets públicas (p. ej. default VPC):** el CDK despliega con `allowPublicSubnet` para poder sintetizar. Una Lambda en subnet pública **no** sale a Internet vía IGW; con `useDatabase` y contexto VPC/RDS, el stack crea un **security group** para los endpoints y **interface endpoints** (Private DNS) para **Secrets Manager**, **KMS**, **CloudWatch Logs**, **SSM** y **API Gateway** (admin), con **entrada 443** desde el SG de las Lambdas. La VPC se importa con `from_lookup`, así que el equivalente a `vpc.addInterfaceEndpoint()` en TypeScript es el constructo `InterfaceVpcEndpoint` por servicio. Si algo más llama a otra API de AWS, puede hacer falta otro endpoint o **NAT**.

## 3. Secreto JSON (`nuwa2/<env>/database`)

Tras el deploy del stack CDK, edita el secreto (consola o CLI) con un **JSON en una sola línea o formateado**:

```json
{
  "host": "nuwa20.xxxxx.us-east-1.rds.amazonaws.com",
  "port": 5432,
  "dbname": "nuwa2",
  "user": "postgres",
  "password": "TU_PASSWORD_AQUI",
  "sslmode": "require"
}
```

También vale **`username`** en lugar de **`user`** (formato típico si el secreto lo creaste como pares clave/valor estilo RDS). Las claves `engine` y `dbInstanceIdentifier` se ignoran.

- **`sslmode`:** `require` suele bastar con RDS. Para `verify-full` necesitas el bundle PEM de AWS en el paquete de la Lambda (no incluido por defecto).

## 4. Activar modo PostgreSQL en las Lambdas

El stack solo inyecta `NUWA_DATABASE_SECRET_ARN` si despliegas con contexto:

```bash
cd cdk
npx aws-cdk@2.170.0 deploy --app "python3 app.py" -c environment=prod -c useDatabase=true
```

Sin `-c useDatabase=true`, las Lambdas siguen usando **Supabase (PostgREST)** como antes.

## 5. Dependencias empaquetadas (`psycopg`)

Antes de `cdk deploy`, en la raíz del repo:

```bash
./scripts/bundle_lambda_deps.sh
```

Esto instala ruedas **manylinux x86_64** para Python 3.12 en `cdk/lambdas/` (compatible con el runtime de la Lambda). Vuelve a ejecutarlo si cambias `cdk/lambdas/requirements.txt`.

## 6. Qué usa cada modo

| Modo | Config | Datos |
|------|--------|--------|
| Supabase (default) | `SUPABASE_*` | PostgREST HTTPS |
| RDS | `NUWA_DATABASE_SECRET_ARN` + JSON arriba | `psycopg` directo a Postgres |

Handlers **search**, **reports**, **admin** están cableados a ambos y exigen **JWT** (`Authorization: Bearer`). **sources** y **chunks** siguen como stub pero validan backend y JWT antes de responder.
