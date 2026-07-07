#!/usr/bin/env pwsh
# =============================================================
# deploy.ps1 — Build & Deploy React SPA to S3 + CloudFront
# =============================================================
#
# Prerequisites:
#   node >= 18, npm, AWS CLI v2
#   Set-AWSCredential or export AWS_PROFILE / AWS_ACCESS_KEY_ID
#   AWS_REGION environment variable (defaults to us-east-1)
#
# Usage:
#   .\deploy.ps1
#   .\deploy.ps1 -Profile my-aws-profile -Region us-east-1
#
param(
    [string]$Profile = $env:AWS_PROFILE,
    [string]$Region  = ($env:AWS_REGION ?? "us-east-1")
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Research Domain Enquirer — Frontend Deploy ===" -ForegroundColor Cyan

# ---- 1. Fetch runtime config from SSM -----------------------
Write-Host "`n[1/6] Fetching runtime config from SSM Parameter Store..." -ForegroundColor Yellow

$SsmArgs = @("ssm", "get-parameter", "--region", $Region)
if ($Profile) { $SsmArgs += "--profile", $Profile }

$ApiUrl = (& aws @SsmArgs --name "/research/config/api-url" --query "Parameter.Value" --output text 2>&1)
$WsUrl  = (& aws @SsmArgs --name "/research/config/ws-url"  --query "Parameter.Value" --output text 2>&1)

if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not fetch SSM params. Falling back to mock mode."
    $ApiUrl = "/api"
    $WsUrl  = "ws://localhost:8000/ws"
    $env:VITE_USE_MOCK_API = "true"
} else {
    Write-Host "  API URL : $ApiUrl" -ForegroundColor Green
    Write-Host "  WS  URL : $WsUrl"  -ForegroundColor Green
    $env:VITE_USE_MOCK_API = "false"
}

# ---- 2. Install dependencies --------------------------------
Write-Host "`n[2/6] Installing npm dependencies..." -ForegroundColor Yellow
npm ci --prefer-offline
if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }

# ---- 3. Build with Vite -------------------------------------
Write-Host "`n[3/6] Building React SPA (Vite)..." -ForegroundColor Yellow
$env:VITE_API_BASE_URL = $ApiUrl
$env:VITE_WS_URL       = $WsUrl
npm run build
if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
Write-Host "  Build complete — output in ./dist/" -ForegroundColor Green

# ---- 4. Resolve S3 bucket name from CloudFormation ----------
Write-Host "`n[4/6] Resolving FrontendStack outputs..." -ForegroundColor Yellow
$CfArgs = @("cloudformation", "describe-stacks", "--stack-name", "FrontendStack", "--region", $Region)
if ($Profile) { $CfArgs += "--profile", $Profile }

$Bucket  = (& aws @CfArgs --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" --output text 2>&1)
$DistId  = (& aws @CfArgs --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" --output text 2>&1)

if ($LASTEXITCODE -ne 0) {
    Write-Warning "FrontendStack not deployed yet — set BUCKET and DIST_ID manually."
    $Bucket = $env:FRONTEND_BUCKET
    $DistId = $env:CLOUDFRONT_DIST_ID
}
Write-Host "  Bucket  : $Bucket" -ForegroundColor Green
Write-Host "  Dist ID : $DistId" -ForegroundColor Green

if (-not $Bucket) { throw "Could not determine S3 bucket name." }

# ---- 5. Sync hashed assets (long TTL) ----------------------
Write-Host "`n[5/6] Uploading hashed assets (1-year cache)..." -ForegroundColor Yellow
$S3Args = @("s3", "sync", "dist/", "s3://$Bucket/",
    "--exclude", "index.html",
    "--cache-control", "public, max-age=31536000, immutable",
    "--delete",
    "--region", $Region)
if ($Profile) { $S3Args += "--profile", $Profile }
& aws @S3Args
if ($LASTEXITCODE -ne 0) { throw "Asset sync failed" }

# Upload index.html with short TTL (no immutable flag)
Write-Host "  Uploading index.html (1-hour cache)..." -ForegroundColor Yellow
$HtmlArgs = @("s3", "cp", "dist/index.html", "s3://$Bucket/index.html",
    "--cache-control", "public, max-age=3600",
    "--region", $Region)
if ($Profile) { $HtmlArgs += "--profile", $Profile }
& aws @HtmlArgs
if ($LASTEXITCODE -ne 0) { throw "index.html upload failed" }

# ---- 6. Invalidate CloudFront cache -------------------------
if ($DistId) {
    Write-Host "`n[6/6] Invalidating CloudFront cache..." -ForegroundColor Yellow
    $InvArgs = @("cloudfront", "create-invalidation",
        "--distribution-id", $DistId,
        "--paths", "/", "/index.html",
        "--region", $Region)
    if ($Profile) { $InvArgs += "--profile", $Profile }
    & aws @InvArgs | Out-Null
    Write-Host "  Cache invalidated for / and /index.html" -ForegroundColor Green
} else {
    Write-Warning "No CloudFront distribution ID — skipping cache invalidation."
}

Write-Host "`n=== Deploy complete! ===" -ForegroundColor Cyan
Write-Host "CloudFront URL: https://$(& aws cloudformation describe-stacks --stack-name FrontendStack --region $Region --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" --output text 2>$null)" -ForegroundColor Magenta
