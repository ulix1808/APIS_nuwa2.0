# Desarrollo local (Lambda + Postgres sin API Gateway)

## Qué “funcionaba” antes

- **Migraciones con `psql`** contra RDS o Postgres local: sí, es independiente del código Lambda.
- **API desplegada en AWS**: es el único flujo “completo” HTTP sin trabajo extra.
- **Este repo** no incluye un servidor HTTP local que enrute todos los paths; las Lambdas esperan un **evento** tipo API Gateway.

## Opción recomendada para integración real

Seguir **`docs/DEPLOYED_API_TESTS.md`** contra **`ApiBaseUrl`** cuando RDS y secretos en AWS estén bien configurados.

## Opción: Postgres local (Docker) + invocar solo login

Sirve para validar **consulta a BD + JWT** sin Secrets Manager ni despliegue.

### 1. Postgres y esquema

```bash
docker run --name nuwa-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=nuwa2 -p 5432:5432 -d postgres:16
```

Aplica las migraciones (desde la raíz del repo, con `PG*` apuntando a `localhost`).

### 2. Dependencias Python (en tu máquina, no en la carpeta Lambda Linux)

Usa un venv en la raíz o instala en el sistema: `psycopg[binary]`, `cryptography`, `PyJWT`, etc. (alineado con `cdk/lambdas/requirements.txt`). En Mac, **no** uses el `psycopg_binary` manylinux de `bundle_lambda_deps.sh` para esto.

### 3. Variables solo para local

**No** configures estas variables en la Lambda en AWS; son para tu terminal.

| Variable | Uso |
|----------|-----|
| `NUWA_DATABASE_CONFIG_JSON` | Mismo JSON que el secreto `database`: `host`, `port`, `dbname`, `user`, `password`, `sslmode` (en local suele ser `disable`). |
| `NUWA_APP_CRYPTO_CONFIG_JSON` | `{"jwt_signing_secret":"...(≥32 chars)...","fernet_key":"..."}` — genera la clave con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |

Con `NUWA_DATABASE_CONFIG_JSON` definido, el código entra en **modo Postgres directo** aunque no exista `NUWA_DATABASE_SECRET_ARN`.

### 4. Probar `handler_auth` desde Python

```bash
cd /Users/ulix/Documents/Code/nuwa2.0/APIs/cdk/lambdas

export NUWA_DATABASE_CONFIG_JSON='{"host":"127.0.0.1","port":5432,"dbname":"nuwa2","user":"postgres","password":"postgres","sslmode":"disable"}'
export NUWA_APP_CRYPTO_CONFIG_JSON='{"jwt_signing_secret":"local-dev-secret-at-least-32-chars!!","fernet_key":"PEGAR_FERNET_KEY"}'

export PYTHONPATH=.
python3 -c "
import json
from handler_auth import handler
ev = {
  'body': json.dumps({
    'email': 'admin@nuwa.local',
    'password': 'ChangeMe!',
    'clientId': 1
  })
}
print(json.dumps(handler(ev, None), indent=2))
"
```

Respuesta esperada: `statusCode` **200** y cuerpo con `accessToken` si el seed RBAC está en la base.

## Tests unitarios

`pytest` en `tests/` cubre helpers (password, RBAC, OpenAPI, etc.), no el despliegue completo.
