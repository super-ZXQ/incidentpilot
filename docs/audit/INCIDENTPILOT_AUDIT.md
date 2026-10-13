# IncidentPilot 源码审计（修复前）

交付导航：[修复报告](INCIDENTPILOT_FIX_REPORT.md) · [安全报告](INCIDENTPILOT_SECURITY_REPORT.md) · [评测报告](INCIDENTPILOT_EVALUATION.md)。机器可读日志与截图位于 `artifacts/`。

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
| A13 / 012–013 | Medium / CONFIRMED | 只有八个 YAML；无 A/B/C 统一对照；Fake patch 固定移除两个延迟分支，不能证明 null/config 修复。 | 演示能力被高估。保留历史八例；扩展独立场景与统一实验记录；失败如实保留。 |
| A14 / 014–015 | Medium / CONFIRMED | 无调查演示页面；现有报告是历史运行，不能作为本次验收。 | 增加轻量同源页面，输出本次机器可读测试/评测和限制。 |
| A15 | Low / NOT_APPLICABLE | 没有 Redis/Celery/Multi-Agent/RAG，当前任务无需引入。 | 保留单 Agent、PostgreSQL、MCP stdio 架构。 |
| A16 / 013 | High / CONFIRMED | `run_tests_node_r` 使用 `pytest_regression`，该测试先 reset_fault；即使 null/config 未修复也可通过。 | 新增独立 fault-active oracle，并在评测/页面标明 test_scope，禁止把健康回归解释为真正解决故障。 |

## 基线与环境

Windows / PowerShell，Python 与依赖由 `uv` 锁文件提供。`uv run ruff check .` 已通过。`uv run pytest -q`：75 passed、4 skipped、1 warning，103.88 秒；跳过为 PostgreSQL 三项（未设置 POSTGRES_TEST_URL）与真实模型一项（无 API Key）。警告来自 Starlette 的 AnyIO deprecated alias。Docker daemon 实测版本 29.5.2；镜像、专用 PostgreSQL、MCP 和真实模型凭据需分别验证。不会打印凭据，不对真实 GitHub 写入。

## 实施顺序与验收

先认证/审批产物/沙箱，再状态机/Worker fencing/外部恢复，再 MCP/SQL/上下文/证据/评测，最后演示和交付。每阶段针对以上复现增加有效回归，再执行全量检查并独立 commit。外部实验不满足条件标 NOT_RUN；Mock 和 Fake 必须标明，不等同于真实修复。

## 最终审计状态

| 发现 | 最终证据状态 | 修复与验证状态 |
|---|---|---|
| A01 | CONFIRMED | 认证、角色、资源 owner、可信 actor 已修复；新增数据 API 同样受保护。 |
| A02 | CONFIRMED | 默认 Docker、受保护环境禁止 host、敏感文件/链接过滤；真实 Docker 行为及取消回收通过。不是通用 DLP 或多租户内核隔离。 |
| A03 | CONFIRMED | Git 解析、类型拒绝、实际路径比较；新增/删除及异常路径测试通过。 |
| A04 | CONFIRMED | 产物/hash/base/test/run/actor/time/decision 绑定，ORM 不可变与恢复重验；篡改和审批竞态测试通过。 |
| A05 | CONFIRMED | 可取消进程、独立 Docker 回收、异步 GitHub HTTP；62 秒心跳及取消探针通过。 |
| A06 | CONFIRMED | 状态表、终止路由、步数及调用预算恢复，停止后无补丁测试通过。 |
| A07 | CONFIRMED | owner/attempt/有效租约 fencing、DB 条件写；同 ID 旧 attempt、接管、DB 心跳故障和最终写冲突均有回归。 |
| A08 | CONFIRMED | Mock HTTP 复现 commit/ref/PR 后崩溃窗口并修复；真实 GitHub 环境 NOT_RUN。 |
| A09 | CONFIRMED | 工具错误/JSON/空/部分/大小/重试预算修复；真实 stdio MCP 通过。 |
| A10 | CONFIRMED | 参考 schema、函数/CTE 默认拒绝、LIMIT/timeout、真实只读角色与高权限角色拒绝测试通过。 |
| A11 | SUSPECTED | 结构化上下文、脱敏、边界、预算与后端权限已落实；真实模型语义注入效果 NOT_RUN，未把静态风险说成成功利用。 |
| A12 | CONFIRMED | 证据归属/调用/hash/去重/独立观测及引用重验；引用合法率与因果支持分开。后者未独立裁决。 |
| A13 | CONFIRMED | 原八例 hash 校验保留，新增十二例；A/B/C 离线各二十例；真实模型实验 NOT_RUN。 |
| A14 | CONFIRMED | 同源轻量 UI、完整报告、机器可读日志和 CI 更新；新 CI 配置尚未在远端 GitHub Actions 执行。 |
| A15 | NOT_APPLICABLE | 保留既有技术栈，未引入额外队列、RAG 或多 Agent 运行架构。 |
| A16 | CONFIRMED | fault-active oracle 识别 null/config 未修复；不再把健康回归当故障解决。 |

无记录被标 NOT_REPRODUCED，因为本次没有把未复现的攻击实验冒充已执行。新增审查还修复了多次 PatchAttempt 累积内容与最后 Diff 不一致、各 TestRun 误用最终 exit code，以及 SQLite 内存中断丢失时无意义重试；证据见修复报告及专门回归。

## 最终验收摘要

`ruff check .` 通过；完整 pytest：**204 passed、1 skipped、14 warnings，198.55 秒**。跳过仅真实模型 Key 缺失；警告为 Starlette/AnyIO 弃用和明确的非隔离开发测试提示。单独真实 MCP：1 passed；Docker：5 passed。完整日志、JUnit、依赖和镜像信息位于 `artifacts/`。没有真实 GitHub 写入或实际真人审批实验，三类离线候选有两个失败，均保留。
