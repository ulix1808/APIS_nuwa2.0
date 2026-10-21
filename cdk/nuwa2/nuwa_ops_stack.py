"""
NuwaOpsStack-{env}: knowing when something fails (docs/reingenieria-escala.md §6, BFF repo).

- Lambda ops-canary every 5 min → BFF GET /api/health/deps (worker secret) → CloudWatch metrics (Nuwa/Ops).
- Alarms → SNS topic `nuwa2-…-ops-alerts` → mail to the addresses in `-c opsAlertEmails=a@x,b@y`
  (each one confirms the SNS subscription once).
  App down, each dependency down (Redis state / cache, scraper, OCR, MCP replicas), screening queue stuck,
  no live screening worker, RDS CPU and disk, early-warning Lambdas erroring.

dev: the machine turns itself off when idle, so the canary schedule is off unless `-c opsSchedule=true`, and
missing data never alarms.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, SecretValue, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns_subscriptions as subs
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

NAMESPACE = "Nuwa/Ops"


class NuwaOpsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        secrets_environment_name: str,
        bff_url: str,
        alert_emails: list[str],
        schedule_enabled: bool,
        dependencies: list[str],
        expect_screening_worker: bool,
        rds_instance_id: str = "",
        serper_min_balance: int = 5000,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        env = environment_name
        prefix = f"{nuwa_name_prefix(environment_name=env, aws_region='us-east-1')}-ops"
        missing = cw.TreatMissingData.BREACHING if env == "prod" else cw.TreatMissingData.NOT_BREACHING

        topic = sns.Topic(self, "OpsAlerts", topic_name=f"{prefix}-alerts", display_name=f"Nuwa ops {env}")
        for email in alert_emails:
            topic.add_subscription(subs.EmailSubscription(email))
        action = cw_actions.SnsAction(topic)

        monitoring_secret_name = f"{TAG_VALUE_PROJECT}/{secrets_environment_name}/monitoring-worker"
        # Same secret the Serper Lambda uses (NuwaApiStack); the canary only calls GET /account (no credits).
        serper_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "SerperSecret", f"{TAG_VALUE_PROJECT}/{secrets_environment_name}/serper"
        )
        canary = lambda_.Function(
            self,
            "OpsCanaryLambda",
            function_name=f"{prefix}-canary",
            handler="handler_ops_canary.handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_.Code.from_asset(str(Path(__file__).resolve().parent.parent / "lambdas_ops")),
            timeout=Duration.seconds(40),
            memory_size=256,
            environment={
                "OPS_ENV": env,
                "OPS_HEALTH_URL": f"{bff_url.rstrip('/')}/api/health/deps",
                "MONITORING_WORKER_SECRET": SecretValue.secrets_manager(monitoring_secret_name).unsafe_unwrap(),
                "NUWA_SERPER_SECRET_ARN": serper_secret.secret_arn,
            },
            log_retention=logs.RetentionDays.TWO_WEEKS,
            retry_attempts=0,
        )
        canary.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": NAMESPACE}},
            )
        )
        serper_secret.grant_read(canary)
        rule = events.Rule(
            self,
            "OpsCanarySchedule",
            rule_name=f"{prefix}-canary-5min",
            description="Nuwa ops: health de BFF, Redis, scraper, OCR, MCP y cola de screening cada 5 min",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=schedule_enabled,
        )
        rule.add_target(targets.LambdaFunction(canary, retry_attempts=0))

        period = Duration.minutes(5)
        dim_env = {"Env": env}
        alarms: list[cw.Alarm] = []

        def alarm(cid: str, name: str, metric: cw.IMetric, threshold: float, op, periods: int, desc: str, miss=None):
            a = cw.Alarm(
                self,
                cid,
                alarm_name=f"{prefix}-{name}",
                alarm_description=desc,
                metric=metric,
                threshold=threshold,
                comparison_operator=op,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                treat_missing_data=miss or cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)
            a.add_ok_action(action)
            alarms.append(a)

        lt = cw.ComparisonOperator.LESS_THAN_THRESHOLD
        gt = cw.ComparisonOperator.GREATER_THAN_THRESHOLD
        alarm(
            "AppDown", "app-down",
            cw.Metric(namespace=NAMESPACE, metric_name="AppUp", dimensions_map=dim_env, statistic="Minimum", period=period),
            1, lt, 2, f"Nuwa {env}: el BFF no responde /api/health/deps (10 min).", miss=missing,
        )
        for dep in dependencies:
            alarm(
                f"Dep{dep[0].upper()}{dep[1:]}Down", f"{dep.lower()}-down",
                cw.Metric(
                    namespace=NAMESPACE, metric_name="DepUp", dimensions_map={**dim_env, "Dep": dep},
                    statistic="Minimum", period=period,
                ),
                1, lt, 3, f"Nuwa {env}: {dep} no responde (15 min).",
            )
        alarm(
            "ScreeningQueueStuck", "screening-queue-stuck",
            cw.Metric(
                namespace=NAMESPACE, metric_name="ScreeningQueueOldestAgeSec", dimensions_map=dim_env,
                statistic="Maximum", period=period,
            ),
            600, gt, 2, f"Nuwa {env}: hay un screening en cola desde hace más de 10 min.",
        )
        if expect_screening_worker:
            alarm(
                "NoScreeningWorker", "no-screening-worker",
                cw.Metric(
                    namespace=NAMESPACE, metric_name="ScreeningWorkers", dimensions_map=dim_env,
                    statistic="Minimum", period=period,
                ),
                1, lt, 3, f"Nuwa {env}: ningún screening-worker vivo (15 min). Los screenings no avanzan.",
            )
        if rds_instance_id:
            rds_dim = {"DBInstanceIdentifier": rds_instance_id}
            alarm(
                "RdsCpuHigh", "rds-cpu-high",
                cw.Metric(namespace="AWS/RDS", metric_name="CPUUtilization", dimensions_map=rds_dim, statistic="Average", period=period),
                80, gt, 2, f"RDS {rds_instance_id}: CPU > 80 % por 10 min.",
            )
            alarm(
                "RdsDiskLow", "rds-disk-low",
                cw.Metric(namespace="AWS/RDS", metric_name="FreeStorageSpace", dimensions_map=rds_dim, statistic="Minimum", period=period),
                5 * 1024**3, lt, 1, f"RDS {rds_instance_id}: menos de 5 GB libres.",
            )
        alarm(
            "SerperBalanceLow", "serper-balance-low",
            cw.Metric(namespace=NAMESPACE, metric_name="SerperBalance", dimensions_map=dim_env, statistic="Minimum", period=Duration.hours(1)),
            serper_min_balance, lt, 1,
            f"Serper {env}: quedan menos de {serper_min_balance} créditos (cada screening usa 28–84). Recargar en serper.dev.",
        )
        for fn in ("sync", "match"):
            fname = f"{nuwa_name_prefix(environment_name=env, aws_region='us-east-1')}-watchlist-{fn}"
            alarm(
                f"Watchlist{fn.title()}Errors", f"watchlist-{fn}-errors",
                cw.Metric(
                    namespace="AWS/Lambda", metric_name="Errors", dimensions_map={"FunctionName": fname},
                    statistic="Sum", period=Duration.hours(1),
                ),
                0, gt, 1, f"Alerta temprana {env}: la Lambda {fname} terminó con error (timeout o excepción).",
            )

        for construct in (topic, canary, rule, *alarms):
            Tags.of(construct).add(TAG_PROJECT, TAG_VALUE_PROJECT)
            Tags.of(construct).add(TAG_ENVIRONMENT, env)
            Tags.of(construct).add(TAG_NAME_PREFIX, prefix)
            Tags.of(construct).add(TAG_MANAGED_BY, TAG_VALUE_MANAGED_BY)

        CfnOutput(self, "OpsAlertsTopicArn", value=topic.topic_arn)
        CfnOutput(self, "OpsCanaryFunctionName", value=canary.function_name)
        CfnOutput(self, "OpsHealthUrl", value=f"{bff_url.rstrip('/')}/api/health/deps")
        CfnOutput(self, "OpsAlarmCount", value=str(len(alarms)))
