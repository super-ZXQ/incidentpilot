# IncidentPilot 面试讲解材料

## 适合的开场

我做的是一个单 Agent 的事故调查与补丁审阅工作流。模型负责计划、选工具、提出假设和补丁；服务端负责权限、状态转换、预算、证据来源、Docker 测试和人工审批。它没有生产部署权限，PR 流程完成也不等于事故解决。

这次先记录基线和源码审计，再分阶段修复。原始八例保留，扩展为二十个可复现模拟故障；真实模型没有 Key，所以没有宣称新的真实模型成绩。Fake 三种策略根因匹配都是 0%，它的用途是回归流程与安全边界。

## 核心技术取舍

| 选择 | 为什么 | 付出的代价 |
|---|---|---|
| 单 Agent + 显式 LangGraph | 状态和停止条件容易审计，支持 interrupt/checkpoint | 依赖框架恢复语义，需要验证重放与 runtime 重建 |
| PostgreSQL queue + lease | 同一套事务存储业务状态及所有权，SKIP LOCKED 避免竞争 | 需要 fencing、DB 时钟、心跳和 checkpoint 串行化；不能随意无限扩容 |
| MCP stdio 只读观测 | 协议清晰，工具面可控，凭据不暴露给模型 | MCP 不是安全沙箱，仍需 Gateway schema、输出语义和数据库角色 |
| Docker 执行补丁 | 默认无网络、非 root、只读挂载和资源限制 | 与宿主共享内核；镜像、文件权限、取消清理仍需实测 |
| 预配置 Bearer 身份 | 最小方案解决未认证与伪造 actor 的真实问题 | 单组织演示方案，不是企业 OIDC/多租户授权 |
| ExternalSideEffect + 远端核对 | 应对 GitHub 已成功但本地未落库的 crash window | 两个系统没有分布式原子事务，需要稳定键与内容比较；可能留下无引用 blob/commit |
| 拒绝复杂 Patch/SQL | 支持范围小而可验证，未知形式默认拒绝 | 不支持 rename/binary/symlink/mode change 或复杂 CTE；要明确告诉使用者 |
| 不增加 Redis/Celery/RAG | 当前问题是边界正确性和证据质量，新增组件不能解决 | 未来高吞吐或组织需求出现后再用实验决定 |

## 可以现场解释的真实 Bug

1. **审批身份伪造。** 原接口直接信任 `body.actor`。现在认证上下文给出 subject/role，发起者不能审批，普通用户不能借 actor 字符串提权；审批同时绑定 artifact/hash/base/test。可以展示 401/403 和 actor 回归。
2. **停止后仍继续生成补丁。** `route_after_collect` 原来只判断 INSUFFICIENT_EVIDENCE，遗漏 NEEDS_HUMAN_INTERVENTION。现在所有非 RUNNING 状态停止，并有状态表和带已有证据的回归。
3. **同步测试阻塞心跳。** async 节点调用 subprocess.run，长测试让 event loop 无法续租。现在取消能等待进程树和 Docker 容器回收；专用 PostgreSQL 的 62 秒实际子进程期间第二 Worker 六次争抢均失败。
4. **旧 Worker 覆盖新 Worker。** 原 mark_status 只改 ORM 快照。现在 owner/attempt/expiry 的条件 UPDATE 锁定当前所有权；即使重复使用同一个 worker_id，旧 attempt 也无法写入。心跳不能复活过期租约。
5. **工具错误变成 Evidence。** MCP isError、异常 JSON 和 Gateway `{error: ...}` 曾被当成功结果。现在失败不生成有效证据，引用必须关联当前 run 的成功调用和匹配 hash。
6. **GitHub 分支创建后的重放失败。** 原重试只查 PR，不处理 branch 已存在。现在读取远端 branch commit/tree/base，再决定后续动作；Mock HTTP 注入 ref/PR 成功后 timeout，重建客户端重放仍只有一个 PR。
7. **Windows 改变补丁字节。** 真实 Git apply 探针发现全局 autocrlf 影响最终文件。应用现在关闭该转换，用 byte patch 文件并校验实际变化范围。
8. **测试通过掩盖修复失败。** 原 healthy regression 先 reset_fault，因此 null/config 的未修复补丁仍可通过。新增候选目录外的 fault-active oracle；三个离线补丁中两个失败，未导出审批产物。
9. **多次尝试累积补丁。** 若在上一候选基础上再改，最后导出的单个 diff 无法代表测试内容。现在每次回到同一 base；回归实际执行 VALUE=1 失败、VALUE=2 通过，并核对各自 TestRun exit code 和最终 hash。

## 可能追问

**为什么 heartbeat 之外还要 fencing？** 心跳是及时发现失去所有权的机制；延迟、断连和暂停会让旧任务继续运行。关键写入必须在数据库里比较 attempt 和有效租约，不能相信进程自己的状态。

**为什么不把所有同步操作 to_thread 就结束？** Python 取消等待线程不等于终止线程中的子进程。Windows Selector loop 的执行线程拥有取消事件、进程树终止和 join；Linux 使用进程组；Docker 还要单独移除容器。

**能保证 GitHub exactly-once 吗？** 不能把两个存储包装成原子事务。采用 at-least-once 重放、稳定业务键、内容寻址对象、不可覆盖分支和远端 PR/head 核对，保证重复恢复不重复业务 PR。真正 GitHub 写入实验仍未运行。

**证据 ID 有效是否说明结论正确？** 不是。当前机械校验能证明来源、归属、成功、完整性和独立观测；模型的因果解释仍可能错误。相关率、因果支持和根因匹配分别报告，未做的人工审阅为 null。

**Prompt Injection 是否已经解决？** 权限不由提示词授予，数据有边界、脱敏和预算，恶意日志不能新增 shell/审批权限。但真实模型仍可能受语义污染；本次没有 Key，真实对抗效果 NOT_RUN。

**二十例是否代表真实 SRE 能力？** 不是。它们是单服务合成故障；延迟注入不等于真实索引/锁争用优化。下一步应在授权测试环境增加真实 DB 计划、独立故障、重复真实模型运行和人工裁决。

**SQLite 为什么不能恢复重启后的审批？** 本项目 SQLite 路径使用 MemorySaver，只用于单进程测试。丢失中断时明确 fail closed；正式 durable recovery 使用 PostgreSQL Checkpoint，并已测试取消后重建沙箱和恢复。

## 演示顺序

先打开 `/demo` 并说明当前 Fake/真实模型、local/Docker、Mock/真实 GitHub 模式。查看来源和假设，再查看具体 Diff/hash/base/test。解释健康回归与 fault-active 的区别，最后展示测试身份的审批与 Mock PR。补充展示 null/config 的失败日志，让面试官能核验限制，而非只看一条成功动画。
