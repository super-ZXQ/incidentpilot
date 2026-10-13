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
