#!/bin/bash
# deploy.sh — Package and deploy the Lambda function
# Usage: ./deploy.sh [function-name] [region]
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Python 3.12 available
#   - Lambda execution role created (see README)

set -e

FUNCTION_NAME="${1:-robinhood-trading-agent}"
REGION="${2:-us-east-1}"
RUNTIME="python3.12"
HANDLER="handler.lambda_handler"
TIMEOUT=900        # 15 min — agent loops + rate limit retries need the headroom
MEMORY=512         # MB

LAMBDA_DIR="."
BUILD_DIR="/tmp/robinhood-lambda-build"
ZIP_FILE="/tmp/robinhood-lambda.zip"

echo "🏗  Building Lambda package..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# Install dependencies into build dir
pip install -r "$LAMBDA_DIR/requirements.txt" \
    --target "$BUILD_DIR" \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --quiet

# Copy Lambda handler
cp "$LAMBDA_DIR/handler.py" "$BUILD_DIR/"

# Zip it up
echo "📦 Zipping..."
cd "$BUILD_DIR"
zip -r "$ZIP_FILE" . -q
cd -

echo "✅ Package size: $(du -sh $ZIP_FILE | cut -f1)"

# Check if function exists
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" &>/dev/null; then
    echo "🔄 Updating existing Lambda function..."
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION" \
        --output text --query 'FunctionArn'
else
    echo "🆕 Creating new Lambda function..."
    echo "⚠ You need an IAM role ARN. Creating with placeholder — update before first run."

    # You'll need to create this role first — see README
    ROLE_ARN="${LAMBDA_ROLE_ARN:-arn:aws:iam::YOUR_ACCOUNT:role/robinhood-lambda-role}"

    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime "$RUNTIME" \
        --role "$ROLE_ARN" \
        --handler "$HANDLER" \
        --zip-file "fileb://$ZIP_FILE" \
        --timeout "$TIMEOUT" \
        --memory-size "$MEMORY" \
        --region "$REGION" \
        --output text --query 'FunctionArn'
fi

echo ""
echo "⚙️  Setting environment variables..."
echo "   (Make sure these are set before running — edit this section)"

# Uncomment and fill in to set env vars during deploy:
# aws lambda update-function-configuration \
#     --function-name "$FUNCTION_NAME" \
#     --region "$REGION" \
#     --environment "Variables={
#         ANTHROPIC_API_KEY=sk-ant-...,
#         RH_AUTH_TOKEN=your_robinhood_oauth_token,
#         RH_ACCOUNT_NUMBER=XXXXXXXXXX,
#         DEFAULT_PROMPT=Buy $50 of SPY every day the market is open
#     }" \
#     --output text --query 'FunctionName'

echo ""
echo "🎉 Deploy complete! Function: $FUNCTION_NAME"
echo ""
echo "Test it manually:"
echo "  aws lambda invoke \\"
echo "    --function-name $FUNCTION_NAME \\"
echo "    --region $REGION \\"
echo "    --payload '{\"prompt\": \"Buy \$25 of AAPL today.\"}' \\"
echo "    --cli-binary-format raw-in-base64-out \\"
echo "    output.json && cat output.json"
