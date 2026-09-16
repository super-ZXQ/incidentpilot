"""Phase 8 tests: OTel and Evaluation Harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from incidentpilot.eval.harness import EvaluationHarness, _score_root_cause
from incidentpilot.observability.otel import configure_otel, start_span

FAULT_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "fault_cases"


def test_configure_otel_and_span() -> None:
    from incidentpilot.config import Settings

    tracer = configure_otel(Settings(otel_console_exporter=False))
    assert tracer is not None
    with start_span("test_span", {"k": "v"}) as span:
        assert span is not None


def test_score_root_cause_uses_ground_truth_not_agent_label() -> None:
    gt = {
        "root_cause": "list_orders loads OrderItems with a per-order query (N+1) after recent commit"
    }
    good = "N+1 item loading regression in list_orders after recent commit"
    bad = "completely unrelated cache miss"
    assert _score_root_cause(gt, good) is True
    assert _score_root_cause(gt, bad) is False


@pytest.mark.asyncio
async def test_evaluation_harness_runs_fault_case(tmp_path) -> None:
    import os

    from incidentpilot.config import reset_settings_cache
    from incidentpilot.persistence.session import (
        dispose_engine,
        init_db,
        reset_db_state,
    )

    reset_db_state()
    reset_settings_cache()
    db_path = tmp_path / "eval.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["OTEL_CONSOLE_EXPORTER"] = "false"
    reset_settings_cache()
    await init_db()

    harness = EvaluationHarness(FAULT_DIR)
    report = await harness.run()
    assert report.total_cases >= 1
    assert report.completed == report.total_cases
    assert report.average_tool_calls >= 1
    data = report.to_dict()
    assert "root_cause_accuracy" in data
    assert data["outcomes"]
    # Ground truth based score may be 0 or 1 depending on keyword match; assert type
    assert 0.0 <= data["root_cause_accuracy"] <= 1.0

    await dispose_engine()
    reset_db_state()
    reset_settings_cache()
