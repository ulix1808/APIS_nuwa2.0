#!/usr/bin/env bash
# Analiza cambios (git) y muestra si conviene correr bundle_lambda_deps + bloque deploy CDK.
#
# Uso (desde la raíz del repo):
#   ./scripts/cdk_deploy_hint.sh
#   ./scripts/cdk_deploy_hint.sh --since origin/main
#   ./scripts/cdk_deploy_hint.sh --force-bundle   # incluye bundle aunque no cambió requirements
#
# Configuración: exporta variables o crea scripts/cdk.deploy.env (ver cdk.deploy.env.example).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/scripts/cdk.deploy.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/scripts/cdk.deploy.env"
  set +a
fi

: "${NUWA_CDK_ENVIRONMENT:=prod}"
: "${NUWA_CDK_USE_DATABASE:=true}"
: "${NUWA_CDK_REUSE_SECRETS:=true}"
: "${NUWA_RDS_VPC_ID:=vpc-0dc24fcb6dec4f5db}"
: "${NUWA_RDS_SG_ID:=sg-02664d9e7ccd46830}"
: "${NUWA_LAMBDA_SUBNET_IDS:=subnet-092397141c9ed4e58,subnet-0d78b173be4b53f2d}"
: "${NUWA_CDK_VERSION:=2.170.0}"
: "${CDK_DEFAULT_REGION:=us-east-1}"

SINCE_REF=""
FORCE_BUNDLE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      SINCE_REF="${2:?--since requiere un ref, ej. origin/main}"
      shift 2
      ;;
    --force-bundle)
      FORCE_BUNDLE=true
      shift
      ;;
    *)
      echo "Argumento desconocido: $1 (usa --since REF o --force-bundle)" >&2
      exit 1
      ;;
  esac
done

CHANGED_FILES="$(mktemp)"
cleanup() { rm -f "$CHANGED_FILES"; }
trap cleanup EXIT

if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  if [[ -n "$SINCE_REF" ]]; then
    git -C "$ROOT" diff --name-only "$SINCE_REF"...HEAD 2>/dev/null | sort -u >"$CHANGED_FILES" || true
    echo "=== Cambios respecto a: $SINCE_REF...HEAD ==="
  else
    {
      git -C "$ROOT" diff --name-only HEAD
      git -C "$ROOT" diff --name-only --cached HEAD
    } 2>/dev/null | sort -u >"$CHANGED_FILES" || true
    echo "=== Cambios locales (working tree + staged) vs HEAD ==="
  fi
else
  echo "=== Aviso: no hay repositorio git; no se puede clasificar cambios ===" >&2
  : >"$CHANGED_FILES"
fi

if [[ ! -s "$CHANGED_FILES" ]] && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "(sin archivos listados: árbol limpio respecto al criterio anterior)"
fi

REQ_CHANGED=false
LAMBDA_DIR_CHANGED=false
CDK_OTHER_CHANGED=false
OUTSIDE_CDK_CHANGED=false

while IFS= read -r f || [[ -n "$f" ]]; do
  [[ -z "$f" ]] && continue
  echo "  - $f"
  case "$f" in
    cdk/lambdas/requirements.txt)
      REQ_CHANGED=true
      LAMBDA_DIR_CHANGED=true
      ;;
    cdk/lambdas/*)
      LAMBDA_DIR_CHANGED=true
      ;;
    cdk/*)
      CDK_OTHER_CHANGED=true
      ;;
    *)
      OUTSIDE_CDK_CHANGED=true
      ;;
  esac
done <"$CHANGED_FILES"

echo ""
echo "=== Resumen ==="
if [[ ! -s "$CHANGED_FILES" ]] && ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "  • Sin git: revisa manualmente si tocaste requirements o código Lambda."
elif [[ ! -s "$CHANGED_FILES" ]]; then
  echo "  • Sin cambios detectados: deploy solo si quieres re-sincronizar AWS con HEAD."
else
  if $REQ_CHANGED; then
    echo "  • cdk/lambdas/requirements.txt cambió → conviene ./scripts/bundle_lambda_deps.sh"
  elif $LAMBDA_DIR_CHANGED; then
    echo "  • Código bajo cdk/lambdas/ cambió → deploy actualiza el asset; bundle no obligatorio si requirements igual."
  fi
  if $CDK_OTHER_CHANGED; then
    echo "  • Otros archivos bajo cdk/ cambiaron → basta cdk deploy (sin bundle salvo requirements)."
  fi
  if $OUTSIDE_CDK_CHANGED; then
    echo "  • Cambios fuera de cdk/ (p. ej. openapi, docs) → no exigen bundle; deploy CDK solo si aplica a tu caso."
  fi
fi

NEED_BUNDLE=false
if $FORCE_BUNDLE || $REQ_CHANGED; then
  NEED_BUNDLE=true
fi

echo ""
echo "=== Copia y pega (mismo directorio: raíz del repo APIs/) ==="
echo ""

if $NEED_BUNDLE; then
  echo "./scripts/bundle_lambda_deps.sh"
  echo ""
fi

PROFILE_LINE=""
if [[ -n "${NUWA_AWS_PROFILE:-}" ]]; then
  PROFILE_LINE=$'export AWS_PROFILE='"${NUWA_AWS_PROFILE}"$'\n'
fi

# shellcheck disable=SC2016
{
  if [[ -n "$PROFILE_LINE" ]]; then
    printf '%s' "$PROFILE_LINE"
  fi
  printf '%s\n' "cd cdk"
  printf '%s\n' "source .venv/bin/activate"
  printf '%s\n' 'export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)'
  printf '%s\n' "export CDK_DEFAULT_REGION=${CDK_DEFAULT_REGION}"
  printf '%s\n' ""
  printf '%s \\\n' "npx aws-cdk@${NUWA_CDK_VERSION} deploy --all --app \"python3 app.py\""
  printf '  -c environment=%s \\\n' "${NUWA_CDK_ENVIRONMENT}"
  printf '  -c useDatabase=%s \\\n' "${NUWA_CDK_USE_DATABASE}"
  printf '  -c reuseAllExternalSecrets=%s \\\n' "${NUWA_CDK_REUSE_SECRETS}"
  printf '  -c rdsVpcId=%s \\\n' "${NUWA_RDS_VPC_ID}"
  printf '  -c rdsSecurityGroupId=%s \\\n' "${NUWA_RDS_SG_ID}"
  printf '  -c "lambdaSubnetIds=%s"\n' "${NUWA_LAMBDA_SUBNET_IDS}"
}

echo ""
echo "Notas: lambdaSubnetIds va entre comillas por las comas. Ajusta IDs vía scripts/cdk.deploy.env o variables NUWA_*."
