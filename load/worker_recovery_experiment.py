"""Controlled local worker-termination experiment; prints one JSON result."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

from sqlalchemy import select

from incidentpilot.models.db import AgentRun
from incidentpilot.persistence.jobs import claim_next_run, create_incident_and_run
from incidentpilot.persistence.session import get_session_factory


async def claim_and_hold(worker_id: str, run_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        claim = await claim_next_run(
            session,
            worker_id=worker_id,
            lease_seconds=10,
            max_attempts=3,
            run_id=run_id,
        )
    if claim is None:
        raise RuntimeError("no run available to claim")
    print(json.dumps({"claimed_run_id": claim.run_id, "worker_id": worker_id}), flush=True)
    await asyncio.sleep(300)


async def read_owned(worker_id: str) -> AgentRun | None:
    factory = get_session_factory()
    async with factory() as session:
        return (
            await session.execute(select(AgentRun).where(AgentRun.worker_id == worker_id))
        ).scalars().first()


async def read_run(run_id: str) -> AgentRun | None:
    factory = get_session_factory()
    async with factory() as session:
        return (
            await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        ).scalar_one_or_none()


async def run_experiment() -> None:
    factory = get_session_factory()
    async with factory() as session:
        _, seeded_run = await create_incident_and_run(
            session,
            incident_data={
                "incident_id": f"CRASH-{uuid.uuid4().hex}",
                "title": "controlled worker crash recovery",
                "service": "orders-api",
                "symptom": "synthetic latency increase",
            },
            max_queued_runs=1000,
        )

    env = dict(os.environ)
    env.update(
        {
            "TOOL_BACKEND": "fake",
            "LLM_ENABLED": "false",
            "SANDBOX_ENABLED": "false",
            "GITHUB_INTEGRATION_ENABLED": "false",
            "WORKER_CONCURRENCY": "1",
            "JOB_LEASE_SECONDS": "10",
            "JOB_HEARTBEAT_SECONDS": "2",
            "MAX_JOB_ATTEMPTS": "3",
        }
    )
    crashed_worker_id = f"crash-worker-{uuid.uuid4().hex[:8]}"
    child = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--claim-and-hold",
            crashed_worker_id,
            "--target-run-id",
            seeded_run.run_id,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    claimed: AgentRun | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        claimed = await read_owned(crashed_worker_id)
        if claimed is not None:
            break
        await asyncio.sleep(0.2)
    if claimed is None:
        child.terminate()
        raise RuntimeError("claiming worker did not acquire a run")

    run_id = claimed.run_id
    first_attempt = claimed.attempt_count
    child.terminate()
    child.wait(timeout=10)
    await asyncio.sleep(10.5)

    replacement = subprocess.Popen(
        [sys.executable, "-m", "incidentpilot.worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    recovered = False
    final_status = "UNKNOWN"
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            current = await read_run(run_id)
            if current is not None:
                recovered = current.attempt_count > first_attempt
                final_status = current.status
                if recovered and current.status not in {"PENDING", "RUNNING"}:
                    break
            await asyncio.sleep(0.5)
    finally:
        replacement.terminate()
        replacement.wait(timeout=10)

    print(
        json.dumps(
            {
                "experiment": "worker_process_kill_recovery",
                "status": "PASS" if recovered else "FAIL",
                "run_id": run_id,
                "first_attempt_count": first_attempt,
                "recovered_attempt_count": (
                    (await read_run(run_id)).attempt_count if await read_run(run_id) else None
                ),
                "final_status": final_status,
                "github_enabled": False,
            }
        )
    )
    if not recovered:
        raise RuntimeError("replacement worker did not recover the expired lease")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim-and-hold")
    parser.add_argument("--target-run-id")
    args = parser.parse_args()
    coroutine = (
        claim_and_hold(args.claim_and_hold, args.target_run_id)
        if args.claim_and_hold
        else run_experiment()
    )
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(coroutine)
    else:
        asyncio.run(coroutine)


if __name__ == "__main__":
    main()
