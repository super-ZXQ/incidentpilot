"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "IncidentPilot"
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # Database (control plane)
    database_url: str = Field(
        default="postgresql+psycopg://incidentpilot:incidentpilot@127.0.0.1:5433/incidentpilot_app",
        description="Async SQLAlchemy URL for IncidentPilot control plane.",
    )
    # SQLite is supported only when tests explicitly override DATABASE_URL.

    # LLM
    llm_base_url: str = Field(default="http://127.0.0.1:9/v1")
    llm_provider: Literal["openai-compatible", "fake"] = Field(default="openai-compatible")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_enabled: bool = Field(default=False)

    # GitHub
    github_token: str = Field(default="")
    github_repo: str = Field(default="")
    github_integration_enabled: bool = Field(default=False)

    # Execution limits (server-side safety; not overridable by Incident payload)
    max_investigation_steps: int = Field(default=20)
    max_tool_calls: int = Field(default=40)
    max_patch_attempts: int = Field(default=3)
    run_timeout_seconds: int = Field(default=900)
    investigation_only: bool = Field(
        default=False,
        description="Evaluation-only stop after a verified root cause; API incidents cannot override it.",
    )

    # Durable PostgreSQL worker queue
    embedded_worker_enabled: bool = Field(default=False)
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    worker_poll_seconds: float = Field(default=0.5, gt=0, le=30)
    job_lease_seconds: int = Field(default=60, ge=10, le=3600)
    job_heartbeat_seconds: int = Field(default=15, ge=1, le=600)
    max_job_attempts: int = Field(default=3, ge=1, le=20)
    max_queued_runs: int = Field(default=100, ge=1, le=10000)

    # Reference environment
    reference_orders_api_url: str = Field(default="http://127.0.0.1:8001")
    reference_db_url: str = Field(default="")
    reference_logs_path: str = Field(default="./reference/orders_api/var/logs")
    reference_repo_path: str = Field(default="./reference/orders_api")

    # Observability
    otel_service_name: str = Field(default="incidentpilot")
    otel_console_exporter: bool = Field(default=False)
    otel_otlp_endpoint: str = Field(default="")

    # Sandbox
    sandbox_enabled: bool = Field(default=False)
    sandbox_image: str = Field(default="incidentpilot-sandbox:py312")
    sandbox_network: str = Field(default="none")

    # Test-only auto approval (must never default true in production)
    auto_approve_for_tests: bool = Field(default=False)

    # MCP server command for live stdio
    mcp_server_command: str = Field(default="")
    tool_backend: Literal["mcp", "fake"] = Field(
        default="mcp",
        description="Formal runtime uses MCP; fake is explicit for deterministic tests only.",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
