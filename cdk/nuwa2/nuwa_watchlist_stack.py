"""
Early-warning lists ("alerta temprana"): own stack, own API Gateway, own Lambdas and schedules.

Separate from Nuwa2ApiStack (close to CloudFormation's 500-resource limit, and an independent flow).
It reads the secrets of the database environment it runs against (`db_environment_name`, prod today):
the dev stack uses the prod database, as decided for this feature.

- watchlist-sync: daily 04:00 UTC = 22:00 CDMX (Mexico has no daylight saving since 2022).
- watchlist-api:  /v1/watchlist/* (search, entries/get, changes, runs, sync).
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from nuwa2.nuwa_naming import (
    TAG_ENVIRONMENT,
    TAG_MANAGED_BY,
    TAG_NAME_PREFIX,
    TAG_PROJECT,
    TAG_VALUE_MANAGED_BY,
    TAG_VALUE_PROJECT,
    nuwa_name_prefix,
)


class NuwaWatchlistStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        db_environment_name: str,
        bff_url: str,
        schedules_enabled: bool,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        env = environment_name
        prefix = f"{nuwa_name_prefix(environment_name=env, aws_region='us-east-1')}-watchlist"

        db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DatabaseSecret", f"{TAG_VALUE_PROJECT}/{db_environment_name}/database"
        )
        app_crypto_name = f"{TAG_VALUE_PROJECT}/{db_environment_name}/app-crypto"
        app_crypto_secret = secretsmanager.Secret.from_secret_name_v2(self, "AppCryptoSecret", app_crypto_name)
        monitoring_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "MonitoringWorkerSecret", f"{TAG_VALUE_PROJECT}/{db_environment_name}/monitoring-worker"
        )

        lambdas_path = str(Path(__file__).resolve().parent.parent / "lambdas")
        code = lambda_.Code.from_asset(lambdas_path)
        base_env = {
            "NUWA_ENV": env,
            "NUWA_DATABASE_SECRET_ARN": db_secret.secret_arn,
            "NUWA_APP_CRYPTO_SECRET_ARN": app_crypto_secret.secret_arn,
            "NUWA_APP_CRYPTO_SECRET_NAME": app_crypto_name,
            "MONITORING_BFF_URL": bff_url.rstrip("/"),
            "MONITORING_WORKER_SECRET": monitoring_secret.secret_value.unsafe_unwrap(),
        }

        sync_fn = lambda_.Function(
            self,
            "WatchlistSyncLambda",
            function_name=f"{prefix}-sync",
            handler="handler_watchlist_sync.handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=code,
            timeout=Duration.minutes(15),
            memory_size=1536,
            environment=base_env,
            log_retention=logs.RetentionDays.ONE_MONTH,
            retry_attempts=0,
        )
        api_fn = lambda_.Function(
            self,
            "WatchlistApiLambda",
            function_name=f"{prefix}-api",
            handler="handler_watchlist_api.handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=code,
            timeout=Duration.seconds(70),
            memory_size=1024,
            environment={**base_env, "WATCHLIST_SYNC_FUNCTION": sync_fn.function_name},
            log_retention=logs.RetentionDays.ONE_MONTH,
        )
        sync_fn.grant_invoke(api_fn)

        # Imported secrets: the ARN has a random suffix, grant by name pattern as Nuwa2ApiStack does.
        secret_access = iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[
                self.format_arn(
                    service="secretsmanager",
                    resource="secret",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    resource_name=f"{TAG_VALUE_PROJECT}/{db_environment_name}/{name}*",
                )
                for name in ("database", "app-crypto", "monitoring-worker")
            ],
        )
        for fn in (sync_fn, api_fn):
            fn.add_to_role_policy(secret_access)

        api = apigw.RestApi(
            self,
            "WatchlistApi",
            rest_api_name=f"{prefix}-api",
            description="Nuwa 2.0 — alerta temprana de listas (OFAC propio + SAT), independiente del screening",
            deploy_options=apigw.StageOptions(stage_name=env, metrics_enabled=True),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "X-Api-Key"],
            ),
            # The account-wide API Gateway CloudWatch role belongs to Nuwa2ApiStack.
            cloud_watch_role=False,
        )
        integration = apigw.LambdaIntegration(api_fn, timeout=Duration.seconds(29), allow_test_invoke=False)
        wl = api.root.add_resource("v1").add_resource("watchlist")
        for path in ("search", "changes", "runs", "sync"):
            wl.add_resource(path).add_method("POST", integration)
        wl.add_resource("entries").add_resource("get").add_method("POST", integration)

        sync_rule = events.Rule(
            self,
            "WatchlistSyncSchedule",
            rule_name=f"{prefix}-sync-daily",
            description="Alerta temprana: descarga OFAC y delta, diario 22:00 CDMX (04:00 UTC)",
            schedule=events.Schedule.cron(minute="0", hour="4"),
            enabled=schedules_enabled,
        )
        sync_rule.add_target(
            targets.LambdaFunction(
                sync_fn, event=events.RuleTargetInput.from_object({"trigger": "schedule"}), retry_attempts=1
            )
        )

        for construct in (sync_fn, api_fn, api, sync_rule):
            Tags.of(construct).add(TAG_PROJECT, TAG_VALUE_PROJECT)
            Tags.of(construct).add(TAG_ENVIRONMENT, env)
            Tags.of(construct).add(TAG_NAME_PREFIX, prefix)
            Tags.of(construct).add(TAG_MANAGED_BY, TAG_VALUE_MANAGED_BY)

        CfnOutput(self, "WatchlistApiBaseUrl", value=api.url, description="Base URL de la API de alerta temprana")
        CfnOutput(self, "WatchlistSyncFunctionName", value=sync_fn.function_name)
        CfnOutput(self, "WatchlistApiFunctionName", value=api_fn.function_name)
        CfnOutput(self, "WatchlistDatabaseEnvironment", value=db_environment_name)
