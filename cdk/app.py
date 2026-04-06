#!/usr/bin/env python3
from __future__ import annotations

import os

import aws_cdk as cdk

from nuwa2.nuwa_api_stack import NuwaApiStack


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

_rds_vpc = str(app.node.try_get_context("rdsVpcId") or "").strip()
_lambda_subnets = str(app.node.try_get_context("lambdaSubnetIds") or "").strip()
_rds_sg = str(app.node.try_get_context("rdsSecurityGroupId") or "").strip()
lambda_vpc_for_rds: dict[str, str] | None = None
if use_database and (_rds_vpc or _lambda_subnets or _rds_sg):
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
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Nuwa 2.0 — API Gateway + Lambdas (sources, chunks, search, reports, admin) us-east-1",
)

app.synth()
