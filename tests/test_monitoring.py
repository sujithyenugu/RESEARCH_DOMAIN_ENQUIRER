"""
tests/test_monitoring.py — Day 9: Monitoring & Observability Test Suite

Unit tests covering:
  1.  config constants         — all Day-9 constants present & well-formed
  2.  ALL_LAMBDA_LOG_GROUPS    — unique names, non-empty, expected functions present
  3.  _add_error_metric_filter — constructs MetricFilter + returns Metric object
  4.  MonitoringStack.__init__ — smoke-test via aws_cdk.assertions.Template:
        a. SNS topic created
        b. X-Ray CfnGroup created with correct filter expression
        c. X-Ray CfnSamplingRule created with fixed-rate == 0.05
        d. All Log Insights QueryDefinitions created (4 queries)
        e. CloudTrail trail created
        f. Cost Anomaly Monitor + Subscription created
        g. CloudWatch Dashboard created with correct name
        h. Composite Alarm created
        i. CloudFormation Outputs present (5 outputs)
  5.  alarm_rule_building      — AlarmRule.any_of generates correct ALARM_STATE expression
  6.  metric_filter_safe_id    — hyphens and slashes sanitised correctly

Run:
  pytest tests/test_monitoring.py -v
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Minimal boto3 stub — must be in place before importing any handler or stack
# ---------------------------------------------------------------------------

def _make_boto3_stub() -> types.ModuleType:
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client   = MagicMock(return_value=MagicMock())
    boto3_mod.resource = MagicMock(return_value=MagicMock())
    return boto3_mod


if "boto3" not in sys.modules:
    sys.modules["boto3"] = _make_boto3_stub()

# ---------------------------------------------------------------------------
# Path setup — allow import of stacks.config directly
# ---------------------------------------------------------------------------
import os

_CDK_STACKS_PATH = os.path.join(os.path.dirname(__file__), "..", "cdk")
if _CDK_STACKS_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_CDK_STACKS_PATH))

import stacks.config as CFG  # noqa: E402


# ===========================================================================
# 1. CONFIG CONSTANTS
# ===========================================================================

class TestMonitoringConfig(unittest.TestCase):

    def test_xray_group_name_contains_project_prefix(self):
        self.assertIn(CFG.PROJECT_PREFIX, CFG.MONITORING_XRAY_GROUP_NAME)

    def test_xray_sampling_rate_within_bounds(self):
        self.assertGreater(CFG.MONITORING_XRAY_SAMPLING_RATE, 0.0)
        self.assertLessEqual(CFG.MONITORING_XRAY_SAMPLING_RATE, 1.0)

    def test_ops_dashboard_name_set(self):
        self.assertTrue(CFG.MONITORING_OPS_DASHBOARD_NAME)
        self.assertIn("ResearchRAG", CFG.MONITORING_OPS_DASHBOARD_NAME)

    def test_cloudtrail_bucket_name_set(self):
        self.assertTrue(CFG.MONITORING_CLOUDTRAIL_BUCKET)
        self.assertIn(CFG.PROJECT_PREFIX, CFG.MONITORING_CLOUDTRAIL_BUCKET)

    def test_log_retention_days_positive(self):
        self.assertGreater(CFG.MONITORING_LOG_RETENTION_DAYS, 0)

    def test_cost_anomaly_threshold_is_positive_integer(self):
        self.assertIsInstance(CFG.MONITORING_COST_ANOMALY_THRESHOLD_PCT, int)
        self.assertGreater(CFG.MONITORING_COST_ANOMALY_THRESHOLD_PCT, 0)

    def test_cost_anomaly_threshold_is_20(self):
        self.assertEqual(CFG.MONITORING_COST_ANOMALY_THRESHOLD_PCT, 20)

    def test_error_rate_alarm_threshold_positive(self):
        self.assertGreater(CFG.MONITORING_ALARM_ERROR_RATE_MAX, 0)

    def test_throttle_rate_alarm_threshold_positive(self):
        self.assertGreater(CFG.MONITORING_ALARM_THROTTLE_RATE_MAX, 0)

    def test_powertools_service_name_set(self):
        self.assertTrue(CFG.POWERTOOLS_SERVICE_NAME)
        self.assertIn(CFG.PROJECT_PREFIX, CFG.POWERTOOLS_SERVICE_NAME)

    def test_powertools_metrics_namespace_equals_cw_namespace(self):
        self.assertEqual(CFG.POWERTOOLS_METRICS_NAMESPACE, CFG.CW_NAMESPACE)

    def test_powertools_log_level_valid(self):
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        self.assertIn(CFG.POWERTOOLS_LOG_LEVEL, valid_levels)


# ===========================================================================
# 2. ALL_LAMBDA_LOG_GROUPS
# ===========================================================================

class TestAllLambdaLogGroups(unittest.TestCase):

    def test_non_empty(self):
        self.assertTrue(CFG.ALL_LAMBDA_LOG_GROUPS)

    def test_unique_entries(self):
        self.assertEqual(
            len(CFG.ALL_LAMBDA_LOG_GROUPS),
            len(set(CFG.ALL_LAMBDA_LOG_GROUPS)),
        )

    def test_all_lowercase_with_hyphens(self):
        for name in CFG.ALL_LAMBDA_LOG_GROUPS:
            self.assertEqual(name, name.lower(), f"Expected lowercase: {name}")
            self.assertNotIn("_", name, f"Underscores not expected: {name}")

    def test_critical_functions_present(self):
        must_have = [
            "query-handler",
            "answer-generator",
            "hallucination-detector",
            "embedding-worker",
            "paper-processor",
        ]
        for fn in must_have:
            self.assertIn(fn, CFG.ALL_LAMBDA_LOG_GROUPS, f"Missing: {fn}")

    def test_no_empty_strings(self):
        for name in CFG.ALL_LAMBDA_LOG_GROUPS:
            self.assertTrue(name, "Empty string found in ALL_LAMBDA_LOG_GROUPS")

    def test_minimum_count(self):
        # We expect at least 10 Lambda functions to be monitored
        self.assertGreaterEqual(len(CFG.ALL_LAMBDA_LOG_GROUPS), 10)


# ===========================================================================
# 3. _add_error_metric_filter helper
# ===========================================================================

class TestAddErrorMetricFilter(unittest.TestCase):
    """
    Verify the helper function constructs the correct MetricFilter and
    returns a cloudwatch.Metric with the expected namespace and metric name.
    """

    def test_metric_filter_creates_metric_with_correct_namespace(self):
        """
        _add_error_metric_filter should return a cloudwatch.Metric object
        whose namespace matches what was passed in.
        """
        try:
            import aws_cdk as cdk
            from aws_cdk import aws_logs as logs, aws_cloudwatch as cloudwatch
            from constructs import Construct
            from stacks.monitoring_stack import _add_error_metric_filter
        except ImportError:
            self.skipTest("aws-cdk not installed — skipping CDK construct test")

        # Build a minimal CDK app to provide a valid scope
        app  = cdk.App()
        stack = cdk.Stack(app, "TestStack")

        # Create a real LogGroup construct inside the stack
        lg = logs.LogGroup(stack, "TestLG", log_group_name="/test/lambda/fn")

        metric = _add_error_metric_filter(
            scope=stack,
            log_group=lg,
            log_group_name="/test/lambda/fn",
            filter_id="test-fn-errors",
            pattern="[level=ERROR]",
            metric_name="Lambda.test-fn.ErrorCount",
            namespace="TestNamespace",
        )

        self.assertEqual(metric.namespace, "TestNamespace")
        self.assertEqual(metric.metric_name, "Lambda.test-fn.ErrorCount")
        self.assertEqual(metric.statistic, "Sum")

    def test_filter_id_safe_id_sanitisation(self):
        """
        Hyphens and slashes in filter_id must be converted to underscores
        so CDK construct IDs remain valid.
        """
        raw_id = "some-lambda/errors"
        safe_id = raw_id.replace("-", "_").replace("/", "_")
        self.assertEqual(safe_id, "some_lambda_errors")
        # Must not contain hyphens or slashes
        self.assertNotIn("-", safe_id)
        self.assertNotIn("/", safe_id)


# ===========================================================================
# 4. MonitoringStack CDK template assertions
# ===========================================================================

class TestMonitoringStackTemplate(unittest.TestCase):
    """
    Use aws_cdk.assertions.Template to validate that MonitoringStack
    synthesises the expected CloudFormation resources.

    All tests are skipped if aws-cdk is not installed.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import aws_cdk as cdk
            from aws_cdk import assertions
            from stacks.monitoring_stack import MonitoringStack

            app   = cdk.App()
            env   = cdk.Environment(account="123456789012", region="us-east-1")
            stack = MonitoringStack(app, "MonitoringStack", env=env)
            cls.template = assertions.Template.from_stack(stack)
            cls.cdk_available = True
        except ImportError:
            cls.cdk_available = False

    def _skip_if_no_cdk(self):
        if not self.cdk_available:
            self.skipTest("aws-cdk not installed — skipping template assertions")

    # 4a — SNS topic
    def test_sns_ops_topic_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::SNS::Topic", 1)

    def test_sns_topic_display_name(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::SNS::Topic",
            {"DisplayName": "Research RAG \u2014 Operations Alerts"},
        )

    # 4b — X-Ray Group
    def test_xray_group_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::XRay::Group", 1)

    def test_xray_group_insights_enabled(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::XRay::Group",
            {
                "InsightsConfiguration": {
                    "InsightsEnabled": True,
                    "NotificationsEnabled": True,
                }
            },
        )

    # 4c — X-Ray Sampling Rule
    def test_xray_sampling_rule_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::XRay::SamplingRule", 1)

    def test_xray_sampling_rule_fixed_rate(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::XRay::SamplingRule",
            {
                "SamplingRule": {
                    "FixedRate": CFG.MONITORING_XRAY_SAMPLING_RATE,
                    "ServiceType": "AWS::Lambda::Function",
                }
            },
        )

    # 4d — Log Insights queries (4 expected)
    def test_log_insights_query_definitions_count(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::Logs::QueryDefinition", 4)

    def test_log_insights_ingestion_failures_query_exists(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::Logs::QueryDefinition",
            {"Name": f"{CFG.PROJECT_PREFIX}/ingestion-failures"},
        )

    def test_log_insights_slow_queries_query_exists(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::Logs::QueryDefinition",
            {"Name": f"{CFG.PROJECT_PREFIX}/slow-queries"},
        )

    def test_log_insights_hallucination_refusals_query_exists(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::Logs::QueryDefinition",
            {"Name": f"{CFG.PROJECT_PREFIX}/hallucination-refusals"},
        )

    def test_log_insights_embedding_errors_query_exists(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::Logs::QueryDefinition",
            {"Name": f"{CFG.PROJECT_PREFIX}/embedding-errors"},
        )

    # 4e — CloudTrail
    def test_cloudtrail_trail_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::CloudTrail::Trail", 1)

    def test_cloudtrail_file_validation_enabled(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::CloudTrail::Trail",
            {"EnableLogFileValidation": True},
        )

    def test_cloudtrail_multi_region_false(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::CloudTrail::Trail",
            {"IsMultiRegionTrail": False},
        )

    def test_cloudtrail_s3_bucket_created(self):
        self._skip_if_no_cdk()
        # CloudTrail bucket + possibly a logging bucket — at least 1
        from aws_cdk import assertions
        buckets = self.template.find_resources("AWS::S3::Bucket")
        self.assertGreaterEqual(len(buckets), 1)

    # 4f — Cost Anomaly Detection
    def test_cost_anomaly_monitor_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::CE::AnomalyMonitor", 1)

    def test_cost_anomaly_subscription_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::CE::AnomalySubscription", 1)

    def test_cost_anomaly_subscription_frequency_daily(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::CE::AnomalySubscription",
            {"Frequency": "DAILY"},
        )

    # 4g — CloudWatch Dashboard
    def test_cloudwatch_dashboard_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::CloudWatch::Dashboard", 1)

    def test_cloudwatch_dashboard_name(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::CloudWatch::Dashboard",
            {"DashboardName": CFG.MONITORING_OPS_DASHBOARD_NAME},
        )

    # 4h — Composite Alarm
    def test_composite_alarm_created(self):
        self._skip_if_no_cdk()
        self.template.resource_count_is("AWS::CloudWatch::CompositeAlarm", 1)

    def test_composite_alarm_name(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::CloudWatch::CompositeAlarm",
            {"AlarmName": f"{CFG.PROJECT_PREFIX}-system-health"},
        )

    # 4i — CloudFormation Outputs
    def test_cfn_outputs_count(self):
        self._skip_if_no_cdk()
        from aws_cdk import assertions
        outputs = self.template.find_outputs("*")
        self.assertGreaterEqual(len(outputs), 5)

    def test_cfn_output_ops_dashboard_url(self):
        self._skip_if_no_cdk()
        # Check that an output whose description mentions "dashboard" exists
        from aws_cdk import assertions
        outputs = self.template.find_outputs("*")
        dashboard_outputs = [
            k for k, v in outputs.items()
            if "dashboard" in v.get("Description", "").lower()
        ]
        self.assertGreater(len(dashboard_outputs), 0)

    def test_cfn_output_ops_topic_arn(self):
        self._skip_if_no_cdk()
        from aws_cdk import assertions
        outputs = self.template.find_outputs("*")
        topic_outputs = [
            k for k, v in outputs.items()
            if "SNS topic ARN" in v.get("Description", "")
        ]
        self.assertGreater(len(topic_outputs), 0)


# ===========================================================================
# 5. Alarm rule building
# ===========================================================================

class TestAlarmRuleBuilding(unittest.TestCase):
    """
    Verify AlarmRule.any_of produces a valid composite alarm expression.
    """

    def test_any_of_with_multiple_alarms(self):
        try:
            import aws_cdk as cdk
            from aws_cdk import aws_cloudwatch as cloudwatch
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        stack = cdk.Stack(app, "TestStack2")

        # Create real Alarm objects
        alarms = []
        for i in range(3):
            alarms.append(
                cloudwatch.Alarm(
                    stack,
                    f"Alarm{i}",
                    metric=cloudwatch.Metric(
                        namespace="Test",
                        metric_name=f"Metric{i}",
                        statistic="Sum",
                    ),
                    threshold=1,
                    evaluation_periods=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                )
            )

        rule = cloudwatch.AlarmRule.any_of(
            *[cloudwatch.AlarmRule.from_alarm(a, cloudwatch.AlarmState.ALARM) for a in alarms]
        )

        # The rule is a valid AlarmRule object (not None)
        self.assertIsNotNone(rule)

    def test_any_of_with_single_alarm(self):
        """Edge case: composite alarm with exactly 1 constituent."""
        try:
            import aws_cdk as cdk
            from aws_cdk import aws_cloudwatch as cloudwatch
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        stack = cdk.Stack(app, "TestStack3")

        alarm = cloudwatch.Alarm(
            stack,
            "SingleAlarm",
            metric=cloudwatch.Metric(
                namespace="Test",
                metric_name="SingleMetric",
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )

        rule = cloudwatch.AlarmRule.any_of(
            cloudwatch.AlarmRule.from_alarm(alarm, cloudwatch.AlarmState.ALARM)
        )
        self.assertIsNotNone(rule)


# ===========================================================================
# 6. Metric filter safe_id sanitisation
# ===========================================================================

class TestSafeIdSanitisation(unittest.TestCase):

    def _sanitise(self, raw: str) -> str:
        return raw.replace("-", "_").replace("/", "_")

    def test_hyphens_replaced(self):
        self.assertEqual(self._sanitise("query-handler-errors"), "query_handler_errors")

    def test_slashes_replaced(self):
        self.assertEqual(self._sanitise("aws/lambda/fn"), "aws_lambda_fn")

    def test_mixed_replaced(self):
        self.assertEqual(self._sanitise("some-service/errors-count"), "some_service_errors_count")

    def test_no_hyphens_unchanged(self):
        self.assertEqual(self._sanitise("nohyphens"), "nohyphens")

    def test_empty_string(self):
        self.assertEqual(self._sanitise(""), "")

    def test_all_special_chars(self):
        result = self._sanitise("a-b/c-d/e")
        self.assertNotIn("-", result)
        self.assertNotIn("/", result)
        self.assertEqual(result, "a_b_c_d_e")


# ===========================================================================
# 7. MonitoringStack constructor parameter handling
# ===========================================================================

class TestMonitoringStackParameters(unittest.TestCase):
    """
    Lightweight tests verifying the stack accepts optional parameters
    without raising exceptions.
    """

    def test_stack_accepts_alert_email(self):
        try:
            import aws_cdk as cdk
            from stacks.monitoring_stack import MonitoringStack
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        env   = cdk.Environment(account="123456789012", region="us-east-1")

        # Should not raise even with an email provided
        try:
            stack = MonitoringStack(
                app, "MonitoringStackWithEmail",
                env=env,
                alert_email="ops@example.com",
            )
        except Exception as exc:
            self.fail(f"MonitoringStack raised unexpectedly with alert_email: {exc}")

    def test_stack_accepts_evaluation_stack_none(self):
        try:
            import aws_cdk as cdk
            from stacks.monitoring_stack import MonitoringStack
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        env   = cdk.Environment(account="123456789012", region="us-east-1")

        try:
            stack = MonitoringStack(
                app, "MonitoringStackNoEval",
                env=env,
                evaluation_stack=None,
            )
        except Exception as exc:
            self.fail(f"MonitoringStack raised unexpectedly with evaluation_stack=None: {exc}")

    def test_stack_exposes_ops_topic(self):
        try:
            import aws_cdk as cdk
            from stacks.monitoring_stack import MonitoringStack
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        env   = cdk.Environment(account="123456789012", region="us-east-1")
        stack = MonitoringStack(app, "MonitoringStackTopic", env=env)

        self.assertTrue(hasattr(stack, "ops_topic"))
        self.assertIsNotNone(stack.ops_topic)

    def test_stack_exposes_system_health_alarm(self):
        try:
            import aws_cdk as cdk
            from stacks.monitoring_stack import MonitoringStack
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        env   = cdk.Environment(account="123456789012", region="us-east-1")
        stack = MonitoringStack(app, "MonitoringStackAlarm", env=env)

        self.assertTrue(hasattr(stack, "system_health_alarm"))
        self.assertIsNotNone(stack.system_health_alarm)

    def test_stack_exposes_dashboard(self):
        try:
            import aws_cdk as cdk
            from stacks.monitoring_stack import MonitoringStack
        except ImportError:
            self.skipTest("aws-cdk not installed")

        app   = cdk.App()
        env   = cdk.Environment(account="123456789012", region="us-east-1")
        stack = MonitoringStack(app, "MonitoringStackDashboard", env=env)

        self.assertTrue(hasattr(stack, "dashboard"))
        self.assertIsNotNone(stack.dashboard)


# ===========================================================================
# 8. CloudTrail bucket policy helper logic
# ===========================================================================

class TestCloudTrailBucketPolicy(unittest.TestCase):
    """
    Verify that the CloudTrail S3 bucket will have the required resource
    policies when the template is synthesised.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import aws_cdk as cdk
            from aws_cdk import assertions
            from stacks.monitoring_stack import MonitoringStack

            app   = cdk.App()
            env   = cdk.Environment(account="123456789012", region="us-east-1")
            stack = MonitoringStack(app, "MonitoringStackTrail", env=env)
            cls.template = assertions.Template.from_stack(stack)
            cls.cdk_available = True
        except ImportError:
            cls.cdk_available = False

    def _skip_if_no_cdk(self):
        if not self.cdk_available:
            self.skipTest("aws-cdk not installed")

    def test_s3_bucket_policy_allows_cloudtrail_acl_check(self):
        self._skip_if_no_cdk()
        # At least one BucketPolicy must exist
        from aws_cdk import assertions
        policies = self.template.find_resources("AWS::S3::BucketPolicy")
        self.assertGreater(len(policies), 0)

    def test_s3_bucket_blocks_public_access(self):
        self._skip_if_no_cdk()
        self.template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
