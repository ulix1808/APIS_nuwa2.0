#!/usr/bin/env bash
# Smoke / regresión rápida contra la API desplegada (JWT Bearer).
#
# Uso:
#   export NUWA_API_BASE="https://xxxx.execute-api.us-east-1.amazonaws.com/prod"
#   export NUWA_EMAIL="nuwa@nuwa.space"
#   export NUWA_PASSWORD="..."
#   ./scripts/smoke_api.sh
#
# Con token ya obtenido (evita login):
#   export NUWA_ACCESS_TOKEN="eyJ..."
#   export NUWA_USER_ID=1
#   export NUWA_CLIENT_ID_BODY=1
#   ./scripts/smoke_api.sh
#
# Opcional:
#   ./scripts/smoke_api.sh --write   # crea fuente + 1 chunk + 1 reporte de prueba
#   NUWA_CLIENT_ID=1                 # body opcional en login si hay email en varias compañías
#
# Requisitos: curl, python3. Sin jq.
#
# Si GET falla con 401 pero el login fue 200: muchos proxies corporativos quitan
# Authorization en GET. Por defecto se usa curl --noproxy '*'. Para respetar
# HTTP(S)_PROXY: NUWA_SMOKE_USE_PROXY=1 ./scripts/smoke_api.sh

set -euo pipefail

WRITE=false
for arg in "$@"; do
  case "$arg" in
    --write) WRITE=true ;;
    -h|--help)
      sed -n '2,32p' "$0"
      exit 0
      ;;
  esac
done

: "${NUWA_API_BASE:?Definir NUWA_API_BASE (sin barra final; ej. https://.../prod)}"
BASE="${NUWA_API_BASE%/}"

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

die() { echo "error: $*" >&2; exit 1; }

# Por defecto no usar proxy: evita que proxies quiten Authorization en GET hacia execute-api.
CURL_EXTRA=(--http1.1)
if [[ "${NUWA_SMOKE_USE_PROXY:-}" != "1" ]]; then
  CURL_EXTRA+=(--noproxy '*')
fi

step() { echo ""; echo "=== $* ==="; }

curl_json() {
  local method="$1"
  local url="$2"
  local data="${3:-}"
  local code
  if [[ "$method" == "GET" ]]; then
    code=$(curl -sS "${CURL_EXTRA[@]}" -o "$BODY" -w "%{http_code}" \
      --oauth2-bearer "$TOKEN" "$url")
  else
    code=$(curl -sS "${CURL_EXTRA[@]}" -o "$BODY" -w "%{http_code}" -X "$method" "$url" \
      -H "Content-Type: application/json" --oauth2-bearer "$TOKEN" -d "$data")
  fi
  echo "HTTP $code"
  head -c 900 "$BODY"
  echo ""
  [[ "$code" =~ ^2 ]] || die "respuesta no 2xx: $url"
}

if [[ -n "${NUWA_ACCESS_TOKEN:-}" ]]; then
  TOKEN="$NUWA_ACCESS_TOKEN"
  : "${NUWA_USER_ID:?Con NUWA_ACCESS_TOKEN define NUWA_USER_ID (claim sub)}"
  : "${NUWA_CLIENT_ID_BODY:?Con NUWA_ACCESS_TOKEN define NUWA_CLIENT_ID_BODY (claim cid)}"
  USER_ID="$NUWA_USER_ID"
  CID="$NUWA_CLIENT_ID_BODY"
else
  : "${NUWA_EMAIL:?Definir NUWA_EMAIL y NUWA_PASSWORD, o NUWA_ACCESS_TOKEN}"
  : "${NUWA_PASSWORD:?Definir NUWA_PASSWORD o NUWA_ACCESS_TOKEN}"
  step "POST /v1/auth/login"
  export NUWA_EMAIL NUWA_PASSWORD
  export NUWA_CLIENT_ID="${NUWA_CLIENT_ID:-}"
  LOGIN_PAYLOAD=$(python3 - <<'PY'
import json, os
body = {"email": os.environ["NUWA_EMAIL"], "password": os.environ["NUWA_PASSWORD"]}
cid = os.environ.get("NUWA_CLIENT_ID", "").strip()
if cid:
    body["clientId"] = int(cid)
print(json.dumps(body))
PY
)
  code=$(curl -sS "${CURL_EXTRA[@]}" -o "$BODY" -w "%{http_code}" -X POST "$BASE/v1/auth/login" \
    -H "Content-Type: application/json" -d "$LOGIN_PAYLOAD")
  echo "HTTP $code"
  echo "(vista previa recortada; el accessToken completo se lee del JSON en disco)"
  head -c 500 "$BODY"
  echo ""
  [[ "$code" == "200" ]] || die "login falló"
  TOKEN=$(python3 -c "import json; print(json.load(open('$BODY'))['accessToken'])")
  TOKEN="${TOKEN//$'\r'/}"
  TOKEN="${TOKEN//$'\n'/}"
  [[ -n "$TOKEN" ]] || die "accessToken vacío tras login (revisa respuesta JSON)"
  USER_ID=$(python3 -c "import json; print(json.load(open('$BODY'))['user']['id'])")
  CID=$(python3 -c "import json; d=json.load(open('$BODY')); u=d['user']; print(u.get('clientId', d['company']['clientId']))")
fi

ACTOR=$(python3 -c "import json; print(json.dumps({'clientId': int('$CID'), 'userId': int('$USER_ID')}))")

step "POST /v1/reports/get?clientId=$CID&limit=5 (lista; mismo contrato que GET)"
curl_json POST "$BASE/v1/reports/get?clientId=$CID&limit=5" "{}"

step "POST /v1/sources/list"
curl_json POST "$BASE/v1/sources/list" "$ACTOR"

step "POST /v1/search"
SEARCH=$(python3 -c "import json; a=json.loads('$ACTOR'); a.update({'query':'nuwa','limit':5}); print(json.dumps(a))")
curl_json POST "$BASE/v1/search" "$SEARCH"

step "POST /v1/admin/companies/list"
curl_json POST "$BASE/v1/admin/companies/list" "$ACTOR"

step "POST /v1/admin/roles/list"
curl_json POST "$BASE/v1/admin/roles/list" "$ACTOR"

step "POST /v1/admin/users/list"
curl_json POST "$BASE/v1/admin/users/list" "$ACTOR"

if $WRITE; then
  TS=$(date +%s)
  NAME="smoke-source-$TS"
  step "POST /v1/sources (create: $NAME)"
  CREATE=$(python3 -c "import json; a=json.loads('$ACTOR'); a.update({'name':'$NAME','riskLevel':1,'visibility':'public'}); print(json.dumps(a))")
  curl_json POST "$BASE/v1/sources" "$CREATE"
  SRC_ID=$(python3 -c "import json; print(json.load(open('$BODY'))['sourceId'])")

  step "POST /v1/chunks/ingest (sourceId=$SRC_ID)"
  INGEST=$(python3 -c "
import json
a=json.loads('''$ACTOR''')
a.update({
  'sourceId': $SRC_ID,
  'replaceStrategy': 'all',
  'riskLevel': 1,
  'visibility': 'public',
  'entityType': 'company',
  'chunks': [{'order': 1, 'chunkText': 'Texto de prueba smoke_api $TS — entidad ficticia.'}],
})
print(json.dumps(a))
")
  curl_json POST "$BASE/v1/chunks/ingest" "$INGEST"

  FOLIO="SMOKE-$TS"
  step "POST /v1/reports/save (folio=$FOLIO)"
  SAVE=$(python3 -c "
import json
report={
  'folio': '$FOLIO',
  'entidad': 'Smoke Test',
  'tipoConsulta': 'Persona Moral',
  'fecha': '2026-04-05',
  'nivelRiesgo': 'low',
  'nivelRiesgoNumerico': 1,
  'resumen': {'totalListas': 0, 'totalMenciones': 0},
}
print(json.dumps({'clientId': int('$CID'), 'userId': int('$USER_ID'), 'report': report}))
")
  curl_json POST "$BASE/v1/reports/save" "$SAVE"
fi

echo ""
echo "OK — smoke completado (base $BASE, userId=$USER_ID, clientId=$CID)."
