#!/usr/bin/env bash
# Despliega Lambdas FUERA de VPC → elimina Interface VPC Endpoints del stack CDK (~\$73/mes).
#
# Requisitos: RDS nuwa20 PubliclyAccessible=true; secreto database con sslmode=require.
#
# Uso (desde raíz del repo):
#   export AWS_PROFILE=nuwa-prod
#   ./scripts/finops_lambda_outside_vpc.sh plan
#   ./scripts/finops_lambda_outside_vpc.sh deploy

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${AWS_PROFILE:=nuwa-prod}"
: "${CDK_DEFAULT_REGION:=us-east-1}"
: "${NUWA_RDS_SG_ID:=sg-02664d9e7ccd46830}"
: "${NUWA_CDK_VERSION:=2.170.0}"

cmd="${1:-plan}"

plan_steps() {
  cat <<EOF
Plan — Lambdas fuera de VPC (sin VPCE)

1. CDK deploy con:
   -c lambdaOutsideVpc=true
   -c rdsSecurityGroupId=${NUWA_RDS_SG_ID}
   (SIN rdsVpcId, lambdaSubnetIds, lambdaRouteTableIds)

2. CloudFormation eliminará:
   - 5 Interface VPC Endpoints (Secrets, KMS, Logs, SSM, API GW)
   - Security groups de Lambda/VPCE en VPC 172.30.x
   - ENIs de Lambda en VPC

3. CloudFormation añadirá:
   - Regla SG RDS: 0.0.0.0/0:5432 (Lambdas sin IP fija)

4. Validar API:
   curl -s -X POST https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod/v1/auth/login \\
     -H 'Content-Type: application/json' -d '{"email":"...","password":"..."}'

5. VPCE S3 Gateway manual (vpce-0a65ed31f64c973be) puede quedarse (gratis) o borrarse.

Ahorro estimado: ~\$73/mes en Interface Endpoints.
EOF
}

do_deploy() {
  cd cdk
  if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
  export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
  export CDK_DEFAULT_ACCOUNT
  CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

  echo "==> RDS publicly accessible (debe ser true)..."
  aws rds describe-db-instances --db-instance-identifier nuwa20 \
    --query 'DBInstances[0].PubliclyAccessible' --output text

  echo "==> CDK deploy (lambdaOutsideVpc)..."
  npx "aws-cdk@${NUWA_CDK_VERSION}" deploy --all --app "python3 app.py" \
    -c environment=prod \
    -c useDatabase=true \
    -c reuseAllExternalSecrets=true \
    -c lambdaOutsideVpc=true \
    -c "rdsSecurityGroupId=${NUWA_RDS_SG_ID}" \
    --require-approval never

  echo ""
  echo "Deploy completado. Verifica endpoints VPCE eliminados:"
  echo "  aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=vpc-0dc24fcb6dec4f5db --query 'VpcEndpoints[?VpcEndpointType==\`Interface\`].VpcEndpointId'"
}

case "$cmd" in
  plan) plan_steps ;;
  deploy) do_deploy ;;
  *)
    echo "Uso: $0 plan|deploy" >&2
    exit 1
    ;;
esac
