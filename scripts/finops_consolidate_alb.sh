#!/usr/bin/env bash
# Consolida nuwa-website en nuwa-app (un solo ALB) y elimina el ALB redundante.
#
# Requisitos: AWS CLI, perfil con permisos elbv2 + route53.
# Uso:
#   export AWS_PROFILE=nuwa-prod
#   ./scripts/finops_consolidate_alb.sh plan    # solo muestra pasos
#   ./scripts/finops_consolidate_alb.sh apply     # ejecuta migración
#   ./scripts/finops_consolidate_alb.sh destroy-website-alb  # tras validar DNS

set -euo pipefail

: "${AWS_PROFILE:=nuwa-prod}"
: "${AWS_DEFAULT_REGION:=us-east-1}"

NUWA_APP_ALB_ARN="${NUWA_APP_ALB_ARN:-arn:aws:elasticloadbalancing:us-east-1:100906894518:loadbalancer/app/nuwa-app/2f11d8ba3d9ef945}"
NUWA_WEBSITE_ALB_ARN="${NUWA_WEBSITE_ALB_ARN:-arn:aws:elasticloadbalancing:us-east-1:100906894518:loadbalancer/app/nuwa-website/bace03a9569af6db}"
WEBSITE_CERT_ARN="${WEBSITE_CERT_ARN:-arn:aws:acm:us-east-1:100906894518:certificate/6553b7ae-4294-4c6e-b162-e6f81c349928}"
NUWA_NGINX_TG_ARN="${NUWA_NGINX_TG_ARN:-arn:aws:elasticloadbalancing:us-east-1:100906894518:targetgroup/NuwaNginx/2468740d1a29b076}"
ROUTE53_ZONE_ID="${ROUTE53_ZONE_ID:-Z08415152EKBWOENTQK44}"
WEBSITE_HOSTS="${WEBSITE_HOSTS:-www.nuwa.space,nuwa.space}"
RULE_PRIORITY="${RULE_PRIORITY:-20}"
EC2_TARGET="${EC2_TARGET:-i-068d242c31169c509}"

APP_HTTPS_LISTENER="${APP_HTTPS_LISTENER:-arn:aws:elasticloadbalancing:us-east-1:100906894518:listener/app/nuwa-app/2f11d8ba3d9ef945/c00ebc5eb1386d95}"
APP_HTTP_LISTENER="${APP_HTTP_LISTENER:-arn:aws:elasticloadbalancing:us-east-1:100906894518:listener/app/nuwa-app/2f11d8ba3d9ef945/cbf8baf1b0fa6c89}"

cmd="${1:-plan}"

plan_steps() {
  cat <<EOF
Plan — consolidar ALB website → nuwa-app

1. Añadir certificado website (${WEBSITE_CERT_ARN}) al listener :443 de nuwa-app
2. Regla host-header (${WEBSITE_HOSTS}) → target group NuwaNginx :80
3. Ajustar health check NuwaNginx (matcher 200-404) si sigue unhealthy
4. Route53: www.nuwa.space → alias nuwa-app (app.nuwa.space ya apunta ahí)
5. Validar https://www.nuwa.space y https://app.nuwa.space
6. destroy-website-alb: eliminar ALB nuwa-website (libera EIPs gestionadas por ese ALB)

Nota: las Elastic IPs del ALB no se "liberan" manualmente; desaparecen al borrar el ALB.
La IP pública de RDS es independiente (solo se quita con RDS privado).
EOF
}

apply_migration() {
  echo "==> Añadiendo certificado website al listener HTTPS nuwa-app..."
  aws elbv2 add-listener-certificates \
    --listener-arn "$APP_HTTPS_LISTENER" \
    --certificates CertificateArn="$WEBSITE_CERT_ARN" \
    2>/dev/null || echo "(certificado ya presente o sin permiso acm — revisar consola)"

  echo "==> Creando target group nuwa-website-app en nuwa-app (un TG no puede asociarse a dos ALB)..."
  VPC_ID=$(aws elbv2 describe-load-balancers --load-balancer-arns "$NUWA_APP_ALB_ARN" \
    --query 'LoadBalancers[0].VpcId' --output text)
  NEW_TG=$(aws elbv2 create-target-group \
    --name nuwa-website-app \
    --protocol HTTP --port 80 \
    --vpc-id "$VPC_ID" \
    --target-type instance \
    --health-check-path "/" \
    --matcher HttpCode=200-404 \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null) || \
  NEW_TG=$(aws elbv2 describe-target-groups --names nuwa-website-app \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
  aws elbv2 register-targets --target-group-arn "$NEW_TG" --targets Id="$EC2_TARGET",Port=80 2>/dev/null || true
  FORWARD_TG="$NEW_TG"

  echo "==> Ajustando health check ${FORWARD_TG##*/}..."
  aws elbv2 modify-target-group \
    --target-group-arn "$FORWARD_TG" \
    --health-check-path "/" \
    --matcher HttpCode=200-404

  IFS=',' read -ra HOSTS <<< "$WEBSITE_HOSTS"
  HOST_JSON=$(printf '"%s",' "${HOSTS[@]}" | sed 's/,$//')

  echo "==> Regla HTTPS host [${WEBSITE_HOSTS}] → NuwaNginx..."
  aws elbv2 create-rule \
    --listener-arn "$APP_HTTPS_LISTENER" \
    --priority "$RULE_PRIORITY" \
    --conditions "[{\"Field\":\"host-header\",\"HostHeaderConfig\":{\"Values\":[${HOST_JSON}]}}]" \
    --actions Type=forward,TargetGroupArn="$FORWARD_TG" \
    2>/dev/null || echo "(regla HTTPS puede existir — revisar prioridad ${RULE_PRIORITY})"

  echo "==> Regla HTTP host [${WEBSITE_HOSTS}] → NuwaNginx..."
  aws elbv2 create-rule \
    --listener-arn "$APP_HTTP_LISTENER" \
    --priority "$RULE_PRIORITY" \
    --conditions "[{\"Field\":\"host-header\",\"HostHeaderConfig\":{\"Values\":[${HOST_JSON}]}}]" \
    --actions Type=forward,TargetGroupArn="$FORWARD_TG" \
    2>/dev/null || echo "(regla HTTP puede existir)"

  APP_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$NUWA_APP_ALB_ARN" \
    --query 'LoadBalancers[0].DNSName' --output text)
  ZONE_ID=$(aws elbv2 describe-load-balancers --load-balancer-arns "$NUWA_APP_ALB_ARN" \
    --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text)

  echo "==> Route53 www.nuwa.space → ${APP_DNS}"
  aws route53 change-resource-record-sets --hosted-zone-id "$ROUTE53_ZONE_ID" --change-batch "{
    \"Changes\": [{
      \"Action\": \"UPSERT\",
      \"ResourceRecordSet\": {
        \"Name\": \"www.nuwa.space\",
        \"Type\": \"A\",
        \"AliasTarget\": {
          \"HostedZoneId\": \"${ZONE_ID}\",
          \"DNSName\": \"dualstack.${APP_DNS}\",
          \"EvaluateTargetHealth\": false
        }
      }
    }]
  }"

  echo ""
  echo "Migración aplicada. Valida:"
  echo "  curl -sI https://www.nuwa.space | head -5"
  echo "  curl -sI https://app.nuwa.space/v2 | head -5"
  echo ""
  echo "Si todo OK: ./scripts/finops_consolidate_alb.sh destroy-website-alb"
}

destroy_website_alb() {
  echo "==> Eliminando ALB nuwa-website (irreversible)..."
  aws elbv2 delete-load-balancer --load-balancer-arn "$NUWA_WEBSITE_ALB_ARN"
  echo "ALB eliminado. Las EIPs gestionadas por ese ALB se liberan automáticamente en minutos."
}

case "$cmd" in
  plan) plan_steps ;;
  apply) apply_migration ;;
  destroy-website-alb) destroy_website_alb ;;
  *)
    echo "Uso: $0 plan|apply|destroy-website-alb" >&2
    exit 1
    ;;
esac
