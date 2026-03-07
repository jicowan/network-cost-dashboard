#!/usr/bin/env bash
#
# deploy.sh — Deploy the network-cost-exporter Lambda, supporting resources,
#              and EventBridge schedule.
#
# Usage:
#   ./deploy.sh                          # interactive prompts for required values
#   ./deploy.sh \
#     --region us-west-2 \
#     --monitor-name eks-network-costs \
#     --s3-bucket 123456789012-eks-network-costs
#
# The script is idempotent: it creates resources that don't exist and updates
# those that do.

set -euo pipefail

# -------------------------------------------------------------------
# Defaults (override with flags or environment variables)
# -------------------------------------------------------------------
REGION="${AWS_REGION:-}"
MONITOR_NAME="${MONITOR_NAME:-eks-network-costs}"
S3_BUCKET="${S3_BUCKET:-}"
S3_PREFIX="${S3_PREFIX:-network-cost-data}"
ATHENA_DATABASE="${ATHENA_DATABASE:-network_costs}"
HAS_S3_ENDPOINT="${HAS_S3_ENDPOINT:-false}"
HAS_DYNAMODB_ENDPOINT="${HAS_DYNAMODB_ENDPOINT:-false}"
FUNCTION_NAME="eks-network-cost-exporter"
ROLE_NAME="eks-network-cost-lambda-role"
SCHEDULE_NAME="eks-network-cost-hourly"
SCHEDULE_ROLE_NAME="eks-network-cost-scheduler-role"
RUNTIME="python3.12"
TIMEOUT=300
MEMORY=256

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -------------------------------------------------------------------
# Parse arguments
# -------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)           REGION="$2";        shift 2 ;;
        --monitor-name)     MONITOR_NAME="$2";  shift 2 ;;
        --s3-bucket)        S3_BUCKET="$2";     shift 2 ;;
        --s3-prefix)        S3_PREFIX="$2";     shift 2 ;;
        --athena-database)  ATHENA_DATABASE="$2"; shift 2 ;;
        --has-s3-endpoint)  HAS_S3_ENDPOINT="$2"; shift 2 ;;
        --has-dynamodb-endpoint) HAS_DYNAMODB_ENDPOINT="$2"; shift 2 ;;
        --function-name)    FUNCTION_NAME="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# -------------------------------------------------------------------
# Validate required inputs
# -------------------------------------------------------------------
if [[ -z "$REGION" ]]; then
    read -rp "AWS region: " REGION
fi
if [[ -z "$S3_BUCKET" ]]; then
    read -rp "S3 bucket name for cost data: " S3_BUCKET
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying to account ${ACCOUNT_ID} in ${REGION}"

# -------------------------------------------------------------------
# 1. Create S3 bucket (if it doesn't exist)
# -------------------------------------------------------------------
echo ""
echo "==> Checking S3 bucket ${S3_BUCKET}..."

if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
    echo "    Bucket already exists."
else
    echo "    Creating bucket..."
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION"
    else
        aws s3api create-bucket --bucket "$S3_BUCKET" --region "$REGION" \
            --create-bucket-configuration LocationConstraint="$REGION"
    fi
fi

echo "    Setting lifecycle policy (90-day expiration)..."
aws s3api put-bucket-lifecycle-configuration \
    --bucket "$S3_BUCKET" \
    --lifecycle-configuration '{
        "Rules": [{
            "ID": "expire-old-cost-data",
            "Status": "Enabled",
            "Filter": {"Prefix": "'"${S3_PREFIX}"'/"},
            "Expiration": {"Days": 90}
        }]
    }'

# -------------------------------------------------------------------
# 2. Create IAM role for the Lambda function
# -------------------------------------------------------------------
echo ""
echo "==> Checking IAM role ${ROLE_NAME}..."

TRUST_POLICY='{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}'

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "    Role already exists."
else
    echo "    Creating role..."
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "Execution role for ${FUNCTION_NAME} Lambda"
fi

LAMBDA_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "NetworkFlowMonitorRead",
            "Effect": "Allow",
            "Action": [
                "networkflowmonitor:StartQueryMonitorTopContributors",
                "networkflowmonitor:GetQueryStatusMonitorTopContributors",
                "networkflowmonitor:GetQueryResultsMonitorTopContributors"
            ],
            "Resource": "arn:aws:networkflowmonitor:${REGION}:${ACCOUNT_ID}:monitor/${MONITOR_NAME}"
        },
        {
            "Sid": "S3Write",
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET}/${S3_PREFIX}/*"
        },
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:*"
        },
        {
            "Sid": "AthenaQueryExecution",
            "Effect": "Allow",
            "Action": [
                "athena:StartQueryExecution",
                "athena:GetQueryExecution"
            ],
            "Resource": "arn:aws:athena:${REGION}:${ACCOUNT_ID}:workgroup/primary"
        },
        {
            "Sid": "GluePartitions",
            "Effect": "Allow",
            "Action": [
                "glue:GetTable",
                "glue:GetPartition",
                "glue:CreatePartition",
                "glue:BatchCreatePartition"
            ],
            "Resource": [
                "arn:aws:glue:${REGION}:${ACCOUNT_ID}:catalog",
                "arn:aws:glue:${REGION}:${ACCOUNT_ID}:database/${ATHENA_DATABASE}",
                "arn:aws:glue:${REGION}:${ACCOUNT_ID}:table/${ATHENA_DATABASE}/*"
            ]
        },
        {
            "Sid": "S3AthenaResults",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET}/athena-results/*"
        },
        {
            "Sid": "S3BucketAccess",
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
            "Resource": "arn:aws:s3:::${S3_BUCKET}"
        },
        {
            "Sid": "PricingAPIRead",
            "Effect": "Allow",
            "Action": ["pricing:GetProducts", "pricing:GetAttributeValues"],
            "Resource": "*"
        }
    ]
}
EOF
)

echo "    Updating inline policy..."
aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "${FUNCTION_NAME}-policy" \
    --policy-document "$LAMBDA_POLICY"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# -------------------------------------------------------------------
# 3. Package the Lambda
# -------------------------------------------------------------------
echo ""
echo "==> Packaging Lambda..."

ZIPFILE="/tmp/handler-$$.zip"
rm -f "$ZIPFILE"
(cd "${SCRIPT_DIR}/lambda" && zip -q "$ZIPFILE" handler.py)

echo "    Created ${ZIPFILE}"

# -------------------------------------------------------------------
# 4. Create or update the Lambda function
# -------------------------------------------------------------------
echo ""
echo "==> Deploying Lambda function ${FUNCTION_NAME}..."

ENV_VARS=$(cat <<EOF
{
    "Variables": {
        "MONITOR_NAME": "${MONITOR_NAME}",
        "S3_BUCKET": "${S3_BUCKET}",
        "S3_PREFIX": "${S3_PREFIX}",
        "QUERY_LIMIT": "500",
        "ATHENA_DATABASE": "${ATHENA_DATABASE}",
        "ATHENA_OUTPUT": "s3://${S3_BUCKET}/athena-results/",
        "HAS_S3_ENDPOINT": "${HAS_S3_ENDPOINT}",
        "HAS_DYNAMODB_ENDPOINT": "${HAS_DYNAMODB_ENDPOINT}"
    }
}
EOF
)

if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "    Function exists, updating code..."
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --region "$REGION" \
        --zip-file "fileb://${ZIPFILE}" \
        --publish

    # Wait for the update to propagate before updating configuration
    echo "    Waiting for update to complete..."
    aws lambda wait function-updated \
        --function-name "$FUNCTION_NAME" \
        --region "$REGION"

    echo "    Updating configuration..."
    aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --region "$REGION" \
        --runtime "$RUNTIME" \
        --handler handler.handler \
        --role "$ROLE_ARN" \
        --timeout "$TIMEOUT" \
        --memory-size "$MEMORY" \
        --environment "$ENV_VARS"
else
    echo "    Creating function..."
    # IAM role propagation can take a few seconds
    echo "    Waiting for IAM role to propagate..."
    sleep 10

    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --region "$REGION" \
        --runtime "$RUNTIME" \
        --handler handler.handler \
        --role "$ROLE_ARN" \
        --zip-file "fileb://${ZIPFILE}" \
        --timeout "$TIMEOUT" \
        --memory-size "$MEMORY" \
        --environment "$ENV_VARS" \
        --publish
fi

rm -f "$ZIPFILE"

FUNCTION_ARN=$(aws lambda get-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --query 'Configuration.FunctionArn' \
    --output text)

echo "    Function ARN: ${FUNCTION_ARN}"

# -------------------------------------------------------------------
# 5. Create IAM role for EventBridge Scheduler
# -------------------------------------------------------------------
echo ""
echo "==> Checking scheduler role ${SCHEDULE_ROLE_NAME}..."

SCHEDULER_TRUST='{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "scheduler.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}'

if aws iam get-role --role-name "$SCHEDULE_ROLE_NAME" >/dev/null 2>&1; then
    echo "    Role already exists."
else
    echo "    Creating role..."
    aws iam create-role \
        --role-name "$SCHEDULE_ROLE_NAME" \
        --assume-role-policy-document "$SCHEDULER_TRUST" \
        --description "Allows EventBridge Scheduler to invoke ${FUNCTION_NAME}"
fi

SCHEDULER_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": "lambda:InvokeFunction",
        "Resource": "${FUNCTION_ARN}"
    }]
}
EOF
)

aws iam put-role-policy \
    --role-name "$SCHEDULE_ROLE_NAME" \
    --policy-name "invoke-${FUNCTION_NAME}" \
    --policy-document "$SCHEDULER_POLICY"

SCHEDULE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SCHEDULE_ROLE_NAME}"

# -------------------------------------------------------------------
# 6. Create or update the EventBridge schedule
# -------------------------------------------------------------------
echo ""
echo "==> Checking EventBridge schedule ${SCHEDULE_NAME}..."

SCHEDULE_TARGET=$(cat <<EOF
{
    "Arn": "${FUNCTION_ARN}",
    "RoleArn": "${SCHEDULE_ROLE_ARN}",
    "Input": "{}"
}
EOF
)

if aws scheduler get-schedule --name "$SCHEDULE_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "    Schedule exists, updating..."
    aws scheduler update-schedule \
        --name "$SCHEDULE_NAME" \
        --region "$REGION" \
        --schedule-expression "rate(1 hour)" \
        --flexible-time-window '{"Mode": "OFF"}' \
        --target "$SCHEDULE_TARGET"
else
    echo "    Creating schedule..."
    # Scheduler role propagation
    sleep 10

    aws scheduler create-schedule \
        --name "$SCHEDULE_NAME" \
        --region "$REGION" \
        --schedule-expression "rate(1 hour)" \
        --flexible-time-window '{"Mode": "OFF"}' \
        --target "$SCHEDULE_TARGET"
fi

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
echo ""
echo "============================================="
echo "  Deployment complete!"
echo "============================================="
echo ""
echo "  Lambda function:  ${FUNCTION_NAME}"
echo "  Schedule:         ${SCHEDULE_NAME} (hourly)"
echo "  S3 output:        s3://${S3_BUCKET}/${S3_PREFIX}/"
echo "  Monitor:          ${MONITOR_NAME}"
echo "  S3 Gateway Endpoint:       ${HAS_S3_ENDPOINT}"
echo "  DynamoDB Gateway Endpoint: ${HAS_DYNAMODB_ENDPOINT}"
echo ""
echo "  Pricing is fetched dynamically from AWS Pricing API."
echo "  If no gateway endpoints exist, S3/DynamoDB traffic is"
echo "  charged at the NAT Gateway processing rate (\$0.045/GB)."
echo ""
echo "  To test manually:"
echo "    aws lambda invoke \\"
echo "      --function-name ${FUNCTION_NAME} \\"
echo "      --region ${REGION} \\"
echo "      --payload '{}' \\"
echo "      /dev/stdout"
echo ""
