"""
frontend_stack.py — Stage 8: Frontend Layer
============================================

FrontendStack provisions every AWS resource that serves the Research Domain
Enquirer React SPA to end-users via CloudFront.

Resources created
-----------------
1. S3 Bucket (``research-frontend``)
   - Private, block-all-public-access, versioning enabled
   - AWS-managed KMS encryption (SSE-S3 / aws:kms managed key)
   - CORS: allow GET from any origin
   - Access logs forwarded to ``research-logs`` (imported existing bucket)
   - RemovalPolicy.RETAIN — never auto-deleted

2. CloudFront Origin Access Control (OAC)
   - New-style OAC (not legacy OAI) for S3 origin
   - Uses ``aws_cloudfront.CfnOriginAccessControl``

3. CloudFront Distribution
   - Origin 1  : S3 frontend bucket (OAC-signed)
   - Origin 2  : API Gateway REST  (`/api/*`)
   - Origin 3  : API Gateway WebSocket (`/ws`)
   - Default root object : ``index.html``
   - SPA error routing  : 403 → index.html(200), 404 → index.html(200)
   - HTTP/2 + HTTP/3 enabled, HTTPS-only (TLS 1.2_2021), IPv6 enabled
   - Brotli + Gzip compression
   - Price class: PRICE_CLASS_100 (US + Europe edges)
   - Security response headers (HSTS, X-Content-Type, X-Frame, Referrer)
   - Cache behaviors per path:
       /api/*    → API GW origin,     TTL=0   (ALL methods)
       /ws       → WebSocket origin,  TTL=0
       /assets/* → S3 origin,         TTL=31536000 (1 year, immutable)
       /*.js     → S3 origin,         TTL=86400
       /*.css    → S3 origin,         TTL=86400
       /*        → S3 origin,         TTL=3600  (default)

4. WAF WebACL (``scope=CLOUDFRONT``, **must deploy in us-east-1**)
   - Rate-based rule: 1 000 req / 5 min per IP
   - AWS Managed: AWSManagedRulesCommonRuleSet
   - AWS Managed: AWSManagedRulesKnownBadInputsRuleSet
   - Associated with the CloudFront distribution

5. S3 Bucket Policy — CloudFront OAC read access

6. BucketDeployment (conditional)
   - Deploys ``frontend/dist/`` if the directory exists
   - Assets: cache-control = public, max-age=31536000, immutable
   - index.html: cache-control = public, max-age=3600
   - prune=True (removes stale files on redeploy)

7. SSM Parameters
   - /research/config/api-url  = REST API base URL
   - /research/config/ws-url   = WebSocket API URL

8. CloudFormation Outputs
   - CloudFrontUrl, CloudFrontDistributionId, FrontendBucketName, WebAclArn

Dependency
----------
  ApiStack must be deployed first.  In app.py call:
    ``frontend.add_dependency(api)``

WAF Note
--------
  CloudFront WAF WebACLs MUST reside in **us-east-1**.  Ensure the CDK app
  environment (``env``) targets us-east-1 when deploying FrontendStack, or
  use a custom resource / nested stack to create the WAF in us-east-1 and
  reference its ARN.  This stack assumes the whole stack is deployed in
  us-east-1 (which matches DEFAULT_REGION in config.py).

-------------------------------------------------------------------------------
deploy_script.sh (copy-paste reference — not executed by CDK)
-------------------------------------------------------------------------------
#!/usr/bin/env bash
# Build the React SPA and deploy to S3/CloudFront.
#
# Prerequisites:
#   node >= 18, npm, AWS CLI v2, jq
#   export AWS_PROFILE=<your-profile>
#   export AWS_REGION=us-east-1
#
# Step 1: Install frontend dependencies
#   cd frontend && npm ci
#
# Step 2: Fetch runtime config from SSM
#   API_URL=$(aws ssm get-parameter --name /research/config/api-url \\
#               --query Parameter.Value --output text)
#   WS_URL=$(aws ssm get-parameter  --name /research/config/ws-url  \\
#               --query Parameter.Value --output text)
#
# Step 3: Build with Vite
#   VITE_API_URL="$API_URL" VITE_WS_URL="$WS_URL" npm run build
#
# Step 4: Sync assets (long-lived cache)
#   BUCKET=$(aws cloudformation describe-stacks \\
#     --stack-name FrontendStack \\
#     --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \\
#     --output text)
#   aws s3 sync dist/ s3://$BUCKET/ \\
#     --exclude "index.html" \\
#     --cache-control "public, max-age=31536000, immutable" \\
#     --delete
#
# Step 5: Upload index.html with short TTL
#   aws s3 cp dist/index.html s3://$BUCKET/index.html \\
#     --cache-control "public, max-age=3600"
#
# Step 6: Invalidate CloudFront cache for HTML entry point
#   DIST_ID=$(aws cloudformation describe-stacks \\
#     --stack-name FrontendStack \\
#     --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \\
#     --output text)
#   aws cloudfront create-invalidation --distribution-id $DIST_ID \\
#     --paths "/index.html" "/"
#
# Done!
-------------------------------------------------------------------------------
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
    aws_wafv2 as wafv2,
)
from constructs import Construct

from .config import (
    API_STAGE_NAME,
    PROJECT_PREFIX,
    S3_FRONTEND,
    S3_LOGS,
    WAF_RATE_LIMIT_PER_5MIN,
)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Relative path from this file to the compiled frontend artefacts
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_FRONTEND_DIST = os.path.join(_REPO_ROOT, "frontend", "dist")

# CloudFront path patterns (ordered by specificity — longer first)
_PATH_API   = "/api/*"
_PATH_WS    = "/ws"
_PATH_ASSET = "/assets/*"
_PATH_JS    = "/*.js"
_PATH_CSS   = "/*.css"


class FrontendStack(Stack):
    """
    CDK Stack — Frontend layer: React SPA + CloudFront CDN + WAF protection.

    Constructs created
    ------------------
    aws_s3.Bucket                        — research-frontend (private SPA host)
    aws_cloudfront.CfnOriginAccessControl — OAC for S3 origin
    aws_cloudfront.Distribution           — CDN with multi-origin routing
    aws_wafv2.CfnWebACL                   — WAF (rate-limit + managed rules)
    aws_iam.PolicyDocument                — S3 bucket policy for OAC access
    aws_s3_deployment.BucketDeployment    — optional — only if dist/ exists
    aws_ssm.StringParameter x 2           — /research/config/{api,ws}-url

    Public attributes
    -----------------
    self.frontend_bucket  : s3.Bucket
    self.distribution     : cloudfront.Distribution
    self.web_acl          : wafv2.CfnWebACL

    WAF must be deployed in us-east-1 (CloudFront requirement).
    This stack assumes env.region == 'us-east-1'.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_stack,  # type: ApiStack  (avoids circular import at type-check time)
        *,
        env: cdk.Environment,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        # Tag the stack for X-Ray tracing visibility
        cdk.Tags.of(self).add("XRayTracing", "active")
        cdk.Tags.of(self).add("Project", PROJECT_PREFIX)
        cdk.Tags.of(self).add("Stack", "FrontendStack")

        # ------------------------------------------------------------------
        # Derive API origin hostnames from the ApiStack outputs
        # ------------------------------------------------------------------
        # REST API:  https://{id}.execute-api.{region}.amazonaws.com/{stage}
        # We need just the domain for the CloudFront origin.
        rest_api_id     = api_stack.rest_api.rest_api_id
        rest_api_domain = f"{rest_api_id}.execute-api.{self.region}.amazonaws.com"
        rest_api_origin_path = f"/{API_STAGE_NAME}"   # strip stage from default route

        # WebSocket API:  wss://{id}.execute-api.{region}.amazonaws.com/{stage}
        ws_api_id     = api_stack.websocket_api.api_id
        ws_api_domain = f"{ws_api_id}.execute-api.{self.region}.amazonaws.com"
        ws_api_origin_path = f"/{API_STAGE_NAME}"

        # ------------------------------------------------------------------
        # 1. S3 — Import existing logs bucket (storage stack creates it)
        # ------------------------------------------------------------------
        logs_bucket = s3.Bucket.from_bucket_name(
            self,
            "ImportedLogsBucket",
            bucket_name=S3_LOGS,
        )

        # ------------------------------------------------------------------
        # 2. S3 — Frontend SPA bucket (private)
        # ------------------------------------------------------------------
        self.frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            bucket_name=S3_FRONTEND,
            # Security
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            public_read_access=False,
            # Encryption — AWS-managed (SSE-S3)
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Versioning
            versioning=True,
            # CORS — allow GET from any origin (CloudFront signs the request)
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
            # Access logging
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="s3-frontend/",
            # Lifecycle
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )

        # ------------------------------------------------------------------
        # 3. CloudFront OAC — Origin Access Control (new-style, not OAI)
        # ------------------------------------------------------------------
        cfn_oac = cloudfront.CfnOriginAccessControl(
            self,
            "FrontendOAC",
            origin_access_control_config=cloudfront.CfnOriginAccessControl.OriginAccessControlConfigProperty(
                name=f"{PROJECT_PREFIX}-frontend-oac",
                description=(
                    "OAC for Research Domain Enquirer frontend S3 bucket — "
                    "signs requests with SigV4 so the bucket stays private"
                ),
                origin_access_control_origin_type="s3",
                signing_behavior="always",
                signing_protocol="sigv4",
            ),
        )

        # ------------------------------------------------------------------
        # 4. WAF WebACL (scope=CLOUDFRONT — must be in us-east-1)
        # ------------------------------------------------------------------
        # CloudFront WAF WebACLs must reside in us-east-1.
        # Ensure env.region == 'us-east-1' when deploying FrontendStack.
        self.web_acl = wafv2.CfnWebACL(
            self,
            "FrontendWebACL",
            name=f"{PROJECT_PREFIX}-frontend-waf",
            scope="CLOUDFRONT",          # CLOUDFRONT scope requires us-east-1
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{PROJECT_PREFIX}-frontend-waf",
                sampled_requests_enabled=True,
            ),
            description=(
                "WAF WebACL for Research Domain Enquirer CloudFront distribution. "
                "Rate-limits, blocks common attack patterns and known bad inputs."
            ),
            rules=[
                # Rule 1 — Rate-based: 1 000 requests / 5 min per IP
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIP",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=WAF_RATE_LIMIT_PER_5MIN,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{PROJECT_PREFIX}-rate-limit",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 2 — AWS Managed: Common Rule Set (OWASP top 10)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{PROJECT_PREFIX}-common-rules",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 3 — AWS Managed: Known Bad Inputs Rule Set
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                    priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesKnownBadInputsRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{PROJECT_PREFIX}-bad-inputs",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )

        # ------------------------------------------------------------------
        # 5. Security Response Headers Policy
        # ------------------------------------------------------------------
        response_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            response_headers_policy_name=f"{PROJECT_PREFIX}-security-headers",
            comment="Security headers for Research Domain Enquirer SPA",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.seconds(31536000),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )

        # ------------------------------------------------------------------
        # 6. Cache policies
        # ------------------------------------------------------------------

        # No-cache policy for API / WebSocket paths (TTL = 0)
        no_cache_policy = cloudfront.CachePolicy(
            self,
            "NoCachePolicy",
            cache_policy_name=f"{PROJECT_PREFIX}-no-cache",
            comment="Zero TTL — passthrough for API and WebSocket origins",
            default_ttl=Duration.seconds(0),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(0),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list(
                "Authorization",
                "Content-Type",
                "Origin",
                "Referer",
            ),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            enable_accept_encoding_brotli=False,
            enable_accept_encoding_gzip=False,
        )

        # Long-lived immutable assets (fonts, hashed bundles, images)
        assets_cache_policy = cloudfront.CachePolicy(
            self,
            "AssetsCachePolicy",
            cache_policy_name=f"{PROJECT_PREFIX}-assets-cache",
            comment="1-year immutable cache for hashed assets",
            default_ttl=Duration.seconds(31536000),
            min_ttl=Duration.seconds(31536000),
            max_ttl=Duration.seconds(31536000),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
        )

        # JS / CSS bundles (1 day)
        js_css_cache_policy = cloudfront.CachePolicy(
            self,
            "JsCssCachePolicy",
            cache_policy_name=f"{PROJECT_PREFIX}-js-css-cache",
            comment="1-day cache for JS and CSS bundles",
            default_ttl=Duration.seconds(86400),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(86400),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
        )

        # Default SPA (index.html + HTML routes — 1 hour)
        default_cache_policy = cloudfront.CachePolicy(
            self,
            "DefaultCachePolicy",
            cache_policy_name=f"{PROJECT_PREFIX}-default-cache",
            comment="1-hour cache for SPA HTML entry points",
            default_ttl=Duration.seconds(3600),
            min_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(3600),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
        )

        # ------------------------------------------------------------------
        # 7. CloudFront Distribution
        # ------------------------------------------------------------------

        # Origin 1 — S3 (private bucket via OAC)
        # S3BucketOrigin.with_origin_access_control creates the OAC-based
        # bucket policy grant automatically.
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.frontend_bucket,
            origin_access_control=cloudfront.S3OriginAccessControl(
                self,
                "S3OAC",
                signing=cloudfront.Signing.SIGV4_NO_OVERRIDE,
            ),
        )

        # Origin 2 — API Gateway REST (strip stage path)
        api_origin = origins.HttpOrigin(
            rest_api_domain,
            origin_path=rest_api_origin_path,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            https_port=443,
            origin_id=f"{PROJECT_PREFIX}-api-origin",
            custom_headers={},
        )

        # Origin 3 — API Gateway WebSocket
        ws_origin = origins.HttpOrigin(
            ws_api_domain,
            origin_path=ws_api_origin_path,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            https_port=443,
            origin_id=f"{PROJECT_PREFIX}-ws-origin",
            custom_headers={},
        )

        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            comment=f"{PROJECT_PREFIX} — React SPA via CloudFront",
            # Default behaviour — S3 origin for SPA
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=default_cache_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                compress=True,
                response_headers_policy=response_headers_policy,
                origin_request_policy=cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
            ),
            additional_behaviors={
                # /api/* — API Gateway REST (no-cache, ALL methods)
                _PATH_API: cloudfront.BehaviorOptions(
                    origin=api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=no_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                    compress=True,
                    response_headers_policy=response_headers_policy,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
                # /ws — WebSocket (no-cache)
                _PATH_WS: cloudfront.BehaviorOptions(
                    origin=ws_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=no_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                    compress=False,  # don't compress WebSocket frames
                    response_headers_policy=response_headers_policy,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
                # /assets/* — S3, 1-year immutable
                _PATH_ASSET: cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=assets_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                    compress=True,
                    response_headers_policy=response_headers_policy,
                    origin_request_policy=cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
                ),
                # /*.js — S3, 1 day
                _PATH_JS: cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=js_css_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                    compress=True,
                    response_headers_policy=response_headers_policy,
                    origin_request_policy=cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
                ),
                # /*.css — S3, 1 day
                _PATH_CSS: cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=js_css_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                    cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                    compress=True,
                    response_headers_policy=response_headers_policy,
                    origin_request_policy=cloudfront.OriginRequestPolicy.CORS_S3_ORIGIN,
                ),
            },
            # SPA routing — 403/404 → index.html so React Router works
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_page_path="/index.html",
                    response_http_status=200,
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_page_path="/index.html",
                    response_http_status=200,
                    ttl=Duration.seconds(0),
                ),
            ],
            default_root_object="index.html",
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            ssl_support_method=cloudfront.SSLMethod.SNI,
            # WAF association — web_acl_id must be in us-east-1
            web_acl_id=self.web_acl.attr_arn,
            # CloudFront access logging
            enable_logging=True,
            log_bucket=logs_bucket,
            log_file_prefix="cloudfront/",
            log_includes_cookies=False,
        )

        # cfn_oac is created for explicit naming / future reference.
        # S3BucketOrigin.with_origin_access_control manages its own OAC;
        # we keep cfn_oac as a named resource so it appears in the template.
        _ = cfn_oac  # noqa: F841

        # ------------------------------------------------------------------
        # 8. S3 Bucket Policy — explicit deny-non-SSL for compliance
        #    (OAC read-access grant is added automatically by S3BucketOrigin)
        # ------------------------------------------------------------------
        self.frontend_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonSSLRequests",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.frontend_bucket.bucket_arn,
                    f"{self.frontend_bucket.bucket_arn}/*",
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        # ------------------------------------------------------------------
        # 9. BucketDeployment (optional — only if frontend/dist/ exists)
        # ------------------------------------------------------------------
        if os.path.isdir(_FRONTEND_DIST):
            # Deploy hashed assets (JS, CSS, fonts, images) with long TTL
            s3_deployment.BucketDeployment(
                self,
                "DeployAssets",
                sources=[
                    s3_deployment.Source.asset(
                        _FRONTEND_DIST,
                        exclude=["index.html"],
                    )
                ],
                destination_bucket=self.frontend_bucket,
                distribution=self.distribution,
                distribution_paths=["/*"],
                prune=True,
                cache_control=[
                    s3_deployment.CacheControl.from_string(
                        "public, max-age=31536000, immutable"
                    )
                ],
                memory_limit=512,
            )

            # Upload index.html separately with a short TTL (no immutable)
            index_html_path = os.path.join(_FRONTEND_DIST, "index.html")
            if os.path.isfile(index_html_path):
                s3_deployment.BucketDeployment(
                    self,
                    "DeployIndexHtml",
                    sources=[
                        s3_deployment.Source.asset(
                            _FRONTEND_DIST,
                            exclude=["*", "!index.html"],
                        )
                    ],
                    destination_bucket=self.frontend_bucket,
                    distribution=self.distribution,
                    distribution_paths=["/", "/index.html"],
                    prune=False,
                    cache_control=[
                        s3_deployment.CacheControl.from_string(
                            "public, max-age=3600"
                        )
                    ],
                    memory_limit=128,
                )

        # ------------------------------------------------------------------
        # 10. SSM Parameters — frontend runtime config
        # ------------------------------------------------------------------
        ssm.StringParameter(
            self,
            "ApiUrlParam",
            parameter_name=f"/{PROJECT_PREFIX}/config/api-url",
            string_value=api_stack.rest_api.url,
            description="Research REST API base URL (consumed by React app at runtime)",
            tier=ssm.ParameterTier.STANDARD,
        )

        ssm.StringParameter(
            self,
            "WsUrlParam",
            parameter_name=f"/{PROJECT_PREFIX}/config/ws-url",
            string_value=api_stack.websocket_stage.url,
            description="Research WebSocket API URL (consumed by React app at runtime)",
            tier=ssm.ParameterTier.STANDARD,
        )

        # ------------------------------------------------------------------
        # 11. CloudFormation Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "CloudFrontUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="CloudFront distribution URL for the Research Domain Enquirer SPA",
            export_name=f"{PROJECT_PREFIX}-cloudfront-url",
        )

        cdk.CfnOutput(
            self,
            "CloudFrontDistributionId",
            value=self.distribution.distribution_id,
            description="CloudFront distribution ID — use for cache invalidation",
            export_name=f"{PROJECT_PREFIX}-cloudfront-distribution-id",
        )

        cdk.CfnOutput(
            self,
            "FrontendBucketName",
            value=self.frontend_bucket.bucket_name,
            description="S3 bucket name hosting the React SPA artefacts",
            export_name=f"{PROJECT_PREFIX}-frontend-bucket-name",
        )

        cdk.CfnOutput(
            self,
            "WebAclArn",
            value=self.web_acl.attr_arn,
            description="WAF WebACL ARN attached to the CloudFront distribution",
            export_name=f"{PROJECT_PREFIX}-web-acl-arn",
        )
