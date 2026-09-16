# IncidentPilot V1 Implementation Decisions

本文档依据已冻结的：

- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`

记录 V1 实现层决策。本轮不包含 application code、目录、依赖锁文件或 SQL schema。

## 1. Runtime & Language

**Python 3.12**

原因：

- 与 FastAPI / LangGraph / MCP / SQLAlchemy 生态兼容
- 类型系统和 asyncio 能力成熟
- 不追求过新的实验性 Python 版本

代码未来采用 **src layout**：

```text
src/incidentpilot/
```

当前只记录决定，不创建目录。

## 2. Dependency Management

**uv + `pyproject.toml`**

不使用：

- `requirements.txt` 作为主要依赖源
- Poetry
- Pipenv

原因：dependency resolution、lockfile、virtual environment、command execution 统一由 uv 管理。

当前不创建 `pyproject.toml`。

## 3. Core Python Stack

### 运行时依赖

- FastAPI
- Pydantic v2
- pydantic-settings
- SQLAlchemy 2.x
- Alembic
- psycopg 3
- LangGraph
- MCP Python SDK
- httpx
- OpenTelemetry Python SDK

### 测试依赖

- pytest
- pytest-asyncio

当前不写最终版本号。版本将在第一次 project scaffold 时锁定。

## 4. LLM Abstraction

Agent Runtime 不允许在各个 LangGraph node 中散落 provider-specific API 调用。

设计一个概念上的 **LLMProvider / ModelClient abstraction**。

第一版实现目标：**OpenAI-compatible API adapter**。

配置概念：

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

开发时可以根据实际可用服务连接 OpenAI-compatible provider。

Benchmark 必须记录真实：

- provider
- model
- model version（如果可获取）

### 禁止

- 在代码里硬编码 API Key
- 在 workflow node 内直接创建 provider client
- 假装不同 provider 行为完全相同

Tool Calling / structured output capability 必须由 adapter 显式声明或验证。

当前不实现 adapter。

## 5. API Execution Model

V1 API **不等待**完整 Agent Run。

### 创建 Incident

`POST /v1/incidents`

返回：`HTTP 202 Accepted`

至少包含：

- `incident_id`
- `run_id`
- `status`

### 查询

- `GET /v1/incidents/{incident_id}`
- `GET /v1/runs/{run_id}`

后续至少还需要：

- `GET /v1/runs/{run_id}/evidence`
- `GET /v1/runs/{run_id}/tool-calls`
- `GET /v1/runs/{run_id}/patch-artifact`
- `POST /v1/runs/{run_id}/approval`

approval body 概念：

```text
decision = APPROVE | REJECT
```

### V1 不做

- WebSocket
- 复杂 SSE 实时 UI

V1 使用 **polling**。

原因：先保证 durable workflow，不为了 UI 增加实时通信复杂度。

## 6. Background Run Execution

V1 不使用：

- Celery
- Redis
- Kafka

使用：**single-process async RunExecutor**

概念：

```text
FastAPI 接收 Incident
→ 写入 PostgreSQL
→ RunExecutor 启动 LangGraph async run
→ API 立即返回 202
```

### 关键要求

Run 的 source of truth 是 **PostgreSQL + LangGraph checkpoint**，不是 Python `asyncio.Task` 本身。

如果进程终止：

- asyncio task 可以丢失
- durable run state 不允许丢失

应用启动时未来应能够：

```text
扫描 non-terminal AgentRun
→ 根据 checkpoint 判断可恢复状态
→ resume eligible runs
```

这是 V1 **single-instance execution model**，不声称支持 distributed workers / high availability。

如果未来需要 multi-instance、high concurrency、distributed queue，再通过 ADR 引入 Redis / Celery 或其他 durable queue。

## 7. PostgreSQL Implementation Boundary

使用：

- SQLAlchemy 2.x
- psycopg 3
- Alembic

业务数据 schema 与 LangGraph checkpoint namespace 必须逻辑分离。

概念示例：

```text
incidentpilot_app
incidentpilot_checkpoint
```

当前不决定最终表结构。

### 隔离要求

Reference Environment 的 PostgreSQL 与 IncidentPilot control-plane PostgreSQL 必须使用独立 database / container 或明确隔离实例。

V1 优先：**独立 PostgreSQL service/container**，不要通过同一业务 schema 混用。

## 8. MCP Transport

V1 Ops Readonly MCP Server 使用：**stdio transport**

原因：

- V1 MCP Server 与 IncidentPilot 在同一受控开发环境
- 无需提前增加 HTTP server / auth / deployment complexity
- 仍然真实使用 MCP protocol
- 后续可以替换为网络 transport

MCP 仅暴露：

- `read_metrics`
- `read_logs`
- `inspect_git_history`
- `inspect_git_diff`
- `read_source_code`
- `query_database_readonly`

**Mutation tools 不通过 MCP。**

## 9. GitHub Authentication

V1 使用：**GitHub Fine-grained Personal Access Token**

仅由 GitHub Integration layer 读取。

环境变量概念：

```text
GITHUB_TOKEN
```

### Token 不进入

- Agent prompt
- LLM context
- MCP Server
- Sandbox
- Reference Environment

权限遵循 least privilege。目标权限仅覆盖：

- repository metadata read
- contents / branch write
- pull request write

不在本文档中声称具体 GitHub permission 名称已经最终验证；实际实现时根据 GitHub API 要求确认。

未来产品化可以迁移到 GitHub App，但 V1 不增加这一复杂度。

## 10. Configuration

使用：

- pydantic-settings
- environment variables

本地开发未来使用 `.env`，但 `.env` 必须 gitignored。仓库只提供 `.env.example`。

Secrets 只能来自环境变量或 secret provider。

### Execution Limits 概念配置

```text
MAX_INVESTIGATION_STEPS
MAX_TOOL_CALLS
MAX_PATCH_ATTEMPTS
RUN_TIMEOUT_SECONDS
```

当前不设置最终数字，先通过 Fault Cases 调整。

V1 不允许用户通过 Incident 任意扩大安全限制。**server-side safety limits 优先。**

## 11. Fault Case Format

Fault Cases 使用 **YAML**。

原因：

- human readable
- Git-friendly
- easy benchmark review

### 限制

Fault Case YAML **不允许**包含 arbitrary shell command。

使用注册式 Fault Injector：

```yaml
fault_type: missing_index
params:
  ...
```

Fault Injector 根据 `fault_type` 调用代码中预注册的 handler。

### 概念字段

- `id`
- `title`
- `category`
- `fault_type`
- `params`
- `incident`
- `ground_truth`
- `expected_evidence_sources`
- `expected_fix_behavior`

Ground Truth 至少定义：

- root cause
- affected component
- expected fault category

当前不创建 YAML 文件。

## 12. Reference Environment

V1 主 Reference Service：**orders-api**

建议技术：

- FastAPI
- PostgreSQL
- structured JSON logging
- Prometheus metrics
- pytest

Reference Environment 与 IncidentPilot API 是两个不同职责的应用。

### orders-api 用途

- 产生真实 requests
- 产生 Metrics
- 产生 Logs
- 访问 reference database
- 承载 Fault Injection
- 提供 automated tests

### V1 暂时不引入

- Loki
- Elasticsearch

Logs 先采用 structured JSON logs，由 readonly MCP tool 读取受控日志源。

Metrics 使用：

```text
Prometheus-compatible metrics endpoint
+
Prometheus
```

这样 `read_metrics` 后续可以执行真实 metrics query。

## 13. Sandbox Implementation Decision

V1 Sandbox 使用：**Docker container**

默认：**network disabled**

### 目标安全参数（概念）

- non-root user
- CPU limit
- memory limit
- PID limit
- execution timeout
- capabilities dropped
- no-new-privileges
- ephemeral container

### workspace

每个 run 创建独立 temporary repository snapshot。

禁止直接 mount developer working repository。

允许 mount 的 writable 路径只应是 run-specific temporary workspace。

### Test execution

Agent 不能传入任意 shell command。Test execution 使用 **registered Test Profile**。

V1 Reference Service 默认 Test Profile：**pytest**

具体命令由 server-side policy 定义，不是 LLM 自由生成 shell。

### 安全边界声明

V1 Sandbox 提供受控的 process / filesystem / network / resource isolation。

V1 **不声称**是 hardened hostile multi-tenant security sandbox。

Container 内不能访问 Docker socket。

## 14. Observability

使用 **OpenTelemetry SDK**。

每个 AgentRun 拥有 `trace_id`。

Trace context 跨越：

- FastAPI
- LangGraph
- Tool Gateway
- MCP
- Sandbox
- GitHub Integration

开发阶段至少支持 **console exporter**，并预留 **OTLP exporter**。

当前不绑定：

- Jaeger
- Tempo
- Datadog
- 其他 vendor backend

Benchmark 中使用的 token / model metadata 从 LLM adapter 收集。

## 15. Initial Implementation Strategy

真正开始写代码后，不要一次性实现完整系统。

### 实现顺序

| Phase | 内容 |
| --- | --- |
| Phase 1 | Core models + config + PostgreSQL + minimal API |
| Phase 2 | LangGraph skeleton，先使用 deterministic fake tools |
| Phase 3 | Reference orders-api + first Fault Case |
| Phase 4 | Tool Gateway + readonly MCP |
| Phase 5 | Evidence / Hypothesis / Root Cause real workflow |
| Phase 6 | Docker Sandbox + PatchArtifact + tests |
| Phase 7 | Human Approval + GitHub PR |
| Phase 8 | OpenTelemetry + Evaluation Harness |

### Milestone 定义

第一个 milestone **不是**“所有组件启动”。

第一个业务 milestone：

```text
一个 Fault Case
→ Incident
→ Agent investigation
→ real Evidence
→ correct Root Cause
```

第二个 milestone：

```text
Root Cause
→ Patch
→ Tests
→ Approval
→ Pull Request
```

## 16. Explicit Non-Decisions

当前仍然不决定：

- production deployment platform
- Kubernetes
- Redis
- Celery
- distributed workers
- observability backend
- frontend framework
- Multi-Agent
- A2A
- Fine-tuning

---

*本实现决策文档服务于 V1 产品与架构约束，不提前创建脚手架，不绑定超出必要范围的运行时组件。*
