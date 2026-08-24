"""
Smoke / integration tests -- run AFTER `docker-compose up -d --build` and
after you've run infra/schema.sql and uploaded at least one sample document
(see README steps 4-6).

Run with: pytest tests/test_smoke.py -v
Requires: requests, a reachable gateway at GATEWAY_URL (default localhost:8000)
"""
import os
import time

import pytest
import requests

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")


def test_health_endpoint():
    resp = requests.get(f"{GATEWAY_URL}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["status"] == "HEALTHY"


def test_query_returns_a_response():
    resp = requests.post(
        f"{GATEWAY_URL}/api/v1/query",
        json={
            "user_id": "test_user",
            "tenant_department": "risk_dept_01",
            "prompt": "What is the account risk status for ACC-9021?",
        },
        timeout=60,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("SUCCESS", "SUCCESS_CACHED")
    assert len(body["response"]) > 0


def test_pii_is_not_echoed_raw_in_logs_response():
    """Ask a question containing a fabricated name and confirm the pipeline
    doesn't error out -- full log inspection for the masked value is a
    manual check (see README 'Manual test: PII masking'), but this at
    minimum confirms the redaction step doesn't break the request."""
    resp = requests.post(
        f"{GATEWAY_URL}/api/v1/query",
        json={
            "user_id": "test_user",
            "tenant_department": "risk_dept_01",
            "prompt": "My name is Alexandra Petrov, what is my account balance?",
        },
        timeout=60,
    )
    assert resp.status_code == 200


def test_repeated_query_hits_cache_on_second_call():
    """First call should be SUCCESS (or already cached from a prior run);
    the immediate second call with the identical prompt must be cached."""
    payload = {
        "user_id": "test_user",
        "tenant_department": "risk_dept_01",
        "prompt": "Cache smoke test prompt -- do not change wording.",
    }
    requests.post(f"{GATEWAY_URL}/api/v1/query", json=payload, timeout=60)
    time.sleep(1)
    second = requests.post(f"{GATEWAY_URL}/api/v1/query", json=payload, timeout=60)
    assert second.json()["status"] == "SUCCESS_CACHED"


def test_tenant_isolation_two_departments_get_different_context():
    """Manual-ish isolation check: two tenants asking about 'my account'
    should not receive each other's account_id/balance in the response.
    This is a smoke-level signal, not a security audit -- see README for
    a fuller manual isolation test using two different account IDs."""
    resp_risk = requests.post(
        f"{GATEWAY_URL}/api/v1/query",
        json={"user_id": "u1", "tenant_department": "risk_dept_01",
              "prompt": "What is my account balance?"},
        timeout=60,
    ).json()
    resp_finance = requests.post(
        f"{GATEWAY_URL}/api/v1/query",
        json={"user_id": "u2", "tenant_department": "finance_dept_01",
              "prompt": "What is my account balance?"},
        timeout=60,
    ).json()
    # Not a strict assertion (the LLM's phrasing varies) -- print for manual
    # review; fails loudly only if one response is literally empty.
    assert len(resp_risk["response"]) > 0
    assert len(resp_finance["response"]) > 0
    print("\nRisk dept response:", resp_risk["response"])
    print("Finance dept response:", resp_finance["response"])


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
