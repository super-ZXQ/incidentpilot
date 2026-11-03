"""Alembic owns the PostgreSQL schema; only throwaway SQLite files may self-build."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from incidentpilot.config import Settings, reset_settings_cache
from incidentpilot.persistence.session import (
    dispose_engine,
    get_engine,
    get_session_factory,
    init_schema,
    reset_db_state,
)


async def _public_tables() -> list[str]:
    async with get_engine().connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
            )
        )
    return sorted(row[0] for row in rows)


async def _use_database(monkeypatch, url: str) -> None:
    reset_db_state()
    monkeypatch.setenv("DATABASE_URL", url)
    reset_settings_cache()


@pytest.mark.asyncio
async def test_development_sqlite_bootstraps_the_schema(monkeypatch, tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"
    await _use_database(monkeypatch, url)
    try:
        await init_schema(Settings(database_url=url, environment="development"))
        async with get_session_factory()() as session:
            tables = (
                await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            ).scalars().all()
        assert "agent_runs" in tables
    finally:
        await dispose_engine()
        reset_db_state()
        reset_settings_cache()


@pytest.mark.asyncio
async def test_protected_environment_refuses_sqlite(monkeypatch, tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"
    await _use_database(monkeypatch, url)
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await init_schema(Settings(database_url=url, environment="production"))
        # Refusing must not leave a half-built schema behind.
        async with get_engine().connect() as connection:
            tables = (
                await connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            ).scalars().all()
        assert tables == []
    finally:
        await dispose_engine()
        reset_db_state()
        reset_settings_cache()


@pytest.mark.asyncio
async def test_migrated_postgres_starts_without_touching_the_schema(monkeypatch) -> None:
    """The accepting half of the gate.

    ``init_schema`` must verify and return: no create_all, no migration, no change
    to what Alembic built.
    """
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL must point at a migrated PostgreSQL test database")
    admin = url.replace("postgresql://", "postgresql+psycopg://")
    await _use_database(monkeypatch, admin)
    try:
        before = await _public_tables()
        assert "agent_runs" in before
        await init_schema(Settings(database_url=admin, environment="production"))
        assert await _public_tables() == before
    finally:
        await dispose_engine()
        reset_db_state()
        reset_settings_cache()


@pytest.mark.asyncio
async def test_unmigrated_postgres_is_not_created_from_the_models(monkeypatch) -> None:
    """The refusing half of the gate, on a PostgreSQL schema with no tables.

    ``create_all`` would quietly build whatever the ORM currently describes, turning a
    forgotten migration into a database that no migration matches. This asserts both
    halves: startup refuses, and it refuses without creating anything.
    """
    url = os.environ.get("POSTGRES_EMPTY_URL")
    if not url:
        pytest.skip("POSTGRES_EMPTY_URL must point at a PostgreSQL schema without agent_runs")
    admin = url.replace("postgresql://", "postgresql+psycopg://")
    await _use_database(monkeypatch, admin)
    try:
        assert "agent_runs" not in await _public_tables()
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await init_schema(Settings(database_url=admin, environment="development"))
        assert "agent_runs" not in await _public_tables()
    finally:
        await dispose_engine()
        reset_db_state()
        reset_settings_cache()
