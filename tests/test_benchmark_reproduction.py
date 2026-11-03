"""The benchmark's own reproduction step: baseline, fault, faulted window."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from incidentpilot.config import Settings
from incidentpilot.eval.harness import BASELINE_SAMPLES, FAULTED_SAMPLES, EvaluationHarness

ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        pass


class _RecordingClient:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str) -> _Response:
        self.calls.append("GET")
        return _Response(self.status_code)

    async def post(self, url: str, json: dict | None = None) -> _Response:
        self.calls.append("POST")
        return _Response(200)


async def _reproduce(monkeypatch, status_code: int = 200) -> list[str]:
    client = _RecordingClient(status_code)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    harness = EvaluationHarness(
        ROOT / "benchmarks" / "fault_cases", Settings(reference_orders_api_url="http://reference.test")
    )
    case = harness.load_cases()[0]
    await harness._setup_and_reproduce(case)
    return client.calls


async def test_reproduction_samples_a_baseline_before_the_fault(monkeypatch):
    """A latency case reports a shift, so the fixture has to leave a before and an after.

    One post-fault request gives the Agent a single number that matches no report, and
    the honest answer to that is to refuse the case.
    """
    calls = await _reproduce(monkeypatch)
    assert calls == ["GET"] * BASELINE_SAMPLES + ["POST"] + ["GET"] * FAULTED_SAMPLES


@pytest.mark.parametrize("status_code", [418, 503])
async def test_reproduction_refuses_an_unusable_response(monkeypatch, status_code):
    with pytest.raises(RuntimeError, match="fault reproduction failed"):
        await _reproduce(monkeypatch, status_code)
