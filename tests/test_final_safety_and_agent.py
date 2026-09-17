from __future__ import annotations

from pathlib import Path

import pytest

from incidentpilot.agent.decision import TOOL_ARGUMENT_GUIDE, AgentDecisionModel
from incidentpilot.llm.provider import FakeLLMProvider
from incidentpilot.sandbox.manager import SandboxManager
from incidentpilot.tools.mcp_adapter import MCPReadonlyAdapter


@pytest.mark.asyncio
async def test_decision_model_selects_next_tool_from_state() -> None:
    model = AgentDecisionModel(FakeLLMProvider())
    incident = {"service": "orders-api", "repository": "reference/orders_api"}
    first = await model.next_action(incident, [])
    assert first.tool_selection and first.tool_selection.tool_name == "read_metrics"
    second = await model.next_action(
        incident,
        [{"evidence_id": "E1", "source": "read_metrics", "result": {}}],
    )
    assert second.tool_selection and second.tool_selection.tool_name == "read_logs"
    after_rejection = await model.next_action(
        {**incident, "rejected_hypotheses": 1},
        [
            {"evidence_id": "E1", "source": "read_metrics", "result": {}},
            {"evidence_id": "E2", "source": "read_logs", "result": {}},
        ],
    )
    assert after_rejection.tool_selection
    assert after_rejection.tool_selection.tool_name == "inspect_git_diff"


def test_real_model_receives_exact_tool_argument_contracts() -> None:
    assert 'read_metrics: {"service": string, "window": string}' in TOOL_ARGUMENT_GUIDE
    assert "Do not add environment, metrics" in TOOL_ARGUMENT_GUIDE


@pytest.mark.asyncio
async def test_live_mcp_never_silently_falls_back_to_fake() -> None:
    adapter = MCPReadonlyAdapter(live=True)
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.call_tool("read_metrics", {"service": "orders-api"})


@pytest.mark.parametrize("path", ["../escape.py", ".env", ".git/config", "secrets/token"])
def test_patch_forbidden_paths_are_rejected(tmp_path: Path, path: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("x = 1\n", encoding="utf-8")
    manager = SandboxManager()
    manager.create_workspace("unsafe", source)
    patch = (
        '{"type":"simple_replace","edits":[{"path":"'
        + path.replace("\\", "/")
        + '","find":"x","replace":"y"}]}'
    )
    with pytest.raises(ValueError, match="unsafe|forbidden|outside"):
        manager.apply_patch("unsafe", patch)
    manager.destroy_workspace("unsafe")


def test_mcp_source_boundary_rejects_secrets_and_other_repositories() -> None:
    from ops_mcp.ops_readonly.server import read_source_code

    assert read_source_code(path=".env").get("error")
    assert read_source_code(repository="..", path="README.md").get("error")
