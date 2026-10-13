# 安全测试报告

本次实验均为本机、临时工作目录或专用测试容器，没有操作生产系统或真实 GitHub 写入。

| 边界 | 实现与证据 | 结论 |
|---|---|---|
| 认证/授权 | 所有 `/v1` 和 metrics 的 401、无效 token、角色 403、跨发起者访问、伪造 actor 回归 | 服务端 token 映射为可信身份；无默认匿名模式。reviewer/approver 为组织范围角色，并非租户隔离。 |
| 审批与补丁 | artifact ID/hash/base/test/run 绑定；篡改 diff/hash/base/test、重复审批、ORM 不可变、严格 Git 解析及新增/删除测试 | 不支持 rename/copy/binary/symlink/mode change；明确拒绝。存储管理员仍是可信边界。 |
| Docker | `tests/test_audit_docker.py` 实际行为测试 2 passed，3.76s；既有 Docker 回归另见最终验收 | uid 65534、CapEff=0、NoNewPrivs、只读根/挂载、.env/socket/宿主文件不可见、外网连接失败、取消移除容器。 |
| Worker | 专用 PostgreSQL：62 秒真实子进程、每秒心跳、6 次第二 Worker 争抢失败；强制接管后旧子进程退出且新状态保留。2 passed，64.07s | attempt fencing 与条件 UPDATE；DB 错误使执行取消，不依赖线程取消的假设。 |
| GitHub | MockTransport 注入 commit/ref/PR 成功后超时、429、503，重建 Integration 实例后重放 | 远端 tree/base/head 核对，稳定 branch/run/hash 幂等。Mock HTTP 不代表真实 GitHub 验收。 |
| MCP/Gateway | isError、error、空/异常 JSON、多个块/部分/超大结果、预算内重试、输入 schema | 失败不生成成功 Evidence；所有重试计入实际调用预算；输入输出脱敏和大小限制。 |
| SQL | 系统表、非许可列、未知函数、危险函数、CTE、锁、SELECT INTO、多语句、超大 LIMIT 默认拒绝 | 仅 reference orders/order_items/customers.id；CTE 不支持。DB 端只读角色+只读事务+5 秒 timeout+100 行 LIMIT。 |
| Prompt Injection | 统一有界 JSON 数据包、字段/内联凭据脱敏、系统数据不可信策略、后端工具白名单；恶意日志/源码仍仅为数据 | 机械权限约束可离线验证；真实模型是否被语义误导为 NOT_RUN（无 API Key），不宣称提示词能杜绝注入。 |
| Evidence | 当前 run、成功 ToolCall、输出/hash 一致、去重、独立来源验证、最终结论必须引用验证证据 | 引用合法不等于因果证明；因果支持/相关性需独立审阅，报告为 null/NOT_RUN。 |

## 使用边界

- development/test 才允许显式关闭 Docker，且显示 NON-ISOLATED 警告；任何其他 environment 都禁止宿主运行。受保护源码必须匹配干净 Git base。
- 文件复制排除环境文件、私钥、credentials/secrets/token、云配置、符号链接及依赖目录；这不是通用 DLP。只允许使用专用、经审查的 reference 源码，不能把任意企业工作目录当作无敏感数据。
- Docker 与宿主共享内核，不能保证抵御内核漏洞；本项目不声明高强度多租户沙箱。Linux 进程组与 Windows 受控线程都在取消后等待子进程回收；Docker 必须额外 rm -f。
- healthy-regression-only 通过仅证明健康路径测试通过，不代表故障消失。full_remediation 使用独立、只读挂载的 fault-active oracle；候选源码仍不可信，测试通过不等于形式化证明。
- 本地预配置 Bearer 凭据适合作品集/单组织演示；需要 TLS 反向代理及服务端密钥管理，尚未接入 OIDC/企业身份目录。没有把客户端字符串当身份的例外路径。

## 最终复验

完整 pytest **204 passed、1 skipped、14 warnings**；其中五个专用 PostgreSQL 审计用例全部实际执行（62 秒心跳、接管取消、数据库心跳错误、checkpoint 沙箱重建、只读角色），原有三项 PostgreSQL 集成也执行。唯一 skip 为真实模型 Key 缺失。真实 MCP 独立 1 passed；Docker 专项 5 passed，包含实际隔离和取消回收。环境/依赖/镜像版本及完整原始输出在 `artifacts/`。

默认 Docker 已开启，测试 fixture 必须显式设置 test + 非隔离模式，避免把宿主执行当默认路径。多候选回归验证不同 TestRun 的 exit code 与最终 Diff，一项候选测试失败不会被上一轮或最终轮的 exit code 覆盖。所有新增 `/v1` 展示接口均纳入 401 检查。

另有实际服务暂停实验：仅暂停本次临时 PostgreSQL 容器 12.22 秒，超过 10 秒租约，再恢复服务；旧进程退出，替换 Worker 的 attempt=2 与 RUNNING 状态保留。区别于 Mock ConnectionError 的单元故障注入，记录在 `artifacts/postgres-pause.json`。未暂停、写入或修改任何生产服务。
