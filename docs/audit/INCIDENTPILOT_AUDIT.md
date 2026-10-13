# IncidentPilot 源码审计（修复前）

审计日期：2026-10-13。基线：`107d28ad78ad53c8b3e93f44b6768c9a0c83f507`；分支：`codex/security-audit-20261013`。工作树起始为空。审计覆盖 API、ORM、队列、Worker、执行器、LangGraph、审批恢复、GitHub、沙箱、Gateway、MCP server/client、SQL guard、模型决策/provider、评测及现有测试和部署文档。

状态定义：CONFIRMED 为源码及执行/回归确认；SUSPECTED 为仍需实验的风险；NOT_REPRODUCED 为实验未复现；NOT_APPLICABLE 为当前范围不适用。静态发现不等于已成功利用。

## 修复前发现

| ID / 任务 | 严重性 / 状态 | 源码证据与复现 | 影响 / 修复计划 |
|---|---|---|---|
| A01 / 001 | Critical / CONFIRMED | `api/app.py` 所有 `/v1` 只有 session 依赖，无身份认证；既有 API 测试不带凭据即可创建和审批。`submit_approval` 把 `body.actor` 传入服务。 | 任意调用者可读取调查、发起测试和伪造审批。增加服务端 bearer 身份/角色、资源所有者、可信 actor。 |
| A02 / 002 | Critical / CONFIRMED | `SandboxManager.run_tests` 仅根据 `sandbox_enabled` 选择本机；默认 False，未检查 environment。`copytree` 仅排除 `.git/__pycache__/*.db/var`。 | 受保护环境可执行不可信宿主代码，敏感文件和符号链接可复制。强制 Docker、复制过滤及安全挂载。 |
| A03 / 003 | High / CONFIRMED | `validate_patch_diff` 仅扫描 `---/+++`；执行含 `old mode/new mode` 的探针仍通过。GitHub 独立从 `+++` 提取路径，遗漏删除和 rename。 | 审批范围与真正 Git 变更不一致。严格支持普通文本修改/新建/删除，显式拒绝其他形式，并核对实际文件变化。 |
| A04 / 003 | Critical / CONFIRMED | `ApprovalService.decide` 不填 `patch_artifact_id`；恢复取 run 的第一个 artifact，未校验 hash/TestRun/审批绑定。 | 测试后篡改或切换产物可能被提交。绑定 ID/hash/base/test 并在恢复时重验。 |
| A05 / 004 | High / CONFIRMED | 活跃 `run_tests_node_r` 直接调用同步 `manager.run_tests`；GitHub 恢复直接调用同步 HTTP client。 | 单事件循环心跳/取消被阻塞。使用受控异步子进程和异步 HTTP，超时/取消清理容器与进程树。 |
| A06 / 005 | High / CONFIRMED | `route_after_collect` 只截断 INSUFFICIENT_EVIDENCE；`needs_human` 和工具预算耗尽设置的状态仍可流向 hypothesis/sandbox。 | 人工停止后继续自动修复。路由统一检查 RUNNING，显式合法转换表。 |
| A07 / 006 | Critical / CONFIRMED | `mark_status` 依赖 ORM 旧快照并直接 commit；无 worker/attempt/lease 条件。`renew_lease` 不检查 lease 尚未到期。 | 旧 Worker 可覆盖新状态并复活过期租约。attempt fencing 与原子条件写。 |
| A08 / 007 | High / SUSPECTED | GitHub 已有远端 PR 查询，但分支已创建且 PR 未创建时 `POST refs` 再次失败；未核对已有 PR 产物。 | crash/timeout 恢复不完整。Mock HTTP 注入分支/PR 成功后响应丢失，恢复前核对远端 commit/tree。 |
| A09 / 008 | High / CONFIRMED | MCP client 忽略 `isError`；非 JSON/空响应作为成功 dict；Gateway 接受 `{error: ...}`。 | 失败内容变成 Evidence。统一拒绝协议/业务错误、空/部分结果，限制输入输出。 |
| A10 / 009 | High / CONFIRMED | `validate_readonly_sql('SELECT * FROM pg_catalog.pg_authid')` 实测通过。server 设置只读事务但不验证 role。 | 系统表/危险函数/复杂查询范围过宽。基于 reference schema 的保守表/字段/函数规则、只读角色核验和 LIMIT。 |
| A11 / 010 | High / SUSPECTED | decision 原样 json.dumps 事件、证据和源码；无总上下文预算/脱敏/明确数据边界；usage 未累计。 | 注入、泄密、上下文膨胀风险。权限以服务端为准；上下文边界、预算、脱敏和对抗测试。真实模型抗注入效果需真实实验。 |
| A12 / 011–012 | High / CONFIRMED | scoring 的 evidence_bound 只是 `bool(ids)`；verification 工具失败后仍调用 verify，Fake 可用旧证据判支持。 | 引用存在不等于因果支持；独立验证可能虚假成功。验证当前 run/成功工具/独立内容，分开报告各指标。 |
| A13 / 012–013 | Medium / CONFIRMED | 只有八个 YAML；无 A/B/C 统一对照；Fake patch 固定移除两个延迟分支，不能证明 null/config 修复。 | 作品集能力被高估。保留历史八例；扩展独立场景与统一实验记录；失败如实保留。 |
| A14 / 014–015 | Medium / CONFIRMED | 无调查演示页面；现有报告是历史运行，不能作为本次验收。 | 增加轻量同源页面，输出本次机器可读测试/评测和限制。 |
| A15 | Low / NOT_APPLICABLE | 没有 Redis/Celery/Multi-Agent/RAG，当前任务无需引入。 | 保留单 Agent、PostgreSQL、MCP stdio 架构。 |
| A16 / 013 | High / CONFIRMED | `run_tests_node_r` 使用 `pytest_regression`，该测试先 reset_fault；即使 null/config 未修复也可通过。 | 新增独立 fault-active oracle，并在评测/页面标明 test_scope，禁止把健康回归解释为真正解决故障。 |

## 基线与环境

Windows / PowerShell，Python 与依赖由 `uv` 锁文件提供。`uv run ruff check .` 已通过。`uv run pytest -q`：75 passed、4 skipped、1 warning，103.88 秒；跳过为 PostgreSQL 三项（未设置 POSTGRES_TEST_URL）与真实模型一项（无 API Key）。警告来自 Starlette 的 AnyIO deprecated alias。Docker daemon 实测版本 29.5.2；镜像、专用 PostgreSQL、MCP 和真实模型凭据需分别验证。不会打印凭据，不对真实 GitHub 写入。

## 实施顺序与验收

先认证/审批产物/沙箱，再状态机/Worker fencing/外部恢复，再 MCP/SQL/上下文/证据/评测，最后演示和交付。每阶段针对以上复现增加有效回归，再执行全量检查并独立 commit。外部实验不满足条件标 NOT_RUN；Mock 和 Fake 必须标明，不等同于真实修复。
