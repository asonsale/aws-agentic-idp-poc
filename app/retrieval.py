"""
Shared helper for the RAG side of the platform.

Replaces the original Bedrock Knowledge Bases + OpenSearch Serverless design
with Amazon Bedrock embeddings (Titan Embed Text v2) + pgvector on the same
Postgres instance already used for the MCP tool queries. This removes the
OpenSearch Serverless cost floor (~$175/mo minimum) for the POC, at the cost
of managing chunking/indexing yourself instead of using a managed KB. See
the architecture doc, section 6, for the cost trade-off this is based on.
"""

import os
import json
import logging

import boto3
import psycopg2
from pgvector.psycopg2 import register_vector
from pgvector import Vector

logger = logging.getLogger("Retrieval")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "1024"))

bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def embed_text(text: str) -> list:
    """Call Bedrock Titan Embed Text v2 and return a float vector."""
    body = json.dumps({
        "inputText": text,
        "dimensions": EMBED_DIMENSIONS,
        "normalize": True,
    })
    response = bedrock_runtime.invoke_model(modelId=EMBED_MODEL_ID, body=body)
    payload = json.loads(response["body"].read())
    return payload["embedding"]


def get_db_connection():
    """Open a Postgres connection with the pgvector type adapter registered."""
    conn = psycopg2.connect(
        host=os.environ["RDS_ENDPOINT"],
        port=int(os.environ.get("RDS_PORT", 5432)),
        dbname=os.environ.get("RDS_DB_NAME", "enterprise_db"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )
    register_vector(conn)
    return conn


def retrieve_context(query: str, tenant_id: str, top_k: int = 3) -> str:
    """
    Tenant-scoped vector similarity search over document_chunks.
    Mirrors the mandatory tenant_id filter the original design applied
    at the Bedrock Knowledge Base layer -- here it's a WHERE clause plus
    RLS, so isolation holds even if a caller forgets the filter.
    """
    query_vector = embed_text(query)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL app.current_tenant = %s;", (tenant_id,))
        cur.execute(
            """
            SELECT chunk_text
            FROM document_chunks
            WHERE tenant_id = %s
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (tenant_id, Vector(query_vector), top_k),
        )
        rows = cur.fetchall()
        return " ".join(r[0] for r in rows) if rows else "No policy context found."
    finally:
        conn.close()


def chunk_text(text: str, chunk_size: int = 500) -> list:
    """Naive fixed-size chunker -- adequate for a POC; swap for a
    sentence/paragraph-aware splitter before this handles real documents."""
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size) if text[i:i + chunk_size].strip()]
