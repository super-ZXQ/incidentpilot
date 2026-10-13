# IncidentPilot

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

后端/SRE 调查人员、审阅者与审批者。来源：用户任务书。

## Product Purpose

以单 Agent 的受控流程调查 Incident，收集证据、验证假设、测试补丁，再由可信人工身份审批外部 PR。展示真实能力和失败边界，不能把流程完成当作故障真正解决。

## Capabilities and Constraints

保留现有 FastAPI、LangGraph、PostgreSQL、MCP stdio、Tool Gateway 和 Docker 架构。演示页面必须轻量、专业，展示 Incident、状态、计划、工具轨迹、证据、假设验证、根因、Diff、测试、审批及 GitHub 状态。明确区分 Fake 与真实模型、Mock 与真实 GitHub、local 与 Docker、健康回归与故障激活测试。

不引入新前端框架或装饰性工程。未运行的真实模型、真实 PR 和人工决策必须标 NOT_RUN。页面本身不保存访问凭据。

## Evidence on Hand

源码与 `docs/audit/` 的测试及实验产物。没有真实模型实验或真实 GitHub PR 成功记录。本次 UI 截图来自本机测试 API，使用明确的 Fake 和 Mock 模式。
