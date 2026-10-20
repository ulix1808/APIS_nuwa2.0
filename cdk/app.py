#!/usr/bin/env python3
from __future__ import annotations

import os

import aws_cdk as cdk

from nuwa2.nuwa_api_stack import NuwaApiStack
from nuwa2.nuwa_ops_stack import NuwaOpsStack
from nuwa2.nuwa_watchlist_stack import NuwaWatchlistStack


app = cdk.App()

environment_name = app.node.try_get_context("environment") or "prod"
supabase_url_placeholder = (
    app.node.try_get_context("supabaseUrl") or "https://YOUR-PROJECT.supabase.co"
)
_use_db = str(app.node.try_get_context("useDatabase") or "").lower()
use_database = _use_db in ("1", "true", "yes")

def _truthy_ctx(key: str) -> bool:
    return str(app.node.try_get_context(key) or "").lower() in ("1", "true", "yes")


_reuse_all = _truthy_ctx("reuseAllExternalSecrets")
reuse_database_secret = _reuse_all or _truthy_ctx("reuseDatabaseSecret")
reuse_supabase_secret = _reuse_all or _truthy_ctx("reuseSupabaseSecret")
reuse_app_crypto_secret = _reuse_all or _truthy_ctx("reuseAppCryptoSecret")

_lambda_outside_vpc = _truthy_ctx("lambdaOutsideVpc")
_rds_vpc = str(app.node.try_get_context("rdsVpcId") or "").strip()
_lambda_subnets = str(app.node.try_get_context("lambdaSubnetIds") or "").strip()
_rds_sg = str(app.node.try_get_context("rdsSecurityGroupId") or "").strip()
lambda_vpc_for_rds: dict[str, str] | None = None
rds_public_lambda_access_sg_id: str | None = None

if _lambda_outside_vpc:
    if use_database:
        if not _rds_sg:
            raise ValueError(
                "Con lambdaOutsideVpc=true y useDatabase=true define rdsSecurityGroupId "
                "(SG de RDS) para añadir ingress 5432 desde Lambdas sin VPC."
            )
        rds_public_lambda_access_sg_id = _rds_sg
elif use_database and (_rds_vpc or _lambda_subnets or _rds_sg):
    missing = [
        n
        for n, v in (
            ("rdsVpcId", _rds_vpc),
            ("lambdaSubnetIds", _lambda_subnets),
            ("rdsSecurityGroupId", _rds_sg),
        )
        if not v
    ]
    if missing:
        raise ValueError(
            "Con useDatabase y VPC para RDS deben definirse los tres: "
            f"rdsVpcId, lambdaSubnetIds, rdsSecurityGroupId. Falta: {', '.join(missing)}"
        )
    lambda_vpc_for_rds = {
        "vpc_id": _rds_vpc,
        "subnet_ids": _lambda_subnets,
        "rds_security_group_id": _rds_sg,
    }

_cdk_account = (os.environ.get("CDK_DEFAULT_ACCOUNT") or "").strip()
if not _cdk_account:
    raise SystemExit(
        "CDK_DEFAULT_ACCOUNT está vacía. Suele pasar si falla `aws sts get-caller-identity` "
        "(perfil mal configurado). Revisa AWS_PROFILE: no uses el placeholder del doc; "
        "o desactívalo (`unset AWS_PROFILE`) para la cuenta default. "
        "Luego: export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)"
    )

NuwaApiStack(
    app,
    f"Nuwa2ApiStack-{environment_name}",
    environment_name=environment_name,
    supabase_url_placeholder=str(supabase_url_placeholder),
    use_database=use_database,
    reuse_database_secret=reuse_database_secret,
    reuse_supabase_secret=reuse_supabase_secret,
    reuse_app_crypto_secret=reuse_app_crypto_secret,
    lambda_vpc_for_rds=lambda_vpc_for_rds,
    rds_public_lambda_access_sg_id=rds_public_lambda_access_sg_id,
    env=cdk.Environment(
        account=_cdk_account,
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Nuwa 2.0 — API Gateway + Lambdas (sources, chunks, search, reports, admin) us-east-1",
)

# Early-warning lists: own stack, only when asked for (`-c watchlistEnvironment=dev|prod`), so the usual
# `deploy --all` of the API does not create or change it. It runs against the prod database for now.
watchlist_environment = str(app.node.try_get_context("watchlistEnvironment") or "").strip()
if watchlist_environment:
    NuwaWatchlistStack(
        app,
        f"NuwaWatchlistStack-{watchlist_environment}",
        environment_name=watchlist_environment,
        db_environment_name=str(app.node.try_get_context("watchlistDbEnvironment") or "prod"),
        bff_url=str(
            app.node.try_get_context("watchlistBffUrl")
            or ("https://app.nuwa.space/v2" if watchlist_environment == "prod" else "https://dev.nuwa.space/v2")
        ),
        schedules_enabled=_truthy_ctx("watchlistSchedules"),
        # dev writes to the prod database: alerts only for the QA client unless told otherwise.
        client_ids=str(
            app.node.try_get_context("watchlistClientIds") or ("" if watchlist_environment == "prod" else "8")
        ),
        env=cdk.Environment(
            account=_cdk_account,
            region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
        ),
        description="Nuwa 2.0 — alerta temprana de listas (OFAC propio + SAT): sync diario, cruce y API",
    )

# Ops alarms (health of BFF, Redis, scraper, OCR, MCP, screening queue, RDS): only with `-c opsEnvironment=dev|prod`.
ops_environment = str(app.node.try_get_context("opsEnvironment") or "").strip()
if ops_environment:
    _ops_prod = ops_environment == "prod"
    NuwaOpsStack(
        app,
        f"NuwaOpsStack-{ops_environment}",
        environment_name=ops_environment,
        # dev and prod BFF share the monitoring-worker secret today.
        secrets_environment_name=str(app.node.try_get_context("opsSecretsEnvironment") or "prod"),
        bff_url=str(
            app.node.try_get_context("opsBffUrl")
            or ("https://app.nuwa.space/v2" if _ops_prod else "https://dev.nuwa.space/v2")
        ),
        alert_emails=[e.strip() for e in str(app.node.try_get_context("opsAlertEmails") or "").split(",") if e.strip()],
        schedule_enabled=_ops_prod or _truthy_ctx("opsSchedule"),
        dependencies=[
            d.strip()
            for d in str(
                app.node.try_get_context("opsDependencies") or "redisState,redisCache,scraper,ocr,mcp,mcp2"
            ).split(",")
            if d.strip()
        ],
        expect_screening_worker=str(app.node.try_get_context("opsExpectWorker") or "true").lower() in ("1", "true", "yes"),
        rds_instance_id=str(app.node.try_get_context("opsRdsInstance") or ("nuwa20" if _ops_prod else "")),
        env=cdk.Environment(
            account=_cdk_account,
            region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
        ),
        description="Nuwa 2.0 — health canary y alarmas (BFF, Redis, scraper, OCR, MCP, cola, RDS) → correo",
    )

app.synth()
