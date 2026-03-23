"""Unit tests for the task router (no RebaseKit calls needed)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from workers.router import execute_task, TASK_CREDITS
from workers.base import WorkerError


def make_mock_client(**kwargs):
    client = MagicMock()
    client.post = AsyncMock(**kwargs)
    return client


@pytest.mark.asyncio
async def test_task_credits_coverage():
    """All task types should have credit costs defined."""
    task_types = [
        "web_research", "entity_lookup", "document_parse", "data_transform",
        "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
        "code_execute", "web_intel",
    ]
    for t in task_types:
        assert t in TASK_CREDITS, f"Missing credits for {t}"
        assert TASK_CREDITS[t] > 0


@pytest.mark.asyncio
async def test_unknown_task_raises():
    client = make_mock_client()
    with pytest.raises(WorkerError) as exc_info:
        await execute_task("definitely_not_a_type", {}, client)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_llm_generate_routing():
    client = make_mock_client(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
    })
    result = await execute_task("llm_generate", {
        "messages": [{"role": "user", "content": "Say hello"}]
    }, client)
    client.post.assert_called_once()
    assert result["summary"] == "Hello!"


@pytest.mark.asyncio
async def test_pii_detect_routing():
    client = make_mock_client(return_value={"entities": [
        {"type": "EMAIL", "value": "test@example.com"}
    ]})
    result = await execute_task("pii_detect", {"text": "My email is test@example.com"}, client)
    assert "1 PII" in result["summary"]


@pytest.mark.asyncio
async def test_web_research_routing():
    client = make_mock_client(return_value={
        "summary": "Page content here",
        "content": "Full page content",
    })
    result = await execute_task("web_research", {"url": "https://example.com"}, client)
    assert result["summary"] == "Page content here"
