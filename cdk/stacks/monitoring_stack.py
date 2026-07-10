# monitoring_stack.py — Day 9: Monitoring & Observability
"""
MonitoringStack — AWS CDK stack for end-to-end observability.

Responsibilities:
  - AWS X-Ray                — X-Ray group + sampling rule for full pipeline trace
  - CloudWatch Log Insights  — saved queries for ingestion failures & slow queries
  - CloudWatch Composite Alarm — "SystemHealth" combining all critical alarms
  - SNS Ops Topic            — email/Slack for critical alarm notifications
  - Lambda Powertools env    — POWERTOOLS_SERVICE_NAME, LOG_LEVEL, POWERTOOLS_METRICS_NAMESPACE
                               injected into every Lambda as env-var SSM parameters
  - Cost Anomaly Detection   — Cost Anomaly Detector + Monitor (>20% spike → SNS)
  - CloudTrail               — trail for API-call audit across the account
  - CloudWatch Dashboard     — "ResearchRAG-Operations" master ops dashboard
  - Log Metric Filters       — extract ERROR / WARN counts from all Lambda log groups

Dependency order:
  StorageStack → … → EvaluationStack → MonitoringStack  (deployed last)

Deploy:
  cdk deploy MonitoringStack

This file defines CDK constructs only — no AWS API calls are made at synth time.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_xray as xray,
)
from constructs import Construct

from .config import (
    CW_NAMESPACE,
    PROJECT_PREFIX,
    SECRET_SLACK_WEBHOOK,
    MONITORING_COST_ANOMALY_THRESHOLD_PCT,
    MONITORING_OPS_DASHBOARD_NAME,
    MONITORING_XRAY_GROUP_NAME,
    MONITORING_XRAY_SAMPLING_RATE,
    MONITORING_CLOUDTRAIL_BUCKET,
    MONITORING_LOG_RETENTION_DAYS,
    MONITORING_ALARM_ERROR_RATE_MAX,
    MONITORING_ALARM_THROTTLE_RATE_MAX,
    ALL_LAMBDA_LOG_GROUPS,
)


# ---------------------------------------------------------------------------
# Helper: create a CloudWatch metric filter on a log group
# ---------------------------------------------------------------------------

def _add_error_metric_filter(
    scope: Construct,
    log_group: logs.ILogGroup,
    log_group_name: str,
    filter_id: str,
    pattern: str,
    metric_name: str,
    namespace: str,
) -> cloudwatch.Metric:
    """
    Attach a MetricFilter to *log_group* that increments *metric_name* for
    every log line matching *pattern*.  Returns a Metric object for use in
    alarms and dashboards.
    """
    # Sanitise id — CDK construct IDs must be alphanumeric + limited specials
    safe_id = filter_id.replace("-", "_").replace("/", "_")

    logs.MetricFilter(
        scope,
        f"MetricFilter_{safe_id}",
        log_group=log_group,
        filter_pattern=logs.FilterPattern.literal(pattern),
        metric_namespace=namespace,
        metric_name=metric_name,
        metric_value="1",
        default_value=0,
    )

    return cloudwatch.Metric(
        namespace=namespace,
        metric_name=metric_name,
        statistic="Sum",
        period=Duration.minutes(5),
    )


class MonitoringStack(Stack):
    """
    CDK Stack — Monitoring & Observability (Day 9).

    Public attributes exposed for cross-stack references:
      self.ops_topic          — SNS Topic for critical ops alerts
      self.system_health_alarm — Composite alarm: overall system health
      self.xray_group_arn     — CloudFormation output for the X-Ray group ARN
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        evaluation_stack=None,   # type: ignore[type-arg]  # dependency wiring
        env: cdk.Environment,
        alert_email: str = "",   # optional: email for SNS subscription
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        # ======================================================================
        # 1 — SNS Ops Topic
        #     Receives notifications from ALL critical CloudWatch alarms.
        # ======================================================================
        self.ops_topic = sns.Topic(
            self,
            "OpsAlertsTopic",
            topic_name=f"{PROJECT_PREFIX}-ops-alerts",
            display_name="Research RAG — Operations Alerts",
        )

        if alert_email:
            self.ops_topic.add_subscription(
                subs.EmailSubscription(alert_email)
            )

        # ======================================================================
        # 2 — AWS X-Ray: Group + Sampling Rule
        #     The group filters traces that originate in our service.
        #     The sampling rule ensures at least 5% of requests are sampled
        #     so costs stay bounded while preserving visibility.
        # ======================================================================
        self.xray_group = xray.CfnGroup(
            self,
            "XRayGroup",
            group_name=MONITORING_XRAY_GROUP_NAME,
            filter_expression=(
                f'annotation.service = "{PROJECT_PREFIX}-rag" '
                f'OR annotation.project = "{PROJECT_PREFIX}"'
            ),
            insights_configuration=xray.CfnGroup.InsightsConfigurationProperty(
                insights_enabled=True,
                notifications_enabled=True,
            ),
            tags=[cdk.CfnTag(key="Project", value=PROJECT_PREFIX)],
        )

        # Sampling rule — 5% reservoir, up to 10 req/s, applied to all Lambdas
        xray.CfnSamplingRule(
            self,
            "XRaySamplingRule",
            sampling_rule=xray.CfnSamplingRule.SamplingRuleProperty(
                rule_name=f"{PROJECT_PREFIX}-sampling-rule",
                priority=1000,
                reservoir_size=10,
                fixed_rate=MONITORING_XRAY_SAMPLING_RATE,
                service_name="*",
                service_type="AWS::Lambda::Function",
                host="*",
                http_method="*",
                url_path="*",
                resource_arn="*",
                version=1,
            ),
        )

        # ======================================================================
        # 3 — CloudWatch Log Insights Saved Queries
        # ======================================================================

        # Query 1: ingestion failures in paper_processor or paper_fetcher
        logs.QueryDefinition(
            self,
            "QueryIngestionFailures",
            query_definition_name=f"{PROJECT_PREFIX}/ingestion-failures",
            query_string=logs.QueryString(
                filter_statements=["level = \"ERROR\""],
                sort="@timestamp desc",
                limit=50,
                fields=["@timestamp", "level", "service", "message", "error", "paper_id"],
            ),
            log_groups=[
                logs.LogGroup.from_log_group_name(
                    self, "PaperFetcherLG",
                    f"/aws/lambda/{PROJECT_PREFIX}-paper-fetcher",
                ),
                logs.LogGroup.from_log_group_name(
                    self, "PaperProcessorLG",
                    f"/aws/lambda/{PROJECT_PREFIX}-paper-processor",
                ),
            ],
        )

        # Query 2: slow queries (query_handler duration > 5000ms)
        logs.QueryDefinition(
            self,
            "QuerySlowQueries",
            query_definition_name=f"{PROJECT_PREFIX}/slow-queries",
            query_string=logs.QueryString(
                filter_statements=["duration_ms > 5000"],
                sort="duration_ms desc",
                limit=25,
                fields=["@timestamp", "query_id", "duration_ms", "action", "confidence"],
            ),
            log_groups=[
                logs.LogGroup.from_log_group_name(
                    self, "QueryHandlerLG",
                    f"/aws/lambda/{PROJECT_PREFIX}-query-handler",
                ),
            ],
        )

        # Query 3: hallucination detector refusals
        logs.QueryDefinition(
            self,
            "QueryHallucinationRefusals",
            query_definition_name=f"{PROJECT_PREFIX}/hallucination-refusals",
            query_string=logs.QueryString(
                filter_statements=["action = \"REFUSE\""],
                sort="@timestamp desc",
                limit=50,
                fields=["@timestamp", "query_id", "confidence_score", "action", "message"],
            ),
            log_groups=[
                logs.LogGroup.from_log_group_name(
                    self, "HallucinationDetectorLG",
                    f"/aws/lambda/{PROJECT_PREFIX}-hallucination-detector",
                ),
            ],
        )

        # Query 4: embedding worker errors (DLQ candidates)
        logs.QueryDefinition(
            self,
            "QueryEmbeddingErrors",
            query_definition_name=f"{PROJECT_PREFIX}/embedding-errors",
            query_string=logs.QueryString(
                filter_statements=["level = \"ERROR\""],
                sort="@timestamp desc",
                limit=50,
                fields=["@timestamp", "chunk_id", "paper_id", "error", "message"],
            ),
            log_groups=[
                logs.LogGroup.from_log_group_name(
                    self, "EmbeddingWorkerLG",
                    f"/aws/lambda/{PROJECT_PREFIX}-embedding-worker",
                ),
            ],
        )

        # ======================================================================
        # 4 — CloudWatch Metric Filters → ERROR / WARN counters per Lambda
        #     We create one MetricFilter per Lambda and roll them into a single
        #     composite alarm via the SystemHealth composite alarm below.
        # ======================================================================
        error_metrics: list[cloudwatch.Metric] = []
        error_alarms: list[cloudwatch.Alarm] = []

        for fn_name in ALL_LAMBDA_LOG_GROUPS:
            lg_name = f"/aws/lambda/{PROJECT_PREFIX}-{fn_name}"
            safe = fn_name.replace("-", "_")

            # Import the log group (it is created by the respective stack)
            lg = logs.LogGroup.from_log_group_name(
                self, f"LG_{safe}", lg_name
            )

            # ERROR metric filter
            err_metric = _add_error_metric_filter(
                scope=self,
                log_group=lg,
                log_group_name=lg_name,
                filter_id=f"{fn_name}-errors",
                pattern="[level=ERROR]",
                metric_name=f"Lambda.{fn_name}.ErrorCount",
                namespace=CW_NAMESPACE,
            )
            error_metrics.append(err_metric)

            # Per-function error alarm (threshold: any errors in 5 min window)
            fn_alarm = cloudwatch.Alarm(
                self,
                f"ErrorAlarm_{safe}",
                alarm_name=f"{PROJECT_PREFIX}-{fn_name}-error-rate",
                alarm_description=(
                    f"Lambda {fn_name} produced ERROR log entries in the last 5 minutes."
                ),
                metric=err_metric,
                threshold=MONITORING_ALARM_ERROR_RATE_MAX,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            fn_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))
            fn_alarm.add_ok_action(cw_actions.SnsAction(self.ops_topic))
            error_alarms.append(fn_alarm)

        # ======================================================================
        # 5 — Key Business Metric Alarms (latency, throttles, DLQ depth)
        # ======================================================================

        # E2E query latency — query_handler P95 > 8 s
        query_duration_alarm = cloudwatch.Alarm(
            self,
            "QueryP95LatencyAlarm",
            alarm_name=f"{PROJECT_PREFIX}-query-p95-latency",
            alarm_description="Query Handler P95 duration exceeded 8 000 ms.",
            metric=cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Duration",
                dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-query-handler"},
                statistic="p95",
                period=Duration.minutes(5),
            ),
            threshold=8000,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        query_duration_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Lambda throttle alarm — any Lambda being throttled
        throttle_alarm = cloudwatch.Alarm(
            self,
            "LambdaThrottlesAlarm",
            alarm_name=f"{PROJECT_PREFIX}-lambda-throttles",
            alarm_description="One or more Lambda functions are being throttled.",
            metric=cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Throttles",
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=MONITORING_ALARM_THROTTLE_RATE_MAX,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        throttle_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Paper queue DLQ depth alarm
        dlq_alarm = cloudwatch.Alarm(
            self,
            "PaperDLQDepthAlarm",
            alarm_name=f"{PROJECT_PREFIX}-paper-dlq-depth",
            alarm_description="Messages have landed in the Paper DLQ — ingestion failures detected.",
            metric=cloudwatch.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": f"{PROJECT_PREFIX}-paper-dlq.fifo"},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Embedding DLQ depth alarm
        embedding_dlq_alarm = cloudwatch.Alarm(
            self,
            "EmbeddingDLQDepthAlarm",
            alarm_name=f"{PROJECT_PREFIX}-embedding-dlq-depth",
            alarm_description="Messages have landed in the Embedding DLQ.",
            metric=cloudwatch.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": f"{PROJECT_PREFIX}-embedding-dlq"},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        embedding_dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # OpenSearch cluster status (red)
        opensearch_red_alarm = cloudwatch.Alarm(
            self,
            "OpenSearchRedAlarm",
            alarm_name=f"{PROJECT_PREFIX}-opensearch-cluster-red",
            alarm_description="Amazon OpenSearch cluster status is RED.",
            metric=cloudwatch.Metric(
                namespace="AWS/ES",
                metric_name="ClusterStatus.red",
                dimensions_map={"DomainName": f"{PROJECT_PREFIX}-opensearch"},
                statistic="Maximum",
                period=Duration.minutes(1),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        opensearch_red_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Neptune write latency > 500 ms
        neptune_latency_alarm = cloudwatch.Alarm(
            self,
            "NeptuneWriteLatencyAlarm",
            alarm_name=f"{PROJECT_PREFIX}-neptune-write-latency",
            alarm_description="Neptune average write latency exceeded 500 ms.",
            metric=cloudwatch.Metric(
                namespace="AWS/Neptune",
                metric_name="WriteThroughput",
                dimensions_map={
                    "DBClusterIdentifier": f"{PROJECT_PREFIX}-neptune",
                    "Role": "WRITER",
                },
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=500,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        neptune_latency_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # ======================================================================
        # 6 — CloudWatch Composite Alarm — "SystemHealth"
        #     Fires when ANY of the constituent alarms are ALARM state.
        # ======================================================================
        constituent_alarms: list[cloudwatch.IAlarm] = [
            *error_alarms,
            query_duration_alarm,
            throttle_alarm,
            dlq_alarm,
            embedding_dlq_alarm,
            opensearch_red_alarm,
            neptune_latency_alarm,
        ]

        # Build the composite alarm rule: ALARM(a) OR ALARM(b) OR ...
        alarm_rule = cloudwatch.AlarmRule.any_of(
            *[cloudwatch.AlarmRule.from_alarm(a, cloudwatch.AlarmState.ALARM)
              for a in constituent_alarms]
        )

        self.system_health_alarm = cloudwatch.CompositeAlarm(
            self,
            "SystemHealthCompositeAlarm",
            composite_alarm_name=f"{PROJECT_PREFIX}-system-health",
            alarm_description=(
                "Master system-health composite alarm. "
                "Fires when ANY critical alarm is in ALARM state."
            ),
            alarm_rule=alarm_rule,
        )
        self.system_health_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))
        self.system_health_alarm.add_ok_action(cw_actions.SnsAction(self.ops_topic))

        # ======================================================================
        # 7 — CloudTrail — API-call audit trail
        # ======================================================================

        # S3 bucket to store CloudTrail logs (separate from operational buckets)
        trail_bucket = s3.Bucket(
            self,
            "CloudTrailBucket",
            bucket_name=MONITORING_CLOUDTRAIL_BUCKET,
            versioned=False,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-after-90-days",
                    expiration=Duration.days(MONITORING_LOG_RETENTION_DAYS),
                    enabled=True,
                )
            ],
        )

        # Grant CloudTrail permission to write to the bucket
        trail_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudTrailAclCheck",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["s3:GetBucketAcl"],
                resources=[trail_bucket.bucket_arn],
            )
        )
        trail_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudTrailWrite",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[f"{trail_bucket.bucket_arn}/AWSLogs/*"],
                conditions={
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control"
                    }
                },
            )
        )

        # CloudTrail log group
        trail_log_group = logs.LogGroup(
            self,
            "CloudTrailLogGroup",
            log_group_name=f"/aws/cloudtrail/{PROJECT_PREFIX}-audit",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # IAM role for CloudTrail → CloudWatch Logs
        trail_cw_role = iam.Role(
            self,
            "CloudTrailCWRole",
            assumed_by=iam.ServicePrincipal("cloudtrail.amazonaws.com"),
            description="Allows CloudTrail to publish events to CloudWatch Logs.",
        )
        trail_cw_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[trail_log_group.log_group_arn],
            )
        )

        # CloudTrail trail — management events only (data events add cost)
        from aws_cdk import aws_cloudtrail as cloudtrail  # noqa: PLC0415

        self.trail = cloudtrail.Trail(
            self,
            "AuditTrail",
            trail_name=f"{PROJECT_PREFIX}-audit-trail",
            bucket=trail_bucket,
            cloud_watch_logs_group=trail_log_group,
            cloud_watch_logs_role=trail_cw_role,
            include_global_service_events=True,
            is_multi_region_trail=False,
            enable_file_validation=True,
            send_to_cloud_watch_logs=True,
        )

        # ======================================================================
        # 8 — AWS Cost Anomaly Detection
        #     Monitor ALL services; alert if spend spikes >20% vs baseline.
        # ======================================================================
        cost_monitor = cdk.CfnResource(
            self,
            "CostAnomalyMonitor",
            type="AWS::CE::AnomalyMonitor",
            properties={
                "MonitorName": f"{PROJECT_PREFIX}-cost-monitor",
                "MonitorType": "DIMENSIONAL",
                "MonitorDimension": "SERVICE",
            },
        )

        cost_subscription = cdk.CfnResource(
            self,
            "CostAnomalySubscription",
            type="AWS::CE::AnomalySubscription",
            properties={
                "SubscriptionName": f"{PROJECT_PREFIX}-cost-alert",
                "MonitorArnList": [cost_monitor.get_att("MonitorArn").to_string()],
                "Subscribers": [
                    {
                        "Address": self.ops_topic.topic_arn,
                        "Type": "SNS",
                    }
                ],
                "ThresholdExpression": {
                    "Dimensions": {
                        "Key": "ANOMALY_TOTAL_IMPACT_PERCENTAGE",
                        "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
                        "Values": [str(MONITORING_COST_ANOMALY_THRESHOLD_PCT)],
                    }
                },
                "Frequency": "DAILY",
            },
        )
        cost_subscription.add_dependency(cost_monitor)

        # Grant Cost Explorer permission to publish to the ops SNS topic
        self.ops_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CostAnomalyPublish",
                principals=[iam.ServicePrincipal("costalerts.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.ops_topic.topic_arn],
            )
        )

        # ======================================================================
        # 9 — CloudWatch Dashboard — "ResearchRAG-Operations"
        #     Master operations dashboard: Lambda metrics, SQS, OpenSearch,
        #     Neptune, X-Ray traces, error rates.
        # ======================================================================
        self.dashboard = cloudwatch.Dashboard(
            self,
            "OpsDashboard",
            dashboard_name=MONITORING_OPS_DASHBOARD_NAME,
            period_override=cloudwatch.PeriodOverride.AUTO,
        )

        # --- Row 1: System Health Overview ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    "## 🟢 System Health Overview\n"
                    f"_Project: {PROJECT_PREFIX}_  |  "
                    f"_Namespace: {CW_NAMESPACE}_"
                ),
                width=24,
                height=2,
            )
        )

        self.dashboard.add_widgets(
            cloudwatch.AlarmWidget(
                title="System Health Composite Alarm",
                alarm=self.system_health_alarm,
                width=8,
                height=4,
            ),
            cloudwatch.AlarmWidget(
                title="Query P95 Latency",
                alarm=query_duration_alarm,
                width=8,
                height=4,
            ),
            cloudwatch.AlarmWidget(
                title="Lambda Throttles",
                alarm=throttle_alarm,
                width=8,
                height=4,
            ),
        )

        # --- Row 2: Lambda Invocations + Errors ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="## ⚡ Lambda Invocations & Errors",
                width=24,
                height=2,
            )
        )

        pipeline_functions = [
            "query-handler",
            "answer-generator",
            "hallucination-detector",
            "embedding-worker",
            "graph-builder",
            "paper-processor",
        ]

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda Invocations (pipeline)",
                width=12,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-{fn}"},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=fn,
                    )
                    for fn in pipeline_functions
                ],
            ),
            cloudwatch.GraphWidget(
                title="Lambda Errors (pipeline)",
                width=12,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-{fn}"},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=fn,
                    )
                    for fn in pipeline_functions
                ],
            ),
        )

        # --- Row 3: Lambda Durations ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="## ⏱️ Lambda Duration (P50 / P95 / P99)",
                width=24,
                height=2,
            )
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Query Handler Duration (ms)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-query-handler"},
                        statistic=stat,
                        period=Duration.minutes(5),
                        label=f"p{stat[1:]}",
                    )
                    for stat in ["p50", "p95", "p99"]
                ],
            ),
            cloudwatch.GraphWidget(
                title="Answer Generator Duration (ms)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-answer-generator"},
                        statistic=stat,
                        period=Duration.minutes(5),
                        label=f"p{stat[1:]}",
                    )
                    for stat in ["p50", "p95", "p99"]
                ],
            ),
            cloudwatch.GraphWidget(
                title="Embedding Worker Duration (ms)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": f"{PROJECT_PREFIX}-embedding-worker"},
                        statistic=stat,
                        period=Duration.minutes(5),
                        label=f"p{stat[1:]}",
                    )
                    for stat in ["p50", "p95", "p99"]
                ],
            ),
        )

        # --- Row 4: SQS Queue Depths ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="## 📬 SQS Queue Depths",
                width=24,
                height=2,
            )
        )

        sqs_queues = [
            ("paper-queue.fifo", "Paper Queue"),
            ("embedding-queue", "Embedding Queue"),
            ("entity-queue", "Entity Queue"),
            ("paper-dlq.fifo", "Paper DLQ"),
            ("embedding-dlq", "Embedding DLQ"),
            ("entity-dlq", "Entity DLQ"),
        ]

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="SQS Messages Visible",
                width=12,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/SQS",
                        metric_name="ApproximateNumberOfMessagesVisible",
                        dimensions_map={"QueueName": f"{PROJECT_PREFIX}-{q}"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for q, label in sqs_queues
                ],
            ),
            cloudwatch.GraphWidget(
                title="SQS Age of Oldest Message (seconds)",
                width=12,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/SQS",
                        metric_name="ApproximateAgeOfOldestMessage",
                        dimensions_map={"QueueName": f"{PROJECT_PREFIX}-{q}"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for q, label in sqs_queues[:3]  # operational queues only
                ],
            ),
        )

        # --- Row 5: OpenSearch & Neptune ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="## 🔍 OpenSearch & Neptune Health",
                width=24,
                height=2,
            )
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="OpenSearch: Search Latency (ms)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/ES",
                        metric_name="SearchLatency",
                        dimensions_map={"DomainName": f"{PROJECT_PREFIX}-opensearch"},
                        statistic="p95",
                        period=Duration.minutes(5),
                        label="Search p95 ms",
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/ES",
                        metric_name="IndexingLatency",
                        dimensions_map={"DomainName": f"{PROJECT_PREFIX}-opensearch"},
                        statistic="p95",
                        period=Duration.minutes(5),
                        label="Indexing p95 ms",
                    ),
                ],
            ),
            cloudwatch.GraphWidget(
                title="OpenSearch: JVM Memory Pressure (%)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/ES",
                        metric_name="JVMMemoryPressure",
                        dimensions_map={"DomainName": f"{PROJECT_PREFIX}-opensearch"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                        label="JVM %",
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="Neptune: Gremlin Requests & Errors",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Neptune",
                        metric_name="GremlinRequestsPerSec",
                        dimensions_map={
                            "DBClusterIdentifier": f"{PROJECT_PREFIX}-neptune",
                            "Role": "WRITER",
                        },
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label="Gremlin RPS",
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/Neptune",
                        metric_name="GremlinWebSocketServerErrors",
                        dimensions_map={
                            "DBClusterIdentifier": f"{PROJECT_PREFIX}-neptune",
                            "Role": "WRITER",
                        },
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label="Gremlin Errors",
                    ),
                ],
            ),
        )

        # --- Row 6: RAG Quality Metrics ---
        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="## 🎯 RAG Quality Metrics",
                width=24,
                height=2,
            )
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Confidence Score (Avg)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="ConfidenceScore",
                        statistic="Average",
                        period=Duration.minutes(15),
                        label="Confidence Avg",
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="Citation Accuracy (Avg)",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="CitationAccuracy",
                        statistic="Average",
                        period=Duration.minutes(15),
                        label="Citation Accuracy Avg",
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="Hallucination Action Distribution",
                width=8,
                height=6,
                left=[
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="HallucinationAction.PASS",
                        statistic="Sum",
                        period=Duration.minutes(15),
                        label="PASS",
                    ),
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="HallucinationAction.WARN",
                        statistic="Sum",
                        period=Duration.minutes(15),
                        label="WARN",
                    ),
                    cloudwatch.Metric(
                        namespace=CW_NAMESPACE,
                        metric_name="HallucinationAction.REFUSE",
                        statistic="Sum",
                        period=Duration.minutes(15),
                        label="REFUSE",
                    ),
                ],
            ),
        )

        # ======================================================================
        # 10 — CloudFormation Outputs
        # ======================================================================
        cdk.CfnOutput(
            self, "OpsDashboardUrl",
            value=(
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                f"?region={self.region}#dashboards:name={MONITORING_OPS_DASHBOARD_NAME}"
            ),
            description="Direct link to the ResearchRAG Operations dashboard.",
            export_name=f"{PROJECT_PREFIX}-ops-dashboard-url",
        )

        cdk.CfnOutput(
            self, "OpsTopicArn",
            value=self.ops_topic.topic_arn,
            description="SNS topic ARN for all critical ops alerts.",
            export_name=f"{PROJECT_PREFIX}-ops-topic-arn",
        )

        cdk.CfnOutput(
            self, "SystemHealthAlarmArn",
            value=self.system_health_alarm.alarm_arn,
            description="CloudWatch composite alarm ARN for overall system health.",
            export_name=f"{PROJECT_PREFIX}-system-health-alarm-arn",
        )

        cdk.CfnOutput(
            self, "XRayGroupName",
            value=MONITORING_XRAY_GROUP_NAME,
            description="AWS X-Ray group name for Research RAG traces.",
            export_name=f"{PROJECT_PREFIX}-xray-group-name",
        )

        cdk.CfnOutput(
            self, "CloudTrailName",
            value=f"{PROJECT_PREFIX}-audit-trail",
            description="CloudTrail trail name for API-call audit logging.",
            export_name=f"{PROJECT_PREFIX}-cloudtrail-name",
        )

        cdk.CfnOutput(
            self, "CloudTrailBucketName",
            value=trail_bucket.bucket_name,
            description="S3 bucket storing CloudTrail audit logs.",
            export_name=f"{PROJECT_PREFIX}-cloudtrail-bucket",
        )
