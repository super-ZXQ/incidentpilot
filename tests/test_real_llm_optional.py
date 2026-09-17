from __future__ import annotations

import os

import pytest

from incidentpilot.agent.decision import AgentDecisionModel
from incidentpilot.llm.provider import OpenAICompatibleProvider


@pytest.mark.llm
@pytest.mark.asyncio
async def test_real_llm_structured_plan_optional() -> None:
    key = os.environ.get("LLM_API_KEY")
    if not key:
        pytest.skip("LLM_API_KEY not configured")
    provider = OpenAICompatibleProvider(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=key,
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )
    plan = await AgentDecisionModel(provider).plan(
        {"service": "orders-api", "symptom": "P95 latency increased"}
    )
    assert plan.steps
