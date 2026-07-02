# generation_stack.py — Day 5: Generation & Hallucination Detection
"""
GenerationStack — AWS CDK stack for the generation pipeline.

Responsibilities:
  - Lambda: Answer Generator      (receives context prompt → Bedrock Claude 3.5 Sonnet → calls Hallucination Detector)
  - Lambda: Hallucination Detector (5-step claim verification via Claude 3 Haiku)
  - IAM grants: Bedrock InvokeModel (Claude 3.5 Sonnet + Claude 3 Haiku), CloudWatch
  - CloudWatch Log Groups (30-day retention per Lambda)
  - Passes Hallucination Detector ARN as env var to Answer Generator

Dependency order:
  StorageStack → IngestionStack → EmbeddingStack → GraphStack → RetrievalStack → GenerationStack

Deploy:
  cdk deploy GenerationStack

This file defines CDK constructs only — no AWS API calls are made here.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

from .config import (
    BEDROCK_GENERATION_MODEL,
    BEDROCK_VERIFY_MODEL,
    CONFIDENCE_PASS_THRESHOLD,
    CONFIDENCE_WARN_THRESHOLD,
    CONFIDENCE_REFUSE_THRESHOLD,
    CW_NAMESPACE,
    LAMBDA_CONFIGS,
    PROJECT_PREFIX,
)

# ---------------------------------------------------------------------------
# Paths from CDK app root to each Lambda source directory
# ---------------------------------------------------------------------------
_LAMBDA_BASE              = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
_ANSWER_GENERATOR_DIR     = os.path.join(_LAMBDA_BASE, "answer_generator")
_HALLUCINATION_DETECT_DIR = os.path.join(_LAMBDA_BASE, "hallucination_detector")


def _pip_bundling(runtime: lambda_.Runtime) -> cdk.BundlingOptions:
    """Standard pip-install bundling options for Python Lambda functions."""
    return cdk.BundlingOptions(
        image=runtime.bundling_image,
        command=[
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
        ],
    )


class GenerationStack(Stack):
    """
    CDK Stack — Generation & Hallucination Detection pipeline.

    Constructs created:
      - aws_lambda.Function: answer_generator
      - aws_lambda.Function: hallucination_detector
      - aws_iam.Role × 2  (one per Lambda)
      - aws_logs.LogGroup × 2
      - Bedrock InvokeModel IAM statements
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        retrieval_stack=None,    # type: ignore[type-arg]  # passed for dependency wiring
        env: cdk.Environment,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        runtime = lambda_.Runtime.PYTHON_3_11

        # ------------------------------------------------------------------
        # IAM — shared Bedrock policy statements
        # ------------------------------------------------------------------

        # Answer Generator needs Claude 3.5 Sonnet
        bedrock_generation_policy = iam.PolicyStatement(
            sid="BedrockConverseGeneration",
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/{BEDROCK_GENERATION_MODEL}",
            ],
        )

        # Hallucination Detector needs Claude 3 Haiku
        bedrock_verify_policy = iam.PolicyStatement(
            sid="BedrockConverseVerify",
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}::foundation-model/{BEDROCK_VERIFY_MODEL}",
            ],
        )

        # Both Lambdas write CloudWatch metrics
        cloudwatch_policy = iam.PolicyStatement(
            sid="CloudWatchPutMetricData",
            effect=iam.Effect.ALLOW,
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
        )

        # ------------------------------------------------------------------
        # Lambda: Hallucination Detector  (built FIRST — Answer Generator needs its ARN)
        # ------------------------------------------------------------------
        hd_cfg = LAMBDA_CONFIGS["hallucination_detector"]

        hd_role = iam.Role(
            self,
            "HallucinationDetectorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=(
                f"{PROJECT_PREFIX} — Hallucination Detector Lambda execution role"
            ),
        )
        hd_role.add_to_policy(bedrock_verify_policy)
        hd_role.add_to_policy(cloudwatch_policy)

        hd_log_group = logs.LogGroup(
            self,
            "HallucinationDetectorLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-hallucination-detector",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.hallucination_detector = lambda_.Function(
            self,
            "HallucinationDetector",
            function_name=f"{PROJECT_PREFIX}-hallucination-detector",
            description=(
                "5-step hallucination detection: claim extraction → evidence "
                "mapping → claim verification → coverage analysis → confidence score"
            ),
            runtime=runtime,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                _HALLUCINATION_DETECT_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=hd_role,
            memory_size=hd_cfg["memory_mb"],
            timeout=Duration.seconds(hd_cfg["timeout_seconds"]),
            environment={
                "BEDROCK_VERIFY_MODEL":        BEDROCK_VERIFY_MODEL,
                "BEDROCK_REGION":              self.region,
                "CW_NAMESPACE":                CW_NAMESPACE,
                "CONFIDENCE_PASS_THRESHOLD":   str(CONFIDENCE_PASS_THRESHOLD),
                "CONFIDENCE_WARN_THRESHOLD":   str(CONFIDENCE_WARN_THRESHOLD),
                "CONFIDENCE_REFUSE_THRESHOLD": str(CONFIDENCE_REFUSE_THRESHOLD),
                "LOG_LEVEL":                   "INFO",
            },
            log_group=hd_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # Lambda: Answer Generator
        # ------------------------------------------------------------------
        ag_cfg = LAMBDA_CONFIGS["answer_generator"]

        ag_role = iam.Role(
            self,
            "AnswerGeneratorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"{PROJECT_PREFIX} — Answer Generator Lambda execution role",
        )
        ag_role.add_to_policy(bedrock_generation_policy)
        ag_role.add_to_policy(cloudwatch_policy)

        # Allow Answer Generator to invoke Hallucination Detector
        ag_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeHallucinationDetector",
                effect=iam.Effect.ALLOW,
                actions=["lambda:InvokeFunction"],
                resources=[self.hallucination_detector.function_arn],
            )
        )

        ag_log_group = logs.LogGroup(
            self,
            "AnswerGeneratorLogGroup",
            log_group_name=f"/aws/lambda/{PROJECT_PREFIX}-answer-generator",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.answer_generator = lambda_.Function(
            self,
            "AnswerGenerator",
            function_name=f"{PROJECT_PREFIX}-answer-generator",
            description=(
                "Calls Bedrock Claude 3.5 Sonnet to generate a cited research "
                "answer, then invokes Hallucination Detector for claim verification"
            ),
            runtime=runtime,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                _ANSWER_GENERATOR_DIR,
                bundling=_pip_bundling(runtime),
            ),
            role=ag_role,
            memory_size=ag_cfg["memory_mb"],
            timeout=Duration.seconds(ag_cfg["timeout_seconds"]),
            environment={
                "BEDROCK_GENERATION_MODEL":         BEDROCK_GENERATION_MODEL,
                "BEDROCK_REGION":                   self.region,
                "HALLUCINATION_DETECTOR_FUNCTION_NAME": (
                    self.hallucination_detector.function_name
                ),
                "CW_NAMESPACE":                     CW_NAMESPACE,
                "MAX_TOKENS":                       "2048",
                "TEMPERATURE":                      "0.1",
                "TOP_P":                            "0.9",
                "LOG_LEVEL":                        "INFO",
            },
            log_group=ag_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ------------------------------------------------------------------
        # CloudFormation Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "AnswerGeneratorFunctionName",
            value=self.answer_generator.function_name,
            description="Answer Generator Lambda function name",
            export_name=f"{PROJECT_PREFIX}-answer-generator-name",
        )
        cdk.CfnOutput(
            self,
            "AnswerGeneratorFunctionArn",
            value=self.answer_generator.function_arn,
            description="Answer Generator Lambda function ARN",
            export_name=f"{PROJECT_PREFIX}-answer-generator-arn",
        )
        cdk.CfnOutput(
            self,
            "HallucinationDetectorFunctionName",
            value=self.hallucination_detector.function_name,
            description="Hallucination Detector Lambda function name",
            export_name=f"{PROJECT_PREFIX}-hallucination-detector-name",
        )
        cdk.CfnOutput(
            self,
            "HallucinationDetectorFunctionArn",
            value=self.hallucination_detector.function_arn,
            description="Hallucination Detector Lambda function ARN",
            export_name=f"{PROJECT_PREFIX}-hallucination-detector-arn",
        )
