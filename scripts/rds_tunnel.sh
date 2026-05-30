#!/usr/bin/env bash
# Túnel SSH a RDS vía bastion Nuwa (misma VPC no; requiere regla SG bastion→5432 en sg nuwa2.0).
#
# Llave por defecto: ~/Downloads/Goleto_pairKey.pem
#
# Uso:
#   export PGPASSWORD='...'
#   ./scripts/rds_tunnel.sh start          # deja túnel en background (puerto local 15432)
#   PGHOST=127.0.0.1 PGPORT=15432 ./scripts/apply_migrations.sh
#   ./scripts/rds_tunnel.sh stop
#
#   ./scripts/rds_tunnel.sh psql -c "SELECT 1"
#   ./scripts/rds_tunnel.sh migrate-one supabase/migrations/20260530120000_source_risk_level_0_3.sql

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${NUWA_SSH_KEY:-$HOME/Downloads/Goleto_pairKey.pem}"
BASTION_HOST="${NUWA_BASTION_HOST:-ec2-user@3.92.3.96}"
RDS_HOST="${PGHOST:-nuwa20.csysyzxle6qq.us-east-1.rds.amazonaws.com}"
LOCAL_PORT="${NUWA_LOCAL_PG_PORT:-15432}"
TUNNEL_PATTERN="ssh.*${LOCAL_PORT}:${RDS_HOST}:5432"

load_env() {
  if [[ -f "$ROOT/.env" ]]; then set -a; # shellcheck disable=SC1091
    source "$ROOT/.env"; set +a; fi
  if [[ -f "$ROOT/scripts/pg.env" ]]; then set -a; # shellcheck disable=SC1091
    source "$ROOT/scripts/pg.env"; set +a; fi
  RDS_HOST="${PGHOST:-$RDS_HOST}"
}

require_key() {
  if [[ ! -f "$KEY" ]]; then
    echo "No se encontró la llave: $KEY" >&2
    echo "Exporta NUWA_SSH_KEY=/ruta/a/Goleto_pairKey.pem" >&2
    exit 1
  fi
  chmod 400 "$KEY" 2>/dev/null || true
}

tunnel_start() {
  require_key
  if pgrep -f "$TUNNEL_PATTERN" >/dev/null 2>&1; then
    echo "Túnel ya activo en 127.0.0.1:${LOCAL_PORT}"
    return 0
  fi
  ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -f -N \
    -L "${LOCAL_PORT}:${RDS_HOST}:5432" "$BASTION_HOST"
  sleep 1
  echo "Túnel: 127.0.0.1:${LOCAL_PORT} -> ${RDS_HOST}:5432 (${BASTION_HOST})"
}

tunnel_stop() {
  pkill -f "$TUNNEL_PATTERN" 2>/dev/null && echo "Túnel cerrado." || echo "No había túnel activo."
}

run_psql() {
  load_env
  : "${PGPASSWORD:?Exporta PGPASSWORD en la shell}"
  export PGHOST=127.0.0.1 PGPORT="$LOCAL_PORT" PGSSLMODE="${PGSSLMODE:-require}"
  export PGDATABASE="${PGDATABASE:-nuwa2}" PGUSER="${PGUSER:-postgres}"
  tunnel_start
  psql "$@"
}

cmd="${1:-}"
shift || true

case "$cmd" in
  start) load_env; tunnel_start ;;
  stop) tunnel_stop ;;
  psql) run_psql "$@" ;;
  migrate-one)
    f="${1:?Pasa ruta al .sql}"
    run_psql -v ON_ERROR_STOP=1 -f "$ROOT/$f"
    ;;
  "")
    echo "Uso: $0 {start|stop|psql|migrate-one} ..." >&2
    exit 1
    ;;
  *)
    echo "Comando desconocido: $cmd" >&2
    exit 1
    ;;
esac
