#!/usr/bin/env bash
# Provisions the minimal AWS resources for the cost-optimized POC:
#   - S3 bucket (document uploads)
#   - SQS queue + S3 event notification
#   - RDS PostgreSQL (single-AZ, db.t4g.micro) with pgvector support
#   - IAM user + least-privilege policy for local docker-compose access
#
# Everything else (EKS, ALB, ElastiCache, OpenSearch Serverless) from the
# original design is deliberately NOT provisioned here -- see the cost
# optimization discussion for why. Requires: AWS CLI v2, configured with an
# admin (or sufficiently privileged) profile to run this script itself.
#
# Usage: ./provision.sh
# Idempotent-ish: safe to re-run, most calls tolerate "already exists".

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROJECT="ai-idp-poc"
BUCKET_NAME="${PROJECT}-docs-$(aws sts get-caller-identity --query Account --output text)"
QUEUE_NAME="${PROJECT}-ingestion-queue"
DB_IDENTIFIER="${PROJECT}-db"
DB_NAME="enterprise_db"
DB_MASTER_USER="poc_admin"
IAM_USER_NAME="${PROJECT}-app-user"
IAM_POLICY_NAME="${PROJECT}-app-policy"

echo "== Region: $REGION | Bucket: $BUCKET_NAME =="

# ---------------------------------------------------------------------------
# 1. S3 bucket (SSE-S3 encrypted)
# ---------------------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION" \
    $( [ "$REGION" != "us-east-1" ] && echo --create-bucket-configuration LocationConstraint=$REGION )
  aws s3api put-bucket-encryption --bucket "$BUCKET_NAME" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  echo "Created bucket $BUCKET_NAME"
else
  echo "Bucket $BUCKET_NAME already exists, skipping"
fi

# ---------------------------------------------------------------------------
# 2. SQS queue + S3 event notification on documents/ prefix
# ---------------------------------------------------------------------------
QUEUE_URL=$(aws sqs create-queue --queue-name "$QUEUE_NAME" --region "$REGION" \
  --attributes '{"VisibilityTimeout":"300","MessageRetentionPeriod":"86400","SqsManagedSseEnabled":"true"}' \
  --query QueueUrl --output text)
QUEUE_ARN=$(aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names QueueArn \
  --query "Attributes.QueueArn" --output text)

# Allow S3 to publish to this queue
aws sqs set-queue-attributes --queue-url "$QUEUE_URL" --attributes '{
  "Policy": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Action\":\"SQS:SendMessage\",\"Resource\":\"'"$QUEUE_ARN"'\",\"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:s3:::'"$BUCKET_NAME"'\"}}}]}"
}'

aws s3api put-bucket-notification-configuration --bucket "$BUCKET_NAME" --notification-configuration '{
  "QueueConfigurations": [{
    "QueueArn": "'"$QUEUE_ARN"'",
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "documents/"}]}}
  }]
}'
echo "Queue: $QUEUE_URL"

# ---------------------------------------------------------------------------
# 3. RDS PostgreSQL -- single instance, db.t4g.micro, single-AZ (POC sizing)
# ---------------------------------------------------------------------------
if [ -z "${DB_MASTER_PASSWORD:-}" ]; then
  echo "ERROR: export DB_MASTER_PASSWORD before running this script (8+ chars)." >&2
  exit 1
fi

if ! aws rds describe-db-instances --db-instance-identifier "$DB_IDENTIFIER" --region "$REGION" >/dev/null 2>&1; then
  aws rds create-db-instance \
    --db-instance-identifier "$DB_IDENTIFIER" \
    --db-instance-class db.t4g.micro \
    --engine postgres \
    --engine-version 16.9 \
    --master-username "$DB_MASTER_USER" \
    --master-user-password "$DB_MASTER_PASSWORD" \
    --allocated-storage 20 \
    --no-multi-az \
    --db-name "$DB_NAME" \
    --backup-retention-period 1 \
    --publicly-accessible \
    --region "$REGION"
  echo "Creating RDS instance -- this takes ~10 minutes. Waiting..."
  aws rds wait db-instance-available --db-instance-identifier "$DB_IDENTIFIER" --region "$REGION"
else
  echo "RDS instance $DB_IDENTIFIER already exists, skipping creation"
fi

DB_ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier "$DB_IDENTIFIER" --region "$REGION" \
  --query "DBInstances[0].Endpoint.Address" --output text)
echo "RDS endpoint: $DB_ENDPOINT"

echo ""
echo "IMPORTANT: --publicly-accessible above is for POC convenience (so your"
echo "laptop/EC2 box can reach it directly). Lock down the security group to"
echo "your own IP only, and remove public accessibility once you move past"
echo "the POC stage. See README 'Security notes for the POC'."

# ---------------------------------------------------------------------------
# 4. IAM user + least-privilege policy for local docker-compose access
#    (No EKS/IRSA in this design, so the app authenticates with an IAM user's
#    access keys via .env -- see README's security caveat on rotating/
#    deleting this user once you're done.)
# ---------------------------------------------------------------------------
cat > /tmp/${IAM_POLICY_NAME}.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${BUCKET_NAME}", "arn:aws:s3:::${BUCKET_NAME}/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      "Resource": "${QUEUE_ARN}"
    }
  ]
}
EOF

POLICY_ARN=$(aws iam create-policy --policy-name "$IAM_POLICY_NAME" \
  --policy-document file:///tmp/${IAM_POLICY_NAME}.json \
  --query "Policy.Arn" --output text 2>/dev/null || \
  aws iam list-policies --query "Policies[?PolicyName=='${IAM_POLICY_NAME}'].Arn" --output text)

if ! aws iam get-user --user-name "$IAM_USER_NAME" >/dev/null 2>&1; then
  aws iam create-user --user-name "$IAM_USER_NAME"
fi
aws iam attach-user-policy --user-name "$IAM_USER_NAME" --policy-arn "$POLICY_ARN"

echo ""
echo "== Create an access key for docker-compose (run manually, shown once) =="
echo "aws iam create-access-key --user-name $IAM_USER_NAME"

# ---------------------------------------------------------------------------
# Summary -- paste these into your .env
# ---------------------------------------------------------------------------
echo ""
echo "=================== Copy into your .env file ==================="
echo "AWS_REGION=$REGION"
echo "S3_BUCKET=$BUCKET_NAME"
echo "SQS_QUEUE_URL=$QUEUE_URL"
echo "RDS_ENDPOINT=$DB_ENDPOINT"
echo "RDS_PORT=5432"
echo "RDS_DB_NAME=$DB_NAME"
echo "==================================================================="
echo ""
echo "Next: also enable Bedrock model access via the console (see README"
echo "step 1c) -- this cannot be scripted reliably across regions/accounts."
