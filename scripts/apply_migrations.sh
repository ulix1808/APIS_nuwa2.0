#!/usr/bin/env bash
# Aplica todas las migraciones de supabase/migrations/ en orden (por nombre de archivo).
#
# Configura conexión con variables estándar de libpq (no pegues contraseñas en el repo):
#   export PGHOST=... PGPORT=5432 PGDATABASE=postgres PGUSER=postgres
#   export PGPASSWORD='...'   # o usa ~/.pgpass
#
# Supabase pooler / muchos hosts cloud:
#   export PGSSLMODE=require
#
# Uso:
#   Opción A: .env en la raíz del repo (PGHOST, PGDATABASE, PGUSER, … sin contraseña)
#   Opción B: scripts/pg.env (mismo contenido; sobreescribe .env si ambos existen)
#   export PGPASSWORD='...'  # solo en tu terminal, luego:
#   ./scripts/apply_migrations.sh
#   ./scripts/apply_migrations.sh --dry-run

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIG_DIR="$ROOT/supabase/migrations"

load_env_file() {
  local f="$1"
  if [[ -f "$f" ]]; then
    # shellcheck source=/dev/null
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
  fi
}

load_env_file "$ROOT/.env"
load_env_file "$ROOT/scripts/pg.env"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "error: psql no está en PATH (instala PostgreSQL client)." >&2
  exit 1
fi

if [[ -z "${PGHOST:-}" || -z "${PGDATABASE:-}" || -z "${PGUSER:-}" ]]; then
  echo "error: definen PGHOST, PGDATABASE y PGUSER (y PGPASSWORD o ~/.pgpass)." >&2
  exit 1
fi

FILES=()
while IFS= read -r f; do
  [[ -n "$f" ]] && FILES+=("$f")
done < <(find "$MIG_DIR" -maxdepth 1 -name '*.sql' -type f | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "error: no hay .sql en $MIG_DIR" >&2
  exit 1
fi

echo "Base: host=$PGHOST db=$PGDATABASE user=$PGUSER"
echo "Migraciones (${#FILES[@]}):"
printf '  %s\n' "${FILES[@]##*/}"

if $DRY_RUN; then
  exit 0
fi

for f in "${FILES[@]}"; do
  echo "---- $(basename "$f") ----"
  psql -v ON_ERROR_STOP=1 -f "$f"
done

echo "OK: migraciones aplicadas."
