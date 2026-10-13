# 修复与验证记录

基线与发现见 [审计报告](INCIDENTPILOT_AUDIT.md)。所有提交保留单 Agent/LangGraph、MCP stdio、PostgreSQL、Docker 和 Human Approval 架构。

## P0 安全边界阶段

- API 增加服务端预配置 Bearer 身份：initiator（仅本人资源）、reviewer（组织内只读）、approver（组织内审批）、admin。未配置凭据默认拒绝；历史无 owner 资源仅 admin 可访问。健康检查公开，metrics 需审阅/审批/admin 身份。请求 actor 仅兼容接收，绝不作为身份来源。
- Incident owner 与 TestRun patch hash/base/mode 增加 `0005_security_bindings` 迁移。审批绑定明确 artifact ID/hash/base/test；恢复重新校验所属 run、内容 hash 与真实成功测试记录。
- 沙箱限制 production/未知环境必须 Docker，默认无网络、非 root、cap-drop、no-new-privileges、只读根和工作目录、tmpfs、CPU/memory/PIDs 上限。敏感文件名、私钥、环境文件及符号链接不复制。
- Git 负责解析 unified diff；显式拒绝 rename/copy/binary/symlink/mode change，支持普通文本修改/新建/删除，核对实际内容变化路径；禁止 Git/Windows 特殊路径。JSON replace 仅保留供显式 Fake 测试。
- 测试节点改为受控可取消执行；Windows Selector loop 通过专用线程管理 subprocess，取消时终止进程树并等待线程结束；Linux 使用 asyncio process group；Docker 单独按唯一容器名强制回收。

验证：`uv run ruff check .` 通过；`uv run pytest -q tests/test_audit_security.py tests/test_phase6_sandbox.py tests/test_phase7_approval.py tests/test_approval_idempotency.py`：**47 passed，5 warnings，17.03 秒**。警告为预期的非隔离开发测试提示。初版新增测试有两处用 GET 请求 POST-only 路由及 helper 被 pytest 收集的问题，已修正测试构造并全部重跑。

后续仍需：Worker fencing、GitHub 实际内容及 crash recovery、MCP/SQL/证据、Docker 行为探针和最终全量验收。当前阶段不宣称这些已完成。

## P0 状态机、Worker 与 GitHub 恢复阶段

- 显式 RUN_TRANSITIONS；收集后的所有非 RUNNING 状态停止路由，sandbox/test 失败不再继续生成或导出；checkpoint 恢复重建调查步数及调用预算。
- ContextVar 携带 ClaimedRun（run、worker、attempt）；状态更新、调查产物和 PatchArtifact 写入前在同一数据库事务执行条件 UPDATE 并检查 rowcount。旧 attempt 即使 worker ID 相同也被拒绝。heartbeat 不可续租已到期的租约。PostgreSQL advisory session lock 串行化同一 run 的 graph/checkpoint 执行。
- 审批恢复纳入 Worker 心跳与重试；GitHub 用 AsyncClient，写前再次 fencing。稳定分支名含 run/hash；远端分支必须具有审批 tree 和 base parent；已有 PR 必须匹配 head。429/5xx/超时交给队列重试并先重新查询远端，绝不覆盖现有分支。
- GitHub 从严格解析的路径获取 base blobs、应用完整补丁、核对实际变更、创建 tree（支持普通新增/删除）。禁用全局 autocrlf，避免 Windows 实测发现的字节变化。GitHub 显式启用却无凭据时失败，不能退回 Mock。
- graph 所有退出路径清理工作目录，恢复不会重复插入 TestRun。

验证：审计安全/可靠性、Worker、P1 resilience、审批和幂等相关 **72 passed，3 个预期本地执行警告，16.16 秒**。GitHub Crash/429/503 为 Mock HTTP，不是真实 GitHub 成功案例。长于 60 秒的心跳、专用 PostgreSQL 和 Docker 行为实验将在最终验收补记。

## P1 MCP、证据与评测阶段

- MCP/Gateway 拒绝 isError、业务 error、异常 JSON、空、多结果块、partial/truncated 和超大输出；输入按 schema、长度、范围校验；实际重试计入调用预算。MCP 子进程只获得专用 reference 配置；工具输出和审计日志有界并脱敏。
- SQL 保留 sqlglot，增加参考 schema 的表/列/函数许可、系统表/未知函数/CTE 拒绝、执行 LIMIT、DB 只读角色和写权限检查、statement/lock timeout。复杂 CTE 当前不支持，安全边界清晰。
- 模型上下文统一为有界、脱敏、不可信 JSON；保留服务端工具权限；累计 provider 返回的真实 token usage，未返回用量则 null。启用模型但缺 key 会失败。
- Evidence 必须关联当前 run 成功调用、内容/hash 一致；去重并要求新独立验证来源；验证失败立即结束，结论必须引用验证证据。评分不再把非空 ID 当作支持。语义因果和相关性仍需真实模型及独立审阅，不编造数值。
- 保留原八例，新增十二个独立 fault case（池等待/缓存回退/代码异常及九种数据/响应契约回归），共二十例。它们是受控 fault injector 的模拟场景，不能当真实 PostgreSQL 优化案例。新增 A 单次固定上下文、B 固定工具、C 自主工具三个策略，复用统一 scorer；P50/P95、产物生成/应用/测试/真实 PR 分开报告。
- 发现并修复 A16 的验收盲点：独立 fault-active oracle 只读挂载在候选目录外。健康测试通过不再计作完整修复准备；三类候选的实际结果保存在离线实验产物。
- 追加 ORM 产物/审批不可变保护、受保护源码干净 base 校验、checkpoint 沙箱 runtime 丢失时重建并重新测试、PostgreSQL 用 DB 时钟校验租约。

验证：相关安全、工具、证据、状态、恢复和 Docker 回归 **115 passed，3 个预期警告，20.19 秒**；二十例 lifecycle 与 reference 测试 **23 passed，1 个依赖警告，3.66 秒**。专用 PostgreSQL 迁移和原有三项集成通过；新增长测试/接管 **2 passed，64.07 秒**。详细测试含义见安全报告，最终全量结果后补。
