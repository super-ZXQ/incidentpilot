# IncidentPilot 评测报告（2026-10-13）

## 实验身份与条件

本次离线运行命令：`uv run python scripts/audit_evaluation.py`。Windows / Python 3.12.6 / uv.lock 依赖，SQLite 临时控制库，`fake-model-v1`、Fake 只读工具、temperature=0，二十例每策略一次。原八个 YAML Ground Truth 未修改；新增十二例均为独立模拟故障机制。Ground Truth 仅交给 evaluator，未放入 Incident payload 或模型上下文。

真实模型诊断、真实模型 A/B/C、真实模型三场景补丁：**NOT_RUN（没有 API Key）**。真实 GitHub PR：**NOT_RUN（没有明确授权的专用测试仓库）**。实际人工审批：**NOT_RUN**；自动化测试中的 test-reviewer 决定仅证明审批机制，不冒充真人审阅。

## Fake 对照结果

| 策略 | 完成 | 自动根因匹配 | 引用合法 | 平均观测调用 | P50 秒 | P95 秒 |
|---|---:|---:|---:|---:|---:|---:|
| A：固定上下文，单次结论 | 20/20 | 0% | 100% | 4.0 | 0.0612 | 0.0744 |
| B：固定工具顺序 | 20/20 | 0% | 100% | 4.0 | 0.0650 | 0.0763 |
| C：自主工具 LangGraph | 20/20 | 0% | 100% | 4.0 | 0.0666 | 0.0716 |

A 的四次调用是固定上下文采集，模型不选择工具；真实模型路径只进行一次结论请求，不独立验证。B 固定顺序采集三个来源，再固定源码验证；C 让模型决定每次工具选择。三者使用同一故障集和 scorer。Fake 本身按脚本返回通用结论，**三种策略全部 0% 根因匹配**；不能据此比较真实模型优劣。耗时只包含本机 Fake 流程，不含真实推理，单次样本也不足以宣称性能改进。

引用合法率检查当前 Run、成功 ToolCall、内容/hash 对应。它不等于证据相关性或因果证明。`evidence_relevance_rate` 和 `causal_support_rate` 为 null，等待独立审阅。自动根因匹配使用类别、组件及词项事实匹配，是启发式代理指标，不是人类裁决的因果正确率。Token 和成本为 null；未调用真实模型，没有虚构用量。

## 三类故障激活补丁实验

这些实验用 Fake 调查与确定性 FakePatcher，但真正把补丁应用到复制的 reference 源码，并运行真实 Docker pytest。独立测试文件在候选目录外、只读挂载；先获得健康响应，再保持指定 fault 激活，比较响应及延迟。

| 原始用例 | 生成 / 应用 | Docker fault-active 测试 | 产物与审批 | 真实解决结论 |
|---|---|---|---|---|
| fault_002 slow_database_query | 成功 / 成功 | PASSED | 生成绑定产物；模拟 test-reviewer APPROVE；Mock PR | 仅移除模拟 delay，不是 PostgreSQL 查询优化；不能声称真实根因/真实事故解决 |
| fault_003 null_exception | 成功 / 成功 | FAILED：仍返回 HTTP 500 | 无可审批产物 | 确定性补丁未修复异常分支 |
| fault_007 incorrect_configuration | 成功 / 成功 | FAILED：响应只有 1 条而非健康 7 条 | 无可审批产物 | 确定性补丁未修复 max_limit 配置 |

三例生成率 3/3、应用率 3/3、fault-active 测试通过 1/3、准备产物 1/3、真实 PR 0/3；真正生产故障解决率 **NOT_RUN**。这里的比例只描述这三次离线候选，不能推广为 Agent 真实模型修复率。失败用例保留原 Ground Truth、Diff、stdout/stderr、Evidence 和原因。

## 原始记录与复跑

- `artifacts/fake-comparison-{direct,fixed,autonomous}.json`：每例状态、分项匹配、工具数量、环境及耗时。
- `artifacts/offline-patch-{slow_database_query,null_exception,incorrect_configuration}.json`：完整工具轨迹、Evidence、假设、Diff、TestRun、产物和模拟审批绑定。
- `artifacts/experiment-status.json`：EXECUTED / NOT_RUN 总表。
- `pytest-final.log` / `pytest-final.xml`：最终测试输出；其他专用实验见安全报告。

真实实验需专用 reference 环境和受保护 Docker。使用 `EvaluationHarness(..., tier="real_llm_investigation", strategy="direct|fixed|autonomous")`，保持相同二十例、相同模型版本/温度/预算，增加重复运行次数，保存 provider 用量，再由独立审阅者评价相关性与因果支持。`tier="full_remediation"` 使用 fault-active oracle。实验故障设置仅可指向专用 reference，不能对生产接口调用 `/admin/fault`。

## 剩余限制

新增数据响应故障有可复现机制，但仍是单服务合成 benchmark；pool/cache 等复杂标签表示实验难度预期，不代表真实连接池、缓存或 PostgreSQL 故障复杂度。默认 healthy-regression-only 测试仅检验健康路径；完整修复必须另做故障保持激活和部署后复现。RESOLVED 保留现有枚举，语义是受控 PR 流程完成，不等于已合并、已部署或事故解除。
