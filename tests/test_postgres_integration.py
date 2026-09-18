from __future__ import annotations

import asyncio
import os

import psycopg
import pytest


@pytest.mark.integration
def test_alembic_schema_exists_in_postgres() -> None:
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL not configured")
    with psycopg.connect(url, connect_timeout=3) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tables = {row[0] for row in cursor.fetchall()}
    assert {"incidents", "agent_runs", "evidence", "patch_artifacts", "approvals"} <= tables


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_postgres_workers_claim_one_run() -> None:
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL not configured")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from incidentpilot.persistence.jobs import claim_next_run, create_incident_and_run

    async_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        _, expected = await create_incident_and_run(
            session,
            incident_data={
                "title": "postgres worker claim",
                "service": "orders-api",
                "symptom": "latency",
            },
            max_queued_runs=1000,
        )

    async def claim(worker_id: str):
        async with factory() as session:
            return await claim_next_run(
                session,
                worker_id=worker_id,
                lease_seconds=30,
                max_attempts=3,
            )

    claims = await asyncio.gather(claim("pg-worker-a"), claim("pg-worker-b"))
    matching = [item for item in claims if item is not None and item.run_id == expected.run_id]
    assert len(matching) == 1
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_checkpoint_interrupt_resume() -> None:
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL not configured")

    from typing import TypedDict
    from uuid import uuid4

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    class State(TypedDict, total=False):
        value: str

    async def wait(state: State) -> State:
        decision = interrupt({"kind": "approval"})
        return {**state, "value": str(decision)}

    builder = StateGraph(State)
    builder.add_node("wait", wait)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    config = {"configurable": {"thread_id": f"checkpoint-{uuid4()}"}}
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        paused = await graph.ainvoke({}, config=config)
        assert paused.get("__interrupt__")
        resumed = await graph.ainvoke(Command(resume="APPROVE"), config=config)
        assert resumed["value"] == "APPROVE"
