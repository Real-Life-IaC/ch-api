from typing import TypedDict

import aws_cdk as cdk

from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_ssm as ssm
from constructs import Construct
from typing_extensions import NotRequired
from typing_extensions import Unpack
from aws_cdk import aws_kms as kms


class Params(TypedDict):
    """Parameters for the UtilStaticSite class."""

    timeout_seconds: int
    memory_size: int
    domain_name: str
    hosted_zone_type: NotRequired[str | None]
    log_retention: NotRequired[logs.RetentionDays]


class B1LambdaApi(Construct):
    """Lambda API construct."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        **kwargs: Unpack[Params],
    ) -> None:
        super().__init__(scope, id)

        # Read the kwargs
        timeout_seconds = kwargs["timeout_seconds"]
        memory_size = kwargs["memory_size"]
        domain_name = kwargs["domain_name"]
        log_retention = kwargs.get(
            "log_retention", logs.RetentionDays.ONE_WEEK
        )
        hosted_zone_type = kwargs.get("hosted_zone_type", "private")
        cors_origins = [
                    f"https://*.{domain_name}",
                    f"https://{domain_name}",
                    f"https://api.{domain_name}",
                ]
        
        vpc_id = ssm.StringParameter.value_from_lookup(
            scope=self,
            parameter_name="/platform/vpc/id",
        )

        stage_name = ssm.StringParameter.value_from_lookup(
            scope=self,
            parameter_name="/platform/stage",
        )

        hosted_zone_name = ssm.StringParameter.value_for_string_parameter(
            scope=self,
            parameter_name=f"/platform/dns/{domain_name}/{hosted_zone_type}-hosted-zone/name",
        )

        hosted_zone_id = ssm.StringParameter.value_for_string_parameter(
            scope=self,
            parameter_name=f"/platform/dns/{domain_name}/{hosted_zone_type}-hosted-zone/id",
        )

        certificate_arn = ssm.StringParameter.value_for_string_parameter(
            scope=self,
            parameter_name=f"/platform/dns/{domain_name}/{hosted_zone_type}-hosted-zone/certificate/arn",
        )

        certificate = acm.Certificate.from_certificate_arn(
            scope=self,
            id="Certificate",
            certificate_arn=certificate_arn,
        )

        kms_key_arn = ssm.StringParameter.value_for_string_parameter(
            scope=self,
            parameter_name="/platform/kms/default-key/arn",
        )

        vpc = ec2.Vpc.from_lookup(
            scope=self,
            id="Vpc",
            vpc_id=vpc_id,
        )

        kms_key = kms.Key.from_key_arn(
            scope=self,
            id="KmsKey",
            key_arn=kms_key_arn,
        )

        self.hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            scope=self,
            id="HostedZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=hosted_zone_name,
        )

        self.security_group = ec2.SecurityGroup(
            scope=self,
            id="SecurityGroup",
            vpc=vpc,
            description="Security group for the Lambda function",
        )

        self.table = dynamodb.TableV2(
            scope=self,
            id="Downloads",
            billing=dynamodb.Billing.on_demand(),
            partition_key=dynamodb.Attribute(
                name="id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="created_at",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        # Create Lambda function
        self.function = _lambda.DockerImageFunction(
            scope=self,
            id="Function",
            description="Lambda function for the API",
            code=_lambda.DockerImageCode.from_image_asset(
                directory="app",
                file="Dockerfile.lambda",
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            timeout=cdk.Duration.seconds(amount=timeout_seconds),
            memory_size=memory_size,
            log_retention=log_retention,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_group_name="PrivateSubnet"
            ),
            security_groups=[self.security_group],
            tracing=_lambda.Tracing.ACTIVE,
            environment_encryption=kms_key,
            environment={
                "TABLE_NAME": self.table.table_name,
            },
        )

        self.table.grant_read_write_data(self.function)

        # Trigger lambda every 4 minutes to keep it warm
        event_rule = events.Rule(
            scope=self,
            id="KeepWarmSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(4)),
        )
        event_rule.add_target(targets.LambdaFunction(handler=self.function))  # type: ignore

        # Create API Gateway
        lambda_api = apigateway.LambdaRestApi(
            scope=self,
            id="Api",
            handler=self.function,
            domain_name=apigateway.DomainNameOptions(
                domain_name=f"api.{domain_name}", certificate=certificate,
                endpoint_type=apigateway.EndpointType.REGIONAL,
                security_policy=apigateway.SecurityPolicy.TLS_1_2,
            ),
            proxy=False,
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=cors_origins,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=["*"],
            ),
            cloud_watch_role=True,
            api_key_source_type=apigateway.ApiKeySourceType.HEADER,
            deploy_options=apigateway.StageOptions(
                stage_name=stage_name,
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=True,
                metrics_enabled=True,
                tracing_enabled=True,
            ),
        )

        apigateway.UsagePlan(
            scope=self,
            id="UsagePlan",
            api_stages=[
                apigateway.UsagePlanPerApiStage(
                    api=lambda_api,
                    stage=lambda_api.deployment_stage,
                )
            ],
            throttle=apigateway.ThrottleSettings(
                rate_limit=2,
                burst_limit=5,
            ),
        )

        downloads = lambda_api.root.add_resource("downloads")
        downloads.add_method("POST", api_key_required=False)

        downloads_count = lambda_api.root.add_resource("downloads:count")
        downloads_count.add_method("GET", api_key_required=False)

        # Add docs endpoint
        docs = lambda_api.root.add_resource("docs")
        docs.add_method("GET", api_key_required=False)
        openapi = lambda_api.root.add_resource("openapi.json")
        openapi.add_method("GET", api_key_required=False)

        route53.ARecord(
            scope=self,
            id="ARecord",
            zone=self.hosted_zone,
            record_name="api",
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGateway(lambda_api),
            ),
        )

        ssm.StringParameter(
            scope=self,
            id="RestApiId",
            parameter_name="/api/rest-api/id",
            string_value=lambda_api.rest_api_id,
            description="The ID of the REST API",
        )

        ssm.StringParameter(
            scope=self,
            id="RestApiUrl",
            parameter_name="/api/rest-api/url",
            string_value=lambda_api.url,
            description="The URL of the REST API",
        )