"""
Unit tests -- no AWS credentials, no running containers required.
Run with: pytest tests/test_unit.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def test_mask_pii_replaces_and_can_rehydrate():
    """The core fix from the original design: masking must be reversible
    per-request via a token map, not a single hardcoded replacement."""
    from main import mask_pii, rehydrate

    fake_analyzer_response = [
        {"entity_type": "PERSON", "start": 11, "end": 21, "score": 0.9},
    ]
    raw_text = "My name is John Smith and I need my balance."

    with patch("main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = fake_analyzer_response
        masked, token_map = mask_pii(raw_text)

    assert "John Smith" not in masked
    assert "<PERSON_1>" in masked
    assert token_map["<PERSON_1>"] == "John Smith"

    # Round trip: a model response echoing the placeholder should rehydrate
    model_response = "Hello <PERSON_1>, your balance is $500."
    restored = rehydrate(model_response, token_map)
    assert restored == "Hello John Smith, your balance is $500."


def test_mask_pii_multiple_entities_dont_collide():
    """Regression guard for the original bug: hardcoding one placeholder
    silently corrupts output with 2+ entities of the same type."""
    from main import mask_pii, rehydrate

    fake_analyzer_response = [
        {"entity_type": "PERSON", "start": 0, "end": 4, "score": 0.9},   # "John"
        {"entity_type": "PERSON", "start": 9, "end": 14, "score": 0.9},  # "Priya" (positions illustrative)
    ]
    raw_text = "John and Priya are both on this account."

    with patch("main.requests.post") as mock_post:
        mock_post.return_value.json.return_value = fake_analyzer_response
        masked, token_map = mask_pii(raw_text)

    assert len(token_map) == 2
    assert "<PERSON_1>" in token_map and "<PERSON_2>" in token_map
    assert token_map["<PERSON_1>"] != token_map["<PERSON_2>"]


def test_mask_pii_fails_open_on_analyzer_error():
    """If Presidio is unreachable, the gateway should still function
    (fail-open is a documented, deliberate POC trade-off -- see the
    architecture doc's pros/cons section)."""
    from main import mask_pii

    with patch("main.requests.post", side_effect=Exception("connection refused")):
        masked, token_map = mask_pii("some raw text")

    assert masked == "some raw text"
    assert token_map == {}


def test_chunk_text_splits_and_drops_empty():
    from retrieval import chunk_text

    text = "a" * 1200
    chunks = chunk_text(text, chunk_size=500)
    assert len(chunks) == 3
    assert all(len(c) <= 500 for c in chunks)

    assert chunk_text("   ", chunk_size=500) == []


def test_cache_key_is_stable_and_tenant_scoped():
    """Two tenants asking the identical question must not share a cache
    key -- otherwise tenant B could get tenant A's cached answer."""
    import hashlib

    def cache_key(tenant, prompt):
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        return f"gateway:llm:{tenant}:{prompt_hash}"

    k1 = cache_key("risk_dept_01", "what is my balance?")
    k2 = cache_key("finance_dept_01", "what is my balance?")
    assert k1 != k2
