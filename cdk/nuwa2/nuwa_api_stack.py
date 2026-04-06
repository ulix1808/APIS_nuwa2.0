from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, SecretValue, Stack, Tags, Token
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_ssm as ssm
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


class NuwaApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        supabase_url_placeholder: str,
        use_database: bool = False,
        reuse_database_secret: bool = False,
        reuse_supabase_secret: bool = False,
        reuse_app_crypto_secret: bool = False,
        lambda_vpc_for_rds: dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment_name
        region_token = self.region
        aws_region = "us-east-1" if Token.is_unresolved(region_token) else str(region_token)
        prefix = nuwa_name_prefix(environment_name=env, aws_region=aws_region)

        url_param = ssm.StringParameter(
            self,
            "SupabaseProjectUrl",
            parameter_name=f"/{TAG_VALUE_PROJECT}/{env}/supabase/url",
            string_value=supabase_url_placeholder,
            description="URL del proyecto Supabase (https://<ref>.supabase.co). Editable en SSM sin redeploy.",
        )

        supabase_secret_name = f"{TAG_VALUE_PROJECT}/{env}/supabase-service-role-key"
        if reuse_supabase_secret:
            secret = secretsmanager.Secret.from_secret_name_v2(
                self,
                "SupabaseServiceRoleKey",
                supabase_secret_name,
            )
        else:
            secret = secretsmanager.Secret(
                self,
                "SupabaseServiceRoleKey",
                secret_name=supabase_secret_name,
                description="JWT service_role de Supabase (Settings → API). Texto plano o JSON {\"service_role_key\":\"...\"}.",
                removal_policy=RemovalPolicy.RETAIN,
            )

        db_secret_name = f"{TAG_VALUE_PROJECT}/{env}/database"
        if reuse_database_secret:
            # El nombre ya existe en Secrets Manager (creación manual o stack previo con RETAIN).
            db_secret = secretsmanager.Secret.from_secret_name_v2(
                self,
                "NuwaPostgresCredentials",
                db_secret_name,
            )
        else:
            db_secret = secretsmanager.Secret(
                self,
                "NuwaPostgresCredentials",
                secret_name=db_secret_name,
                description="PostgreSQL (RDS): JSON con host, port, dbname, user, password, sslmode. Ver docs/RDS_LAMBDA.md.",
                removal_policy=RemovalPolicy.RETAIN,
            )

        lambdas_path = str(Path(__file__).resolve().parent.parent / "lambdas")
        # Dependencias Linux x86_64 (psycopg) en lambdas/ — ver scripts/bundle_lambda_deps.sh
        lambda_code = lambda_.Code.from_asset(lambdas_path)

        lambda_env: dict[str, str] = {
            "SUPABASE_URL_PARAMETER": url_param.parameter_name,
            "SUPABASE_SECRET_ARN": secret.secret_arn,
            "NUWA_ENV": env,
        }
        if use_database:
            lambda_env["NUWA_DATABASE_SECRET_ARN"] = db_secret.secret_arn

        app_crypto_name = f"{TAG_VALUE_PROJECT}/{env}/app-crypto"
        if reuse_app_crypto_secret:
            app_crypto_secret = secretsmanager.Secret.from_secret_name_v2(
                self,
                "NuwaAppCrypto",
                app_crypto_name,
            )
        else:
            app_crypto_secret = secretsmanager.Secret(
                self,
                "NuwaAppCrypto",
                secret_name=app_crypto_name,
                description=(
                    'JSON: {"jwt_signing_secret":">=32 chars","fernet_key":"Fernet.generate_key() url-safe"}. '
                    "Rotar en producción tras el primer deploy."
                ),
                removal_policy=RemovalPolicy.RETAIN,
                secret_string_value=SecretValue.unsafe_plain_text(
                    '{"jwt_signing_secret":"__REEMPLAZAR_MIN_32_CARACTERES_EN_SECRETS_MANAGER__",'
                    '"fernet_key":"1g6pQ06TDCsJlqHZBKEYrTIDwNWetZAxB1hswpD6e0w="}'
                ),
            )

        lambda_env["NUWA_APP_CRYPTO_SECRET_ARN"] = app_crypto_secret.secret_arn
        lambda_env["NUWA_APP_CRYPTO_SECRET_NAME"] = app_crypto_name

        lambda_kwargs: dict = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_code,
            timeout=Duration.seconds(29),
            memory_size=256,
            environment=lambda_env,
            log_retention=logs.RetentionDays.TWO_WEEKS,
        )

        if lambda_vpc_for_rds:
            vpc_id = lambda_vpc_for_rds["vpc_id"]
            subnet_ids = [
                s.strip()
                for s in lambda_vpc_for_rds["subnet_ids"].split(",")
                if s.strip()
            ]
            rds_sg_id = lambda_vpc_for_rds["rds_security_group_id"]
            if not subnet_ids:
                raise ValueError("lambdaSubnetIds debe listar al menos un subnet id.")

            vpc = ec2.Vpc.from_lookup(self, "NuwaRdsVpc", vpc_id=vpc_id)
            lambda_sg = ec2.SecurityGroup(
                self,
                "NuwaLambdaToRdsSg",
                vpc=vpc,
                description="Salida Nuwa Lambdas hacia RDS y AWS APIs (requiere NAT o VPC endpoints)",
                allow_all_outbound=True,
            )
            ec2.CfnSecurityGroupIngress(
                self,
                "RdsIngressFromNuwaLambdas",
                group_id=rds_sg_id,
                ip_protocol="tcp",
                from_port=5432,
                to_port=5432,
                source_security_group_id=lambda_sg.security_group_id,
                description="PostgreSQL desde Lambdas Nuwa (CDK)",
            )
            lambda_subnets = [
                ec2.Subnet.from_subnet_id(self, f"NuwaLambdaSubnet{i}", sid)
                for i, sid in enumerate(subnet_ids)
            ]
            lambda_subnet_sel = ec2.SubnetSelection(subnets=lambda_subnets)

            # VPC importada (from_lookup): no se puede usar vpc.add_interface_endpoint() como con una Vpc nueva
            # en el mismo stack; el equivalente es InterfaceVpcEndpoint en cada servicio.
            #
            # Patrón (igual que un SG dedicado al endpoint + ingress 443 desde el SG de la Lambda):
            #   endpoint_sg.add_ingress_rule(lambda_sg, Port.tcp(443), ...)
            #   + un InterfaceVpcEndpoint por servicio con security_groups=[endpoint_sg], private_dns_enabled=True.
            #
            # Sin NAT, las Lambdas en subnets públicas no alcanzan las APIs públicas de AWS; estos endpoints
            # + Private DNS enrutan secretsmanager, kms, logs, ssm, apigateway dentro de la VPC.
            # Mismo patrón que endpointSg + addIngressRule(lambdaSg, 443) + addInterfaceEndpoint en TS;
            # con VPC importada se usa InterfaceVpcEndpoint explícito (ver comentario arriba).
            interface_endpoint_sg = ec2.SecurityGroup(
                self,
                "NuwaLambdaInterfaceVpceSg",
                vpc=vpc,
                description="SG de interface endpoints: 443 desde NuwaLambdaToRdsSg (Secrets, KMS, Logs, SSM, API GW)",
                allow_all_outbound=True,
            )
            interface_endpoint_sg.add_ingress_rule(
                lambda_sg,
                ec2.Port.tcp(443),
                "Allow Lambda to access interface endpoints",
            )
            for ep_id, aws_svc in (
                ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
                ("Kms", ec2.InterfaceVpcEndpointAwsService.KMS),
                ("CloudWatchLogs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
                ("Ssm", ec2.InterfaceVpcEndpointAwsService.SSM),
                ("ApiGateway", ec2.InterfaceVpcEndpointAwsService.APIGATEWAY),
            ):
                ec2.InterfaceVpcEndpoint(
                    self,
                    f"NuwaLambdaVpce{ep_id}",
                    vpc=vpc,
                    service=aws_svc,
                    subnets=lambda_subnet_sel,
                    security_groups=[interface_endpoint_sg],
                    private_dns_enabled=True,
                )

            # Default-VPC subnets suelen ser "públicas" (ruta a IGW); CDK exige esto explícitamente.
            # Ojo: Lambda en subnet pública no sale a Internet vía IGW; para Secrets Manager / logs hace
            # falta NAT en subnets privadas o VPC interface endpoints (p. ej. secretsmanager, logs).
            lambda_kwargs = {
                **lambda_kwargs,
                "vpc": vpc,
                "vpc_subnets": lambda_subnet_sel,
                "security_groups": [lambda_sg],
                "allow_public_subnet": True,
            }

        sources_fn = lambda_.Function(
            self,
            "SourcesLambda",
            function_name=f"{prefix}-lambda-sources",
            handler="handler_sources.handler",
            **lambda_kwargs,
        )
        chunks_fn = lambda_.Function(
            self,
            "ChunksLambda",
            function_name=f"{prefix}-lambda-chunks-ingest",
            handler="handler_chunks.handler",
            **lambda_kwargs,
        )
        search_fn = lambda_.Function(
            self,
            "SearchLambda",
            function_name=f"{prefix}-lambda-search",
            handler="handler_search.handler",
            **lambda_kwargs,
        )
        reports_fn = lambda_.Function(
            self,
            "ReportsLambda",
            function_name=f"{prefix}-lambda-reports",
            handler="handler_reports.handler",
            **lambda_kwargs,
        )
        admin_fn = lambda_.Function(
            self,
            "AdminLambda",
            function_name=f"{prefix}-lambda-admin",
            handler="handler_admin.handler",
            **lambda_kwargs,
        )
        auth_fn = lambda_.Function(
            self,
            "AuthLambda",
            function_name=f"{prefix}-lambda-auth",
            handler="handler_auth.handler",
            **lambda_kwargs,
        )

        for fn in (sources_fn, chunks_fn, search_fn, reports_fn, admin_fn, auth_fn):
            url_param.grant_read(fn)
            secret.grant_read(fn)
            db_secret.grant_read(fn)
            app_crypto_secret.grant_read(fn)

        # app-crypto: además de grant_read, refuerzo IAM. A veces el recurso en la política (ARN+comodín)
        # no casa con cómo IAM evalúa GetSecretValue (SecretId parcial sin sufijo). AWS documenta usar
        # Resource "*" + Condition StringLike sobre secretsmanager:SecretId.
        _ac_name = f"{TAG_VALUE_PROJECT}/{env}/app-crypto"
        # ARN parcial (sin sufijo -XXXXXX): es lo que suele inyectar CDK en env y lo que boto3 envía a GetSecretValue.
        # IAM NO hace coincidir .../app-crypto-* con .../app-crypto (falta el guión antes del comodín).
        _app_crypto_arn_partial = self.format_arn(
            service="secretsmanager",
            resource="secret",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
            resource_name=_ac_name,
        )
        app_crypto_secret_id_patterns = [
            _app_crypto_arn_partial,
            self.format_arn(
                service="secretsmanager",
                resource="secret",
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
                resource_name=f"{_ac_name}-*",
            ),
            self.format_arn(
                service="secretsmanager",
                resource="secret",
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
                resource_name=f"{_ac_name}*",
            ),
            _ac_name,
        ]
        app_crypto_iam_fix = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
            ],
            resources=["*"],
            conditions={
                "StringLike": {"secretsmanager:SecretId": app_crypto_secret_id_patterns}
            },
        )
        for fn in (sources_fn, chunks_fn, search_fn, reports_fn, admin_fn, auth_fn):
            fn.add_to_role_policy(app_crypto_iam_fix)

        # ARN explícito + comodín de sufijo (Secrets Manager añade 6 chars). grant_read / StringLike a veces
        # no casan con el SecretId que envía el SDK (p. ej. secreto importado por nombre).
        _app_crypto_arn_wildcard = self.format_arn(
            service="secretsmanager",
            resource="secret",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
            resource_name=f"{_ac_name}-*",
        )
        app_crypto_resource_allow = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
            ],
            resources=[
                app_crypto_secret.secret_arn,
                _app_crypto_arn_partial,
                _app_crypto_arn_wildcard,
            ],
        )
        for fn in (sources_fn, chunks_fn, search_fn, reports_fn, admin_fn, auth_fn):
            fn.add_to_role_policy(app_crypto_resource_allow)

        api = apigw.RestApi(
            self,
            "NuwaHttpApi",
            rest_api_name=f"{prefix}-api",
            description="Nuwa 2.0 — catálogo, chunks, búsqueda, reportes, admin, auth",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
                metrics_enabled=True,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=[
                    "Content-Type",
                    "X-Amz-Date",
                    "Authorization",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                ],
            ),
            cloud_watch_role=True,
        )

        # Documentación OpenAPI estática pública (S3 website):
        # - index.html (Swagger UI) + openapi.yaml
        # - URL pública por endpoint de website S3 (HTTP)
        docs_bucket = s3.Bucket(
            self,
            "NuwaOpenApiDocsBucket",
            bucket_name=f"{prefix}-openapi-docs",
            website_index_document="index.html",
            public_read_access=True,
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=False,
                restrict_public_buckets=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )

        repo_root = Path(__file__).resolve().parents[2]
        openapi_yaml = (repo_root / "openapi" / "openapi.yaml").read_text(encoding="utf-8")
        index_html = """
<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Nuwa API - OpenAPI</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist/swagger-ui.css" />
    <style>
      body { margin: 0; background: #fafafa; }
      .topbar { display: none; }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: "./openapi.yaml",
        dom_id: "#swagger-ui",
        deepLinking: true,
        displayRequestDuration: true,
        defaultModelsExpandDepth: 1
      });
    </script>
  </body>
</html>
""".strip()

        s3deploy.BucketDeployment(
            self,
            "NuwaOpenApiDocsDeploy",
            destination_bucket=docs_bucket,
            sources=[
                s3deploy.Source.data("index.html", index_html),
                s3deploy.Source.data("openapi.yaml", openapi_yaml),
            ],
            retain_on_delete=True,
        )

        sources_integration = apigw.LambdaIntegration(sources_fn)
        chunks_integration = apigw.LambdaIntegration(chunks_fn)
        search_integration = apigw.LambdaIntegration(search_fn)
        reports_integration = apigw.LambdaIntegration(reports_fn)
        admin_integration = apigw.LambdaIntegration(admin_fn)
        auth_integration = apigw.LambdaIntegration(auth_fn)

        v1 = api.root.add_resource("v1")
        v1.add_resource("auth").add_resource("login").add_method(
            "POST", auth_integration, api_key_required=False
        )
        sources = v1.add_resource("sources")
        sources.add_method("POST", sources_integration, api_key_required=False)
        sources.add_resource("list").add_method("POST", sources_integration, api_key_required=False)
        sources.add_resource("get").add_method("POST", sources_integration, api_key_required=False)
        sources.add_resource("update").add_method("POST", sources_integration, api_key_required=False)
        sources.add_resource("delete").add_method("POST", sources_integration, api_key_required=False)

        v1.add_resource("chunks").add_resource("ingest").add_method(
            "POST", chunks_integration, api_key_required=False
        )

        v1.add_resource("search").add_method("POST", search_integration, api_key_required=False)

        reports = v1.add_resource("reports")
        reports_get = reports.add_resource("get")
        # GET y POST equivalentes: algunos proxies/clientes alteran Authorization solo en GET.
        reports_get.add_method("GET", reports_integration, api_key_required=False)
        reports_get.add_method("POST", reports_integration, api_key_required=False)
        reports.add_resource("save").add_method("POST", reports_integration, api_key_required=False)
        upd = reports.add_resource("update")
        upd.add_method("POST", reports_integration, api_key_required=False)
        upd.add_method("PUT", reports_integration, api_key_required=False)
        reports.add_resource("delete").add_method("POST", reports_integration, api_key_required=False)

        admin = v1.add_resource("admin")
        ac = admin.add_resource("companies")
        ac.add_resource("list").add_method("POST", admin_integration, api_key_required=False)
        ac.add_resource("create").add_method("POST", admin_integration, api_key_required=False)
        ac.add_resource("update").add_method("POST", admin_integration, api_key_required=False)
        ac.add_resource("delete").add_method("POST", admin_integration, api_key_required=False)
        ar = admin.add_resource("roles")
        ar.add_resource("list").add_method("POST", admin_integration, api_key_required=False)
        au = admin.add_resource("users")
        au.add_resource("list").add_method("POST", admin_integration, api_key_required=False)
        au.add_resource("create").add_method("POST", admin_integration, api_key_required=False)
        au.add_resource("update").add_method("POST", admin_integration, api_key_required=False)
        au.add_resource("delete").add_method("POST", admin_integration, api_key_required=False)

        plan = api.add_usage_plan(
            "NuwaUsagePlan",
            name=f"{prefix}-usage",
            throttle=apigw.ThrottleSettings(rate_limit=100, burst_limit=200),
            quota=apigw.QuotaSettings(limit=100_000, period=apigw.Period.MONTH),
        )
        api_key = api.add_api_key(
            "NuwaApiKey",
            api_key_name=f"{prefix}-api-key",
            description="Enviar en cabecera x-api-key",
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(api=api, stage=api.deployment_stage)

        # No usar plan.usage_plan_id en env/IAM de admin_fn: crea dependencia circular
        # (usage plan → stage → deployment → métodos → admin Lambda → usage plan).
        # El nombre del plan es estable; la Lambda resuelve el id en runtime.
        admin_fn.add_environment("NUWA_APIGW_USAGE_PLAN_NAME", f"{prefix}-usage")
        admin_fn.add_environment("NUWA_RESOURCE_PREFIX", prefix)
        admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "apigateway:GET",
                    "apigateway:POST",
                    "apigateway:DELETE",
                ],
                resources=[
                    f"arn:aws:apigateway:{self.region}::/apikeys",
                    f"arn:aws:apigateway:{self.region}::/apikeys/*",
                    f"arn:aws:apigateway:{self.region}::/usageplans",
                    f"arn:aws:apigateway:{self.region}::/usageplans/*",
                ],
            )
        )

        for construct in (
            sources_fn,
            chunks_fn,
            search_fn,
            reports_fn,
            admin_fn,
            auth_fn,
            api,
            plan,
            api_key,
            url_param,
            secret,
            db_secret,
            app_crypto_secret,
            docs_bucket,
        ):
            Tags.of(construct).add(TAG_PROJECT, TAG_VALUE_PROJECT)
            Tags.of(construct).add(TAG_ENVIRONMENT, env)
            Tags.of(construct).add(TAG_NAME_PREFIX, prefix)
            Tags.of(construct).add(TAG_MANAGED_BY, TAG_VALUE_MANAGED_BY)

        CfnOutput(self, "ApiBaseUrl", value=api.url, description="Base URL (stage prod)")
        CfnOutput(
            self,
            "NuwaResourcePrefix",
            value=prefix,
            description="Prefijo de nombres físicos (Lambdas, API, API key, usage plan); filtrar/borrar con CLI o por tags nuwa:*",
        )
        CfnOutput(
            self,
            "ApiKeyId",
            value=api_key.key_id,
            description=(
                "ID de la API Key de plataforma (Nuwa/super_admin); el valor secreto solo vía consola o "
                "get-api-key --include-value. Cada compañía nueva recibe su propia key al crearla (ver docs)."
            ),
        )
        CfnOutput(
            self,
            "UsagePlanId",
            value=plan.usage_plan_id,
            description="Usage plan al que se asocian la key de plataforma y las keys por tenant.",
        )
        CfnOutput(
            self,
            "SupabaseUrlParameterName",
            value=url_param.parameter_name,
            description="Parámetro SSM con la URL del proyecto Supabase",
        )
        CfnOutput(
            self,
            "SupabaseSecretArn",
            value=secret.secret_arn,
            description="ARN del secreto con service_role JWT",
        )
        CfnOutput(
            self,
            "DatabaseSecretArn",
            value=db_secret.secret_arn,
            description="ARN del secreto JSON PostgreSQL (host, port, dbname, user, password, sslmode)",
        )
        CfnOutput(
            self,
            "AppCryptoSecretArn",
            value=app_crypto_secret.secret_arn,
            description="ARN JSON jwt_signing_secret + fernet_key (cifrado apigw_key_secret). Rotar valores en prod.",
        )
        CfnOutput(
            self,
            "OpenApiDocsWebsiteUrl",
            value=docs_bucket.bucket_website_url,
            description="Sitio estático público con Swagger UI + openapi.yaml (S3 website endpoint, HTTP).",
        )

        # Documentación operativa
        CfnOutput(
            self,
            "ConfigureSupabaseHint",
            value=(
                "1) Edita SSM URL si no usaste -c supabaseUrl. "
                "2) put-secret-value en el ARN del secreto. "
                "3) Login: POST .../v1/auth/login (sin x-api-key). "
                "4) Resto: x-api-key de plataforma o tenant."
            ),
        )
