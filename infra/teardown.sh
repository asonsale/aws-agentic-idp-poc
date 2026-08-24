#!/usr/bin/env bash
# Deletes everything provision.sh created. Run this when you're done with a
# POC session to stop incurring charges -- RDS in particular bills by the
# hour even when idle.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROJECT="ai-idp-poc"
BUCKET_NAME="${PROJECT}-docs-$(aws sts get-caller-identity --query Account --output text)"
QUEUE_NAME="${PROJECT}-ingestion-queue"
DB_IDENTIFIER="${PROJECT}-db"
IAM_USER_NAME="${PROJECT}-app-user"
IAM_POLICY_NAME="${PROJECT}-app-policy"

read -p "This will permanently delete the RDS instance, S3 bucket, SQS queue, and IAM user for $PROJECT. Type 'yes' to continue: " CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 1; }

echo "Deleting RDS instance (no final snapshot -- POC data only)..."
aws rds delete-db-instance --db-instance-identifier "$DB_IDENTIFIER" --skip-final-snapshot --region "$REGION" 2>/dev/null || true

echo "Emptying and deleting S3 bucket..."
aws s3 rm "s3://$BUCKET_NAME" --recursive 2>/dev/null || true
aws s3api delete-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null || true

echo "Deleting SQS queue..."
QUEUE_URL=$(aws sqs get-queue-url --queue-name "$QUEUE_NAME" --region "$REGION" --query QueueUrl --output text 2>/dev/null || true)
[ -n "$QUEUE_URL" ] && aws sqs delete-queue --queue-url "$QUEUE_URL" --region "$REGION" || true

echo "Detaching and deleting IAM user/policy..."
POLICY_ARN=$(aws iam list-policies --query "Policies[?PolicyName=='${IAM_POLICY_NAME}'].Arn" --output text)
if [ -n "$POLICY_ARN" ]; then
  aws iam detach-user-policy --user-name "$IAM_USER_NAME" --policy-arn "$POLICY_ARN" 2>/dev/null || true
  aws iam delete-policy --policy-arn "$POLICY_ARN" 2>/dev/null || true
fi
for KEY in $(aws iam list-access-keys --user-name "$IAM_USER_NAME" --query "AccessKeyMetadata[].AccessKeyId" --output text 2>/dev/null); do
  aws iam delete-access-key --user-name "$IAM_USER_NAME" --access-key-id "$KEY" 2>/dev/null || true
done
aws iam delete-user --user-name "$IAM_USER_NAME" 2>/dev/null || true

echo "Teardown complete. Double-check the AWS Console (RDS deletion takes a few minutes)."
