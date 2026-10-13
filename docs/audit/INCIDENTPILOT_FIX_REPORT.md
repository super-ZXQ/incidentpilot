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

## P2 演示与最终工程验收

- `api/static/demo.{html,css,js}` 和 favicon：同源轻量工作台，无框架或外部资源，数据用 textContent 渲染；CSP、凭据只留页面内存；显示完整调查、测试、产物绑定及审批。桌面 1440、手机 390 检查并实际通过测试身份审批，显示明确的 Mock PR。独立 UI 审阅的产品记录和 UTC 时间两个问题均已关闭；未宣称完整 WCAG 审计。机械检测为 parser 缺失的 regex 降级，报告无命中也不当成完整通过。
- `api/app.py`、`models/schemas.py`、`agent/graph.py`：受保护的 runtime/tests/approval 查询；Run 返回外部 Incident ID，展示假设；节点状态/计划落库；统一 UTC 输出；工具/证据保留实际观测时间。
- `sandbox/manager.py`、`agent/graph.py`：每次候选回到同一 base；测试前后检查工作区内容；各 TestRun 保存自己的退出码/耗时；checkpoint 重建后的测试重新执行。VALUE=1 失败、VALUE=2 成功的真实子进程回归证实最终 artifact 对应第二个完整候选。
- `worker.py`：SQLite memory checkpoint 缺失时明确转人工介入；正常停机取消活跃执行并等待清理。`config.py` 默认启用 Docker；只有显式 development/test 非隔离覆盖；受保护环境拒绝 embedded worker。
- `eval/harness.py`、`eval/scoring.py`：测试失败计入测试通过率分母；根因类别只匹配明确类别/许可别名。`scripts/audit_evaluation.py` 保存 A/B/C、三类 Docker 候选和失败证据，无真实外部写入。
- `.env.example`、`README.md`、`docs/ARCHITECTURE.md`：更新认证、默认 Docker、迁移、启动 factory、当前能力与历史报告界限。`load/locustfile.py` 添加明确测试凭据；`.github/workflows/ci.yml` 增加 Docker 行为测试镜像及专用 PostgreSQL 审计测试配置。CI 远端运行与 Locust 性能实验仍 NOT_RUN。
- `infra/reference-db-init.sql`：只读角色的 work_mem/temp_file/idle timeout；server 拒绝无时间归属的旧缓存遥测成为当前证据。实际只读角色查询与 superuser 拒绝已在专用 PostgreSQL 验证。
- `PRODUCT.md`、`DESIGN.md`、`.impeccable/design.json`、`docs/audit/DEMO_DESIGN.md`：仅记录用户既定产品限制与实际轻量 UI，没有重设计产品或新增运行组件。

### 最终检查与原始材料

| 检查 | 实际结果 | 原始证据 |
|---|---|---|
| Ruff 全仓库 | PASS | `artifacts/ruff-final.log` |
| 完整 pytest（含专用 PG、双 Worker、62 秒进程、恢复、MCP、Docker） | 204 passed / 1 skipped / 14 warnings，198.55s | `artifacts/pytest-final.log`、`pytest-final.xml` |
| 独立 live MCP stdio | 1 passed，4.21s | `artifacts/mcp-live.log` |
| Docker 专项 | 5 passed / 200 deselected / 1 依赖警告，10.67s | `artifacts/docker-final.log` |
| PostgreSQL Alembic | 0001 → 0005 PASS，专用临时容器 | `artifacts/postgres-migration.log` |
| SQLite Alembic | 0001 → 0005 PASS，临时文件 | `artifacts/sqlite-migration.log` |
| 实际数据库暂停与接管 | 专用 PostgreSQL 暂停 12.22s > 10s 租约；旧进程退出、新 worker attempt=2 | `artifacts/postgres-pause.json` |
| 二十例 A/B/C | Fake 各 20/20，根因匹配均 0% | `artifacts/fake-comparison-*.json` |
| 三类 fault-active 补丁 | 1 PASSED、2 FAILED；无真实 PR/真人审批 | `artifacts/offline-patch-*.json` |
| 原八例未修改 | 8/8 与基线内容一致 | `artifacts/original-case-hashes.json` |
| 浏览器审批及响应式 | 可信测试身份 / Mock PR；390px 无横向溢出；UTC 修复 | `artifacts/demo-verification.json`、`demo-desktop.png`、`demo-mobile.png` |

首次完整运行 200 passed 后，对默认隔离、日志时间、独立 TestRun exit code 和评测分母补做修正及回归；最终重新执行全套得出上述 204 passed。开发期间新增测试构造/导入错误已修正，最终没有失败或 xfail 隐藏。CI YAML 更新未冒充远端 CI 成功。

完整文件清单见 `artifacts/modified-files.json`，相对于基线 SHA 记录全部源码、测试、配置、文档与实验产物。

收尾配置核验额外修复：GitHub 工厂明确接收当前 Settings，避免只读进程环境而忽略 `.env` 的启用配置；启用但缺凭据仍返回 real 模式失败，不会降级 Mock。认证凭据按 bytes 比较，非法非 ASCII Bearer 返回 401 而非 TypeError/500。两项均有回归。

### 剩余风险与未执行项

真实模型诊断/抗注入/补丁能力因无 Key 为 NOT_RUN；真实 GitHub crash recovery 因无授权专用仓库为 NOT_RUN；真人审阅为 NOT_RUN。复制过滤不是通用 DLP，必须使用专用经审查源码；Docker 不防御内核级逃逸；预配置身份不是 OIDC/租户隔离。真实 GitHub 还要求 reference repo 根与 remote repo 根、clean base 一致，不能把 monorepo 子目录直接假定成独立仓库。高于已测双 Worker 的并发需先评估 SQLAlchemy pool、checkpoint 连接及 PostgreSQL 容量，未给出扩容或性能提升百分比。graph.py 早期重复 builder/未使用节点保留，未做无关清理。

### 测试资源收尾

本次专用 PostgreSQL 容器已停止并由 `--rm` 移除，沙箱容器列表为空，演示 API 的 8019 端口不再监听。浏览器会话已关闭，截图作为交付产物保留。自动审批审查拒绝删除临时 SQLite 文件，工具只返回 `blocked by policy`；未尝试绕过策略。该文件保留在 `C:\Users\26811\AppData\Local\Temp\incidentpilot-audit-demo-20261013.db`，仅含本次 Fake/Mock 演示数据。
