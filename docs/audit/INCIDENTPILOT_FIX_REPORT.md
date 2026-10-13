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
