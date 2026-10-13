"""Ops Readonly MCP Server.

Exposes read-only external-system tools over MCP stdio transport.
Mutation tools must never be registered here.
"""

from __future__ import annotations

import json
import os
import re
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

REFERENCE_REPO = Path(os.environ.get("REFERENCE_REPO_PATH", "./reference/orders_api"))
REFERENCE_LOGS = Path(os.environ.get("REFERENCE_LOGS_PATH", "./reference/orders_api/var/logs"))
REFERENCE_ORDERS_API_URL = os.environ.get("REFERENCE_ORDERS_API_URL", "http://127.0.0.1:8001")

mcp = FastMCP("ops-readonly")


def safe_tool(function):
    """Direct MCP callers receive the same bounded/redacted output as the Agent."""
    @wraps(function)
    def call(*args, **kwargs):
        from incidentpilot.tools.data import redact

        if len(json.dumps([args, kwargs], default=str)) > 16_000:
            return {"error": "tool input limit exceeded"}
        result = function(*args, **kwargs)
        if len(json.dumps(result, default=str).encode("utf-8")) > 64_000:
            return {"error": "tool output limit exceeded"}
        return redact(result)
    return call


def _resolve_reference_repo(repository: str) -> Path:
    configured = REFERENCE_REPO.resolve()
    candidate = Path(repository).resolve() if repository else configured
    if candidate != configured:
        raise ValueError("repository is outside the configured reference repository")
    return candidate


def _read_json_file(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


@mcp.tool()
@safe_tool
def read_metrics(service: str = "orders-api", window: str = "incident") -> dict[str, Any]:
    """Read metrics from the reference environment."""
    http_error = "metrics source unavailable"
    try:
        import httpx

        resp = httpx.get(f"{REFERENCE_ORDERS_API_URL}/metrics/json", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            data["source"] = "reference_orders_api_http"
            data["window"] = window
            return data
    except Exception as exc:
        http_error = str(exc)
    cached = _read_json_file(REFERENCE_LOGS / "metrics.json", {})
    return {
        "service": service,
        "window": window,
        "source": "reference_logs_cache",
        **cached,
        "error": http_error,  # Unversioned cached telemetry cannot establish current evidence.
    }


@mcp.tool()
@safe_tool
def read_logs(service: str = "orders-api", window: str = "incident", limit: int = 50) -> dict[str, Any]:
    """Read structured JSON logs from the reference environment."""
    if not 1 <= limit <= 50:
        return {"error": "limit must be between 1 and 50"}
    log_file = REFERENCE_LOGS / "orders.jsonl"
    lines: list[dict[str, Any]] = []
    if log_file.exists():
        with log_file.open("rb") as stream:
            stream.seek(max(0, log_file.stat().st_size - 128_000))
            if stream.tell():
                stream.readline()  # Drop a partial first line.
            raw_lines = stream.read(128_000).decode("utf-8", "replace").splitlines()
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not lines:
        return {
            "service": service,
            "window": window,
            "count": 0,
            "error_count": 0,
            "events": [],
            "error": "structured log source unavailable",
            "source": str(log_file),
        }
    incident_start = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if lines[index].get("path") == "/admin/fault"
            and lines[index].get("method") == "POST"
        ),
        max(0, len(lines) - limit),
    )
    selected = lines[incident_start:][-limit:]
    errors = [
        item
        for item in selected
        if item.get("error")
        or item.get("error_type")
        or int(item.get("status", 0) or 0) >= 500
    ]
    return {
        "service": service,
        "window": window,
        "count": len(selected),
        "error_count": len(errors),
        "events": selected,
    }


@mcp.tool()
@safe_tool
def inspect_git_history(repository: str = "", limit: int = 10) -> dict[str, Any]:
    """Inspect git history in the reference repository (read-only)."""
    import subprocess

    try:
        repo_path = _resolve_reference_repo(repository)
    except ValueError as exc:
        return {"error": str(exc), "repository": repository}
    limit = max(1, min(int(limit), 50))
    if not repo_path.exists():
        return {"repository": str(repo_path), "commits": [], "source": "missing_repo"}
    try:
        out = subprocess.check_output(
            ["git", "log", f"-n{limit}", "--pretty=format:%H|%s|%an|%ci"],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        commits = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append(
                    {
                        "sha": parts[0],
                        "message": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                    }
                )
        return {"repository": str(repo_path), "commits": commits}
    except Exception as exc:
        return {"repository": str(repo_path), "commits": [], "error": str(exc)}


@mcp.tool()
@safe_tool
def inspect_git_diff(repository: str = "", commit_sha: str = "HEAD") -> dict[str, Any]:
    """Inspect a commit diff (read-only)."""
    import subprocess

    try:
        repo_path = _resolve_reference_repo(repository)
    except ValueError as exc:
        return {"error": str(exc), "repository": repository}
    if commit_sha != "HEAD" and re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None:
        return {"error": "invalid commit sha", "commit_sha": commit_sha}
    if not repo_path.exists():
        return {"repository": str(repo_path), "diff": "", "error": "repo missing"}
    try:
        diff = subprocess.check_output(
            ["git", "show", "--stat", "--patch", commit_sha],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return {"repository": str(repo_path), "commit_sha": commit_sha, "diff": diff[:20000]}
    except Exception as exc:
        return {"repository": str(repo_path), "commit_sha": commit_sha, "error": str(exc)}


@mcp.tool()
@safe_tool
def read_source_code(repository: str = "", path: str = "app.py") -> dict[str, Any]:
    """Read a source file from the reference repository (read-only)."""
    try:
        repo_path = _resolve_reference_repo(repository)
    except ValueError as exc:
        return {"error": str(exc), "repository": repository, "path": path}
    from incidentpilot.sandbox.manager import sensitive_path

    if sensitive_path(path):
        return {"error": "sensitive source path forbidden", "path": path}
    lowered_parts = {part.lower() for part in Path(path).parts}
    if any(
        part == ".env"
        or part.startswith(".env.")
        or "credential" in part
        or "secret" in part
        or part == ".git"
        for part in lowered_parts
    ):
        return {"error": "sensitive source path forbidden", "path": path}
    from incidentpilot.sandbox.manager import validate_patch_path

    try:
        file_path = validate_patch_path(repo_path, path)
    except ValueError as exc:
        return {"error": str(exc)}
    # Prevent path escape
    try:
        resolved = file_path.resolve()
        repo_resolved = repo_path.resolve()
        if not resolved.is_relative_to(repo_resolved):
            return {"error": "path outside repository", "path": path}
    except Exception as exc:
        return {"error": str(exc), "path": path}
    if not file_path.exists():
        return {"error": "file not found", "path": str(file_path)}
    if file_path.is_symlink() or not file_path.is_file() or file_path.stat().st_size > 50_000:
        return {"error": "source must be a bounded regular file"}
    content = file_path.read_text(encoding="utf-8", errors="strict")
    return {"path": str(file_path), "content": content}


@mcp.tool()
@safe_tool
def query_database_readonly(query: str = "SELECT 1") -> dict[str, Any]:
    """Run a read-only SQL query against the reference database.

    Write statements are rejected via sqlglot AST + keyword defense.
    """
    try:
        from incidentpilot.tools.sql_guard import validate_readonly_sql

        validated = validate_readonly_sql(query)
        query = validated["query"]
    except Exception as exc:
        return {"error": str(exc), "query": query, "validated": False}

    import os

    ref_db_url = os.environ.get("REFERENCE_DB_URL", "")
    if ref_db_url.startswith("postgresql"):
        import psycopg

        try:
            with psycopg.connect(ref_db_url, connect_timeout=3) as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                    role = cur.fetchone()
                    cur.execute("SHOW default_transaction_read_only")
                    if role is None or any(role) or cur.fetchone()[0] != "on":
                        raise PermissionError("dedicated readonly database role required")
                    cur.execute("SELECT has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p')")
                    if any(row[0] for row in cur.fetchall()):
                        raise PermissionError("database role has write privileges")
                    cur.execute("SET statement_timeout = '5s'")
                    cur.execute("SET LOCAL lock_timeout = '1s'")
                    cur.execute("SET LOCAL search_path = pg_catalog, public")
                    cur.execute("SET LOCAL work_mem = '4MB'")
                    cur.execute(query)
                    if cur.description is None:
                        return {"query": query, "rows": [], "source": "reference_postgres"}
                    cols = [c.name for c in cur.description]
                    rows = [dict(zip(cols, row, strict=False)) for row in cur.fetchmany(100)]
                    return {"query": query, "rows": rows, "source": "reference_postgres"}
        except Exception as exc:
            return {"error": str(exc), "query": query, "source": "reference_postgres"}

    return {
        "error": "REFERENCE_DB_URL must point to the readonly PostgreSQL account",
        "query": query,
        "rows": [],
        "source": "unavailable",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
