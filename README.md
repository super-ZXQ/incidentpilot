# IncidentPilot

**AI incident investigation and human-governed remediation agent for backend/SRE scenarios.**

IncidentPilot is not a chatbot or log summarizer. It executes a bounded investigation: a model chooses the next readonly observation, every claim is tied to tool-produced evidence, a hypothesis is independently verified, and a proposed fix is tested in isolation before a human can approve a pull request.

> Scope: a portfolio-grade V1 against a controlled, production-like `orders-api`. It is not a hardened multi-tenant sandbox and has not been deployed in enterprise production.

## Problem

Incident response requires engineers to correlate metrics, logs, source, recent commits, and database behavior. A plausible LLM answer is not enough: the investigation needs provenance, bounded execution, safe tools, reproducible tests, and human-controlled external side effects.

## 30-second workflow

```text
Incident → InvestigationPlan → model-selected Tool Call → Evidence
→ Hypothesis → independent verification Tool Call → CONFIRMED / REJECTED
→ Root Cause (or INSUFFICIENT_EVIDENCE) → Sandbox → Patch → pytest
→ bounded Reflection → immutable PatchArtifact → destroy Sandbox
→ LangGraph interrupt: WAIT_FOR_APPROVAL
→ APPROVE resumes graph → exact approved patch → Pull Request
```

The model decides the plan, next evidence source, hypothesis, verification action, conclusion, and—on the real-model path—the unified diff. The graph owns legal transitions and stop conditions. The Tool Gateway owns permissions, schemas, timeouts, retries, audit metadata, trace propagation, normalization, and budgets.

## Why this is an agent rather than a fixed pipeline

`AgentDecisionModel.next_action()` receives the Incident and current Evidence and selects one next tool or a stop/reasoning action. The graph does not prewire `logs → metrics → git → summary`. CI uses an explicit deterministic model policy for repeatability; the formal path uses an OpenAI-compatible provider with Pydantic-validated `InvestigationPlan`, `NextAction`, `HypothesisProposal`, `VerificationDecision`, `RootCauseConclusion`, and `PatchProposal` outputs.

## Evidence → Hypothesis → Verification

Every Evidence row contains an ID, run ID, source/type, tool-call ID, timestamp, raw result, summary, and SHA-256 content hash. Hypotheses may cite only IDs present in the run. Root Cause creation is rejected if it cites no evidence or unknown evidence. A separate verification tool call occurs before conclusion. Unsupported conclusions terminate as `INSUFFICIENT_EVIDENCE`.

## Architecture

```mermaid
flowchart LR
    U[Engineer] --> API[FastAPI Control Plane]
    API --> LG[LangGraph single-agent workflow]
    LG <--> PG[(PostgreSQL state + checkpoints)]
    LG --> GW[Tool Gateway]
    GW --> MC[MCP stdio client]
    MC --> MS[Ops Readonly MCP Server]
    MS --> REF[orders-api / PostgreSQL / logs / metrics / git]
    GW --> SB[Sandbox Manager]
    SB --> DC[Ephemeral Docker container]
    LG --> INT[interrupt: WAIT_FOR_APPROVAL]
    U -->|approve/reject| API
    API -->|Command resume| LG
    LG --> GH[GitHub Integration]
```

V1 deliberately uses one explicit Agent rather than Multi-Agent coordination. It does not need Redis, Celery, Kafka, Kubernetes, a vector database, or RAG.

## Tool Gateway and MCP

The Agent cannot call subprocesses, databases, GitHub, or arbitrary Python. Readonly external tools are transported over a real MCP stdio session:

- `read_metrics`
- `read_logs`
- `inspect_git_history`
- `inspect_git_diff`
- `read_source_code`
- `query_database_readonly`

The MCP server exposes no mutation, test, shell, deploy, or PR tool. `TOOL_BACKEND=mcp` is the default. `TOOL_BACKEND=fake` is explicit and reserved for deterministic tests; the live adapter never silently substitutes fake observations. Sandbox mutation is intentionally not MCP: it is a local security boundary, not an external readonly capability.

## Database safety

Readonly SQL uses `sqlglot` AST validation, single-statement enforcement, dangerous function/statement rejection, a 100-row cap, PostgreSQL read-only transactions, and a five-second statement timeout. Docker initialization creates a separate `reference_readonly` role with `default_transaction_read_only=on`. Fault setup never uses the Agent database tool.

## Sandbox and patch safety

The formal path uses an ephemeral Docker container with `network=none`, non-root UID/GID, `cap-drop ALL`, `no-new-privileges`, CPU/memory/PID limits, bounded execution, a run-specific copied workspace, no credentials, and no Docker socket. Docker mode fails closed when the daemon is unavailable. Local copied-workspace execution exists only when `SANDBOX_ENABLED=false` for development/tests.

Every patch path is resolved before apply. Absolute paths, traversal, `.git`, `.env`, credentials, secrets, and Docker socket paths are denied. The model cannot supply a command; it selects only a server-defined test profile.

## Human approval and GitHub

After tests pass, IncidentPilot stores an immutable PatchArtifact (`base_commit_sha`, exact diff, hash, test-run ID), destroys the Sandbox, and reaches a real LangGraph `interrupt()`. The API persists one decision per run and resumes the checkpoint with `Command(resume=...)`.

- `REJECT` → `NEEDS_HUMAN_INTERVENTION`
- `APPROVE` → resumed graph invokes GitHub Integration

GitHub is disabled by default. Mock mode performs no external mutation. Real mode requires explicit credentials, rejects stale/unversioned bases, applies the exact approved unified diff, creates Git blobs/tree/commit from that base, then opens a PR. Tokens are read only by the integration layer.

## Reference environment and benchmark

Eight YAML Fault Cases cover N+1 access, slow query, null exception, missing index, dependency timeout, schema mismatch, incorrect configuration, and bad query refactor. Each has structured Ground Truth (`fault_category`, `affected_component`, `root_cause`, `causal_facts`) used only by evaluation.

Reports are labeled and never mixed:

1. `workflow_fake` — deterministic workflow and safety behavior.
2. `real_llm_investigation` — real model reasoning; requires credentials and live services.
3. `full_remediation` — investigation through an approval-ready tested artifact.

Metrics include structured category/component/root-cause accuracy, evidence support, tool selection, investigation success, remediation readiness, full resolution, patch pass rate, unsafe action rate, tool count, and median duration. Real tiers run setup → reproduce → investigate → reset and fail rather than silently use fake data. Token/cost is omitted unless provider metadata is available.

## Quick start

Requirements: Python 3.12, `uv`, Docker with Compose.

```bash
uv sync --extra dev
docker compose build sandbox-image
docker compose up -d incidentpilot-postgres reference-postgres reference-orders-api prometheus
uv run alembic upgrade head
uv run uvicorn incidentpilot.api.app:app --factory --port 8000
```

Suggested environment:

```bash
DATABASE_URL=postgresql+psycopg://incidentpilot:incidentpilot@127.0.0.1:5433/incidentpilot_app
TOOL_BACKEND=mcp
REFERENCE_ORDERS_API_URL=http://127.0.0.1:8001
REFERENCE_DB_URL=postgresql://reference_readonly:reference_readonly@127.0.0.1:5434/orders
REFERENCE_REPO_PATH=./reference/orders_api
SANDBOX_ENABLED=true
SANDBOX_IMAGE=incidentpilot-sandbox:py312
GITHUB_INTEGRATION_ENABLED=false
```

For a real model also set `LLM_ENABLED=true`, `LLM_PROVIDER=openai-compatible`, `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`. Never commit `.env`.

## API

- `POST /v1/incidents` — persist Incident/AgentRun and return `202`
- `GET /v1/incidents/{incident_id}`
- `GET /v1/runs/{run_id}`
- `GET /v1/runs/{run_id}/evidence`
- `GET /v1/runs/{run_id}/tool-calls`
- `GET /v1/runs/{run_id}/patch-artifact`
- `POST /v1/runs/{run_id}/approval` — `APPROVE` or `REJECT`

## Tests

```bash
uv run ruff check .
uv run pytest -q
uv run pytest -q tests/test_mcp_live.py -s
uv run pytest -q -m docker -rs
```

CI runs Python 3.12, `uv`, Ruff, pytest, and PostgreSQL without LLM or GitHub credentials. Real LLM/GitHub checks are optional and reported separately.

## Repository structure

```text
src/incidentpilot/agent       LangGraph workflow and model decisions
src/incidentpilot/tools       Gateway, MCP adapter, SQL guard
src/incidentpilot/sandbox     isolated patch/test lifecycle
src/incidentpilot/persistence SQLAlchemy state and repositories
src/incidentpilot/eval        structured, tiered evaluation
ops_mcp/ops_readonly          readonly MCP stdio server
reference/orders_api          production-like reference service
benchmarks/fault_cases        eight withheld Ground Truth cases
alembic                       control-plane migrations
tests                         unit/API/workflow/MCP/sandbox/eval tests
docs                          V1 product and architecture ground truth
```

## Demo

Inject a fault, submit its Incident without fault ID/Ground Truth, watch model-selected Tool Calls and Evidence accumulate, inspect the verified Root Cause and PatchArtifact, then approve and show the resumed workflow's mock PR. Label FakeModel, real model, local workspace, Docker, mock GitHub, and real GitHub paths explicitly.

## Limitations

- V1 is single-instance, not a distributed worker platform.
- The Docker sandbox is controlled isolation, not a hostile multi-tenant boundary.
- Several reference faults are deterministic application-level injections rather than failures from an external managed service.
- Real LLM quality, GitHub mutation, Docker execution, and PostgreSQL recovery are claimed only when those dependencies were actually exercised.
- There is no automatic production deployment or production write access.

## Roadmap

High-value follow-ups: publish a real-model benchmark, strengthen semantic causal-fact scoring, add crash injection at every checkpoint, sign PatchArtifact provenance, and record a short demo.

## English summary

IncidentPilot demonstrates how to turn LLM reasoning into a governed software workflow: model-owned investigation decisions, evidence provenance, independent hypothesis verification, capability-based tools over MCP, bounded sandboxed remediation, durable human approval, exact-patch GitHub integration, and reproducible evaluation. It intentionally avoids claims of enterprise production maturity.
