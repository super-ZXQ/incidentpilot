# IncidentPilot V1 产品规格

## 1. Problem

线上服务出现异常时，Backend Engineer / SRE 通常没有一个统一视图能直接给出答案。他们需要在 Metrics、Logs、Git history、Application source code 和 Database 控制台之间来回切换，靠人工对齐时间线、拼凑 Hypothesis。整个过程高度依赖个人经验。

这个过程既慢又不稳定。MTTR 主要耗在证据收集和假设验证上，而不是修复动作本身。排障经验散落在聊天记录和个人笔记里，无法形成可复用、可审计的系统能力。

IncidentPilot 的目标不是聊天机器人，也不是单纯的日志总结工具，而是执行一段可验证的 Incident investigation workflow：接收结构化 Incident，通过真实 Tool Call 收集 Evidence，提出并验证 Hypothesis，在隔离 Sandbox 中生成 Patch，用自动化测试验证修复，在 Human Approval 后创建 Pull Request。

## 2. Target Users

V1 面向：

- **Backend Engineer** — 负责服务代码，对 Root Cause 判断和 Patch 正确性负责。
- **SRE / DevOps Engineer** — 负责可靠性信号、runbook，以及自动化与生产风险的边界。
- **小型研发团队** — 需要共享、可审计的调查轨迹，而不是某位专家的私人调试记录。

这些用户已经在运营后端服务，并具备 Metrics、Logs、源码仓库和只读运维数据的访问权限。他们希望减少调查时间，同时不放弃对高风险动作的控制。

## 3. Core Scenario

V1 只聚焦一个场景：

> 某后端服务出现错误率、延迟或业务指标异常。系统收到 Incident 后，Agent 自动调查并尝试生成经过测试验证的代码修复方案。

### 示例

一个有代表性的 Incident：

- 服务：`orders-api`
- 症状：`/orders` 的 P95 延迟从约 400ms 上升到约 2.8s；错误率从约 0.3% 上升到约 7%。
- 环境标签：`reference-production`
- 时间窗口：起始于已知的 `start_time`

`reference-production` 表示 Reference Environment 中模拟生产行为的环境标签，不代表真实企业生产系统。

Agent 至少需要调查：

- Metrics（延迟、错误率、相关服务健康度）
- Logs（错误类型、堆栈、与部署/时间窗口的关联）
- Git commits / diffs（受影响服务的近期变更）
- Application source code（被 Metrics 和 Logs 指向的路径）
- Read-only database information（查询延迟、锁/等待信号、在相关时的明显数据异常）

这个场景的成功条件不是“给出一段解释”，而是：基于 Evidence 定位可信 Root Cause，在隔离环境生成 Patch、运行测试，并产出等待 Human Approval 的 Pull Request 候选。

## 4. Reference Environment

IncidentPilot V1 **不连接真实企业生产环境**。

因为这是一个公开求职项目，所以 V1 使用一个：

> **可控、可复现、production-like 的故障实验环境**

来模拟真实后端线上系统。

Reference Environment 至少应包含：

- Backend API
- Database
- Logs
- Metrics
- Git repository / Git history
- Application source code
- Automated tests

后续系统可以向其中注入真实可复现的故障，例如：

- slow database query
- missing index
- bad code commit
- null exception
- dependency timeout
- connection pool exhaustion
- schema mismatch
- cache failure
- incorrect configuration

### 数据诚实性

Benchmark 中使用的数据必须来自 Agent 对这些故障环境的真实执行结果。不能把模拟实验结果包装成真实生产结果，也不能把演示脚本结果当作 Benchmark 数据。

## 5. V1 Input

Incident 对象至少需要包含：

| 字段 | 说明 |
| --- | --- |
| `incident_id` | 本次调查的稳定标识 |
| `title` | 简短可读摘要 |
| `service` | 主要被调查服务 |
| `severity` | 团队使用的严重级别 |
| `symptom` | 观测到的异常 |
| `start_time` | 症状首次出现时间 |
| `repository` | Agent 可检查并生成 Patch 的源码仓库 |
| `environment` | 目标环境标识 |

后续可以扩展字段，但 V1 不应依赖更复杂的多服务拓扑或 on-call 排班元数据。

## 6. V1 Workflow

V1 状态机如下：

```text
INCIDENT_RECEIVED
→ PLAN
→ COLLECT_EVIDENCE
→ FORM_HYPOTHESIS
→ VERIFY_HYPOTHESIS
→ ROOT_CAUSE_FOUND
→ CREATE_SANDBOX
→ GENERATE_PATCH
→ RUN_TESTS
```

### 测试失败循环

```text
RUN_TESTS
→ REFLECT
→ GENERATE_PATCH
→ RUN_TESTS
```

### 测试通过路径

```text
RUN_TESTS
→ WAIT_FOR_APPROVAL
→ CREATE_PULL_REQUEST
→ RESOLVED
```

### 合法终止状态

除了成功路径的 `RESOLVED`，V1 必须支持以下合法终止状态：

| 状态 | 含义 |
| --- | --- |
| `RESOLVED` | 调查完成，Patch 通过测试，并在 Human Approval 后创建 Pull Request |
| `INSUFFICIENT_EVIDENCE` | Evidence 不足以支撑可信 Root Cause |
| `NEEDS_HUMAN_INTERVENTION` | 超出 Agent 能力范围或达到执行限制 |
| `FAILED` | 出现不可恢复错误 |

### 设计原则

Agent **不允许为了完成任务而强行生成一个 Root Cause**。

- 如果 Evidence 不足，必须允许返回 `INSUFFICIENT_EVIDENCE`。
- 如果超出能力范围或预算，必须允许 `NEEDS_HUMAN_INTERVENTION`。
- 如果运行出现不可恢复错误，可以进入 `FAILED`。

核心循环是：

**Evidence → Hypothesis → Verification → Action → Validation**

每一个结论都必须能追溯到本次调查中收集的 Evidence。

## 7. Evidence Model

当前只做概念定义，不设计最终数据库表。

Evidence 是**结构化对象**，而不是 Prompt 里的一段自由文本。

每条 Evidence 至少需要在概念上能够追溯到：

- `evidence_id`
- `source`
- `source_type`
- `timestamp`
- `tool_call_id`
- `content` / `result`

### 绑定关系

- Hypothesis 必须引用 Evidence ID。
- Root Cause 必须引用 Evidence ID。

概念示例：

```text
Hypothesis:
  Recent commit introduced an N+1 query.

Supported by:
  EVID-012
  EVID-018
  EVID-021
```

### 核心原则

```text
Evidence
→ Hypothesis
→ Verification
→ Root Cause
```

没有 Evidence 支撑的 Root Cause **不属于合法调查结论**。

## 8. V1 Tool Boundary

Tool availability 是系统安全边界，而不是 Prompt 建议。Agent 不应该拥有超出允许列表的工具。

### 允许 Agent 自动执行

- `read_metrics`
- `read_logs`
- `inspect_git_history`
- `inspect_git_diff`
- `read_source_code`
- `query_database_readonly`
- `run_tests`
- `inspect_test_results`

### 只能在 Sandbox 中执行

- `modify_code`
- `execute_generated_patch`

### 必须 Human Approval

- `create_pull_request`

### V1 明确禁止

- production database write
- production deployment
- restart production service
- delete production resources
- arbitrary host command execution

## 9. Database Safety

`query_database_readonly` 必须遵循：

- **read-only** — 只允许读操作
- **least privilege** — 最小权限账号
- **query validation** — 查询前校验
- **timeout** — 强制超时
- **audit logging** — 完整审计日志

V1 不允许任何 Agent 对生产数据库执行：

- `INSERT`
- `UPDATE`
- `DELETE`
- `DROP`
- `ALTER`
- `TRUNCATE`

Reference Environment 中即便未来为了测试需要修改数据库，也**不能**通过生产查询工具执行。测试用的写操作必须走隔离环境中的专用路径，并与 `query_database_readonly` 严格分离。

## 10. Sandbox Boundary

当前只定义安全边界，不实现具体隔离技术。

未来 Sandbox 至少应遵循以下原则：

- **ephemeral workspace** — 临时工作区，用完即弃
- **repository copy only** — 仅包含仓库副本，不含无关系统资源
- **no production credentials** — 不携带生产凭据
- **no modification of developer host workspace** — 不修改开发者本机工作区
- **network disabled by default** — 默认禁网，或严格 allowlist
- **CPU limit** — CPU 使用上限
- **memory limit** — 内存使用上限
- **execution-time limit** — 执行时间上限
- **executed commands must be auditable** — 所有执行命令必须可审计

### 边界强调

Agent 生成的 Patch 必须先在隔离环境中验证。不能直接修改开发者本机仓库，更不能修改生产环境。

## 11. Technical Principles

V1 必须满足：

- **Agent workflow 可恢复** — 调查可在中断后从持久化状态恢复。
- **所有 Tool Call 有结构化输入输出** — 不以自由文本 shell 作为主接口。
- **Tool 有 timeout / retry / audit log** — 每次调用有超时、可安全重试、有记录。
- **每个结论必须绑定 Evidence** — 没有 Evidence 支撑的结论不是合法调查结果。
- **代码修改仅发生在 Sandbox** — Agent 不直接修改开发机工作区或生产系统。
- **高风险操作必须 Human-in-the-loop** — 尤其是创建 Pull Request。
- **每个 Incident 可以完整 Trace** — 计划步骤、Tool Call、Hypothesis、Patch、测试结果构成一条审计链。
- **系统支持自动 Evaluation** — 结果可由评测 harness 打分，而不只是演示。
- **所有重要状态变化都应该可以被审计** — 状态迁移、工具调用、审批动作均可追溯。

## 12. Execution Limits

V1 必须支持“停止条件”的设计理念。

Agent 不允许无限调查、无限 Tool Calling、无限修改代码。超过限制后必须**安全停止**，而不是继续循环。

系统未来应支持：

- maximum investigation steps
- maximum tool calls
- maximum patch attempts
- execution timeout
- model / token / cost budget

当前不虚构具体数字。具体阈值应在 Reference Environment 上通过真实运行校准后写入配置。

### 停止后的行为

达到限制后，Agent 应进入合法终止状态（通常是 `NEEDS_HUMAN_INTERVENTION`），并输出已有 Evidence、已验证内容、未完成步骤，而不是静默失败或无限重试。

## 13. Evaluation

后期必须建立 **Fault Injection Benchmark**，而不是只做演示。

Benchmark 必须使用可重复运行的 Fault Cases，不能只准备 2～3 个演示案例。

至少评估：

- Root Cause Accuracy
- Tool Selection Accuracy
- Incident Resolution Rate
- Patch Test Pass Rate
- Unsafe Action Rate
- Average Tool Calls
- Median Resolution Time
- Token / Model Cost

当前不填写任何最终指标。所有数据必须在真实 Benchmark 跑完后填写。

## 14. V1 Non-Goals

第一版明确不做：

- Multi-Agent
- A2A
- Kubernetes 自动运维
- 自动生产部署
- 自动修改生产数据库
- Fine-tuning
- 复杂前端
- 20+ 工具接入

不要为了“看起来高级”增加这些内容。V1 目标是把一个核心闭环真正做深，而不是把功能面铺宽。

## 15. Definition of Done

V1 只有在下列链路**真实可执行**时才算完成，而不是画出来：

```text
Incident
→ Agent 调查
→ 多个真实 Tool Call
→ 收集 Evidence
→ 形成并验证 Hypothesis
→ 找到 Root Cause
→ 创建 Sandbox
→ 生成 Patch
→ 自动运行真实测试
→ Human Approval
→ GitHub Pull Request
```

同时必须允许：

```text
Incident
→ Evidence 不足
→ INSUFFICIENT_EVIDENCE
```

或者：

```text
Incident
→ 达到执行限制
→ NEEDS_HUMAN_INTERVENTION
```

**“能安全承认不知道”比“编一个 Root Cause”更符合产品目标。**

跳过工具、伪造 Evidence、在 Sandbox 外改代码、或未经审批就创建 PR 的演示，都不满足 V1。

---

*本规格描述产品范围与工程约束，刻意不含营销话术，也不虚构任何性能数据、客户案例或生产结果。*
