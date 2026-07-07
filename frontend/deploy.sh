#!/usr/bin/env bash
# =============================================================
# deploy.sh — Build & Deploy React SPA to S3 + CloudFront
# =============================================================
#
# Prerequisites:
#   node >= 18, npm, AWS CLI v2
#   export AWS_PROFILE=<your-profile>  (or configure default creds)
#   export AWS_REGION=us-east-1
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="FrontendStack"

echo "=== Research Domain Enquirer — Frontend Deploy ==="

# ---- 1. Fetch runtime config from SSM -----------------------
echo ""
echo "[1/6] Fetching runtime config from SSM..."

API_URL=$(aws ssm get-parameter \
  --name "/research/config/api-url" \
  --region "$REGION" \
  --query "Parameter.Value" \
  --output text 2>/dev/null) || { echo "Warning: SSM fetch failed. Using defaults."; API_URL="/api"; }

WS_URL=$(aws ssm get-parameter \
  --name "/research/config/ws-url" \
  --region "$REGION" \
  --query "Parameter.Value" \
  --output text 2>/dev/null) || WS_URL="ws://localhost:8000/ws"

echo "  API URL : $API_URL"
echo "  WS  URL : $WS_URL"

# ---- 2. Install dependencies --------------------------------
echo ""
echo "[2/6] Installing npm dependencies (ci)..."
npm ci --prefer-offline

# ---- 3. Build with Vite -------------------------------------
echo ""
echo "[3/6] Building React SPA (Vite)..."
VITE_API_BASE_URL="$API_URL" \
VITE_WS_URL="$WS_URL" \
VITE_USE_MOCK_API="false" \
npm run build

echo "  Build complete — output in ./dist/"

# ---- 4. Resolve S3 bucket + CloudFront distribution --------
echo ""
echo "[4/6] Resolving FrontendStack outputs..."

BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text)

DIST_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
  --output text)

echo "  Bucket  : $BUCKET"
echo "  Dist ID : $DIST_ID"

[ -z "$BUCKET" ] && { echo "Error: Could not determine S3 bucket."; exit 1; }

# ---- 5. Sync hashed assets (long TTL) ----------------------
echo ""
echo "[5/6] Uploading hashed assets (1-year cache)..."
aws s3 sync dist/ "s3://$BUCKET/" \
  --exclude "index.html" \
  --cache-control "public, max-age=31536000, immutable" \
  --delete \
  --region "$REGION"

echo "  Uploading index.html (1-hour cache)..."
aws s3 cp dist/index.html "s3://$BUCKET/index.html" \
  --cache-control "public, max-age=3600" \
  --region "$REGION"

# ---- 6. Invalidate CloudFront cache -------------------------
echo ""
if [ -n "$DIST_ID" ]; then
  echo "[6/6] Invalidating CloudFront cache..."
  aws cloudfront create-invalidation \
    --distribution-id "$DIST_ID" \
    --paths "/" "/index.html" \
    --region "$REGION" > /dev/null
  echo "  Cache invalidated for / and /index.html"
else
  echo "[6/6] No CloudFront distribution ID — skipping cache invalidation."
fi

echo ""
echo "=== Deploy complete! ==="
CF_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" \
  --output text 2>/dev/null) || true
[ -n "$CF_URL" ] && echo "CloudFront URL: $CF_URL"
