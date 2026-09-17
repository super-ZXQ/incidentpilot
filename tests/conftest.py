"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.config import reset_settings_cache
from incidentpilot.persistence.session import (
    dispose_engine,
    get_session_factory,
    init_db,
    reset_db_state,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_asyncio_loop_factories(config, item):
    del config, item
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest_asyncio.fixture
async def db_session(tmp_path) -> AsyncIterator[AsyncSession]:
    reset_db_state()
    reset_settings_cache()
    db_path = tmp_path / "test_incidentpilot.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    await init_db(url)
    factory = get_session_factory()
    async with factory() as session:
        yield session
    await dispose_engine()
    reset_db_state()
    reset_settings_cache()


@pytest_asyncio.fixture
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    reset_db_state()
    reset_settings_cache()
    db_path = tmp_path / "test_api.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    import os

    os.environ["DATABASE_URL"] = url
    os.environ["TOOL_BACKEND"] = "fake"
    reset_settings_cache()

    from incidentpilot.api.app import create_app

    app = create_app()
    await init_db(url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    from incidentpilot.agent.executor import reset_run_executor

    reset_run_executor()
    await dispose_engine()
    reset_db_state()
    reset_settings_cache()
