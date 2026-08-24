import os
import json
import time
import logging
import hashlib
from typing import TypedDict

import boto3
import requests
import redis
from fastapi import FastAPI
from pydantic import BaseModel
from langgraph.graph import StateGraph, START, END

from retrieval import retrieve_context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Gateway")

app = FastAPI(title="Enterprise Agentic AI Gateway (POC)", version="1.0.0-poc")

# ---------------------------------------------------------------------------
# Config -- Docker Compose service names instead of EKS cluster.local DNS
# ---------------------------------------------------------------------------
PRESIDIO_ANALYZER = os.environ.get("PRESIDIO_ANALYZER_URL", "http://presidio-analyzer:3000/analyze")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8001/sse")
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
redis_gateway_cache = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, db=1)


class QueryRequest(BaseModel):
    user_id: str
    tenant_department: str
    prompt: str


class AgentState(TypedDict):
    raw_prompt: str
    tenant_id: str
    masked_prompt: str
    pii_token_map: dict
    retrieved_context: str
    mcp_tool_output: str
    final_output: str


# ---------------------------------------------------------------------------
# PII masking -- FIX: build our own reversible token map instead of the
# original hardcoded `.replace("<PERSON>", "John Doe")`. Presidio's
# anonymizer discards the original value by design (that's the point of
# anonymization); to safely re-hydrate later, we mask using the analyzer's
# offsets ourselves and keep the mapping in-process for this request only.
# ---------------------------------------------------------------------------
def _resolve_overlaps(entities: list) -> list:
    """
    Presidio commonly returns multiple overlapping candidate entities for the
    same text -- e.g. an email address's domain also matches its URL
    recognizer, so "rajesh.kumar@example.com" comes back as both an
    EMAIL_ADDRESS span and a URL span covering part of the same characters.
    Blindly replacing every returned span (the original behavior) lets a
    later replacement slice into text a previous replacement already
    inserted, corrupting the masked prompt (e.g. a stray "DDRESS_1>"
    fragment leaking into the text sent to Bedrock).

    We keep only the best entity per cluster of overlapping spans: highest
    confidence score first, then longest span as a tiebreaker. Entities that
    don't overlap anything else are kept as-is.
    """
    if not entities:
        return entities

    ordered = sorted(entities, key=lambda e: e["start"])
    resolved = []
    cluster = [ordered[0]]

    def flush(cluster):
        best = max(
            cluster,
            key=lambda e: (e.get("score", 0), e["end"] - e["start"]),
        )
        resolved.append(best)

    for ent in ordered[1:]:
        # Overlaps if it starts before the furthest end reached in the
        # current cluster.
        cluster_end = max(c["end"] for c in cluster)
        if ent["start"] < cluster_end:
            cluster.append(ent)
        else:
            flush(cluster)
            cluster = [ent]
    flush(cluster)

    return resolved


def mask_pii(raw_text: str) -> tuple:
    try:
        entities = requests.post(
            PRESIDIO_ANALYZER, json={"text": raw_text, "language": "en"}, timeout=5
        ).json()
    except Exception as e:
        logger.error(f"Presidio analyzer call failed, forwarding unmasked text: {e}")
        return raw_text, {}

    entities = _resolve_overlaps(entities)

    # Replace spans back-to-front so earlier offsets stay valid.
    entities = sorted(entities, key=lambda e: e["start"], reverse=True)
    masked = raw_text
    token_map = {}
    counters = {}
    for ent in entities:
        etype = ent["entity_type"]
        counters[etype] = counters.get(etype, 0) + 1
        token = f"<{etype}_{counters[etype]}>"
        original_value = raw_text[ent["start"]:ent["end"]]
        token_map[token] = original_value
        masked = masked[:ent["start"]] + token + masked[ent["end"]:]
    return masked, token_map


def rehydrate(text: str, token_map: dict) -> str:
    for token, original in token_map.items():
        text = text.replace(token, original)
    return text


# ---------------------------------------------------------------------------
# LangGraph nodes -- same linear, bounded pipeline as the original design
# ---------------------------------------------------------------------------
def tier1_redaction(state: AgentState) -> dict:
    masked, token_map = mask_pii(state["raw_prompt"])
    return {"masked_prompt": masked, "pii_token_map": token_map}


def tier2_retrieval(state: AgentState) -> dict:
    try:
        context = retrieve_context(state["masked_prompt"], state["tenant_id"])
    except Exception as e:
        logger.error(f"Retrieval error: {e}")
        context = "No policy context found."
    return {"retrieved_context": context}


async def tier2_mcp(state: AgentState) -> dict:
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(MCP_SERVER_URL) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                res = await session.call_tool(
                    "query_customer_risk_multitenant",
                    arguments={"account_id": "ACC-9021", "tenant_id": state["tenant_id"]},
                )
                return {"mcp_tool_output": res.content[0].text}
    except Exception as e:
        logger.error(f"MCP call failed: {e}")
        return {"mcp_tool_output": "MCP_UNAVAILABLE"}


def tier3_inference(state: AgentState) -> dict:
    sys_prompt = f"Context: {state['retrieved_context']}. DB Data: {state['mcp_tool_output']}"
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "system": sys_prompt,
        "messages": [{"role": "user", "content": state["masked_prompt"]}],
    }
    response = bedrock_runtime.invoke_model(modelId=MODEL_ID, body=json.dumps(payload))
    raw_out = json.loads(response["body"].read())["content"][0]["text"]
    final = rehydrate(raw_out, state["pii_token_map"])
    return {"final_output": final}


builder = StateGraph(AgentState)
builder.add_node("redaction", tier1_redaction)
builder.add_node("retrieval", tier2_retrieval)
builder.add_node("mcp", tier2_mcp)
builder.add_node("inference", tier3_inference)
builder.add_edge(START, "redaction")
builder.add_edge("redaction", "retrieval")
builder.add_edge("retrieval", "mcp")
builder.add_edge("mcp", "inference")
builder.add_edge("inference", END)
pipeline = builder.compile()


@app.get("/health")
def health():
    return {"status": "HEALTHY"}


@app.post("/api/v1/query")
async def execute_agent_query(payload: QueryRequest):
    start_time = time.time()

    prompt_hash = hashlib.sha256(payload.prompt.encode()).hexdigest()
    cache_key = f"gateway:llm:{payload.tenant_department}:{prompt_hash}"
    cached_resp = redis_gateway_cache.get(cache_key)
    if cached_resp:
        return {"status": "SUCCESS_CACHED", "department": payload.tenant_department, "response": json.loads(cached_resp)}

    result = await pipeline.ainvoke({"raw_prompt": payload.prompt, "tenant_id": payload.tenant_department})

    redis_gateway_cache.setex(cache_key, 3600, json.dumps(result["final_output"]))
    elapsed_ms = (time.time() - start_time) * 1000
    logger.info(json.dumps({
        "event": "query_complete",
        "tenant_id": payload.tenant_department,
        "latency_ms": round(elapsed_ms, 1),
        # Fields below feed evaluation_cron.py's nightly Ragas audit.
        # masked_prompt/retrieved_context are already PII-scrubbed.
        "masked_prompt": result["masked_prompt"],
        "retrieved_context": result["retrieved_context"],
        "response": result["final_output"],
    }))

    return {"status": "SUCCESS", "department": payload.tenant_department, "response": result["final_output"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)