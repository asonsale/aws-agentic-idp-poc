"""
Nightly quality-gate script.

The original design pulled structured JSON logs from CloudWatch Logs
Insights. For the POC, the gateway container just logs JSON to stdout,
which `docker logs gateway` captures -- so this script reads from a
docker logs export file instead of calling CloudWatch, removing a
dependency for local runs. Point CLOUDWATCH_MODE=1 (and set
AWS credentials) to restore the original CloudWatch-based path.
"""

import json
import os

import pandas as pd
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_recall

LOG_FILE = os.environ.get("GATEWAY_LOG_FILE", "gateway_logs.jsonl")


def load_local_log_records() -> list:
    if not os.path.exists(LOG_FILE):
        print(f"No local log file found at {LOG_FILE} -- run "
              f"`docker logs gateway > {LOG_FILE}` first, or set CLOUDWATCH_MODE=1.")
        return []
    records = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_cloudwatch_log_records() -> list:
    import boto3
    logs_client = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    response = logs_client.filter_log_events(
        logGroupName=os.environ.get("LOG_GROUP", "/aws/ec2/ai-platform/telemetry"),
        filterPattern='{ $.service = "api-gateway" }',
    )
    return [json.loads(event["message"]) for event in response.get("events", []) if "message" in event]


def execute_nightly_audit():
    log_records = load_cloudwatch_log_records() if os.environ.get("CLOUDWATCH_MODE") else load_local_log_records()

    log_records = [r for r in log_records if "masked_prompt" in r and "response" in r]
    if not log_records:
        print("No audit telemetry logs with question/answer/context fields found.")
        return

    evaluation_data = {
        "question": [r.get("masked_prompt", "") for r in log_records],
        "contexts": [[r.get("retrieved_context", "")] for r in log_records],
        "answer": [r.get("response", "") for r in log_records],
    }

    dataset = Dataset.from_dict(evaluation_data)
    audit_results = evaluate(dataset=dataset, metrics=[faithfulness, answer_relevancy, context_recall])

    print("\nSYSTEM COMPLIANCE REPORT METRICS:")
    print(audit_results.to_pandas().mean(numeric_only=True))


if __name__ == "__main__":
    execute_nightly_audit()
