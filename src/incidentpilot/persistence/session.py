"""Async SQLAlchemy engine and session helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from incidentpilot.config import Settings, get_settings
from incidentpilot.models.db import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Only these may bootstrap a schema from the ORM models.
_TEST_ENVIRONMENTS = frozenset({"development", "test"})


def get_engine(url: str | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        database_url = url or settings.database_url
        kwargs: dict = {"echo": False}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(database_url, **kwargs)
    return _engine


def get_session_factory(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(url),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db(url: str | None = None) -> None:
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_schema(settings: Settings) -> None:
    """Prepare the control plane before anything reads or writes its tables.

    Alembic owns the schema. `create_all` reads the current ORM models, so on a real
    database it can build a layout no migration describes and later migrations then
    fail or, worse, silently disagree with what the code expects. Only the throwaway
    SQLite files used by development and tests are bootstrapped here.
    """
    if settings.database_url.startswith("sqlite"):
        if settings.environment not in _TEST_ENVIRONMENTS:
            raise RuntimeError(
                f"sqlite is not a supported control plane in {settings.environment!r}; "
                "point DATABASE_URL at PostgreSQL and run `alembic upgrade head`"
            )
        await init_db()
        return

    engine = get_engine()
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM agent_runs LIMIT 0"))
    except DBAPIError as exc:
        if not _missing_table(exc):
            raise
        raise RuntimeError(
            f"database has no IncidentPilot schema ({settings.environment!r}); "
            "run `alembic upgrade head` before starting"
        ) from exc


def _missing_table(exc: DBAPIError) -> bool:
    """True only when the probe failed because the table is absent.

    A connection drop or a permission denial must surface as itself: swallowing
    those as "no schema" would point operators at the wrong fix.
    """
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if isinstance(state, str):
        return state == "42P01"  # undefined_table
    return isinstance(exc, ProgrammingError) and "does not exist" in str(exc.orig)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset_db_state() -> None:
    """Sync reset for tests that switch database URLs."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
