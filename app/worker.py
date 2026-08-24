import os
import json
import time
import logging

import boto3

from retrieval import embed_text, get_db_connection, chunk_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IngestionWorker")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)
sqs_client = boto3.client("sqs", region_name=AWS_REGION)

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]


def tenant_from_key(object_key: str) -> str:
    """Expects uploads at documents/<tenant_id>/<filename>. Falls back to
    'default' so a malformed key doesn't crash the worker."""
    parts = object_key.split("/")
    if len(parts) >= 3 and parts[0] == "documents":
        return parts[1]
    return "default"


def process_s3_event(message_body: str):
    records = json.loads(message_body).get("Records", [])
    for record in records:
        bucket_name = record["s3"]["bucket"]["name"]
        object_key = record["s3"]["object"]["key"]
        tenant_id = tenant_from_key(object_key)
        logger.info(f"Processing upload: s3://{bucket_name}/{object_key} (tenant={tenant_id})")

        obj = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        text = obj["Body"].read().decode("utf-8", errors="ignore")
        chunks = chunk_text(text)

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SET LOCAL app.current_tenant = %s;", (tenant_id,))
            for chunk in chunks:
                vector = embed_text(chunk)
                cur.execute(
                    "INSERT INTO document_chunks (tenant_id, source_key, chunk_text, embedding) "
                    "VALUES (%s, %s, %s, %s)",
                    (tenant_id, object_key, chunk, vector),
                )
            conn.commit()
        finally:
            conn.close()

        logger.info(f"Indexed {len(chunks)} chunks from {object_key} for tenant {tenant_id}")


def start_worker():
    logger.info("Starting asynchronous SQS ingestion worker...")
    while True:
        try:
            response = sqs_client.receive_message(
                QueueUrl=SQS_QUEUE_URL, MaxNumberOfMessages=5, WaitTimeSeconds=20
            )
            for msg in response.get("Messages", []):
                process_s3_event(msg["Body"])
                sqs_client.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                logger.info("SQS message processed and deleted.")
        except Exception as e:
            logger.error(f"Error in worker loop: {str(e)}")
            time.sleep(5)


if __name__ == "__main__":
    start_worker()
