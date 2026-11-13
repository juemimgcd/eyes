# Eyes

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

开发和调试 Agent 的工程师。他们需要从一条任务找到对应的模型通信、工具调用与执行证据，也需要在修改 Agent 后比较两次实验的结果。

## Product Purpose

被动观测和主动评测共用个人工作空间。观测从 Agent 实际上报的任务开始。评测从已发布的目标、测试集和评分口径开始，实验固定使用的版本，回归报告保留所依据的执行记录。

## Capabilities and Constraints

- 任务状态和证据完整度独立展示。模型代理可记录模型通信，工具执行需要 Agent 上报。缺失、截断或过期的证据应当可识别。
- 列表分页、会话筛选、深链接和键盘导航支撑从任务到证据的往返。同步状态以实际连接和读取结果为准。
- 接入用途决定权限。仅观测和允许平台发起评测分别授权。

## Evidence on Hand

产品流程见 [前端说明](docs/frontend.md)、[观测说明](docs/observation.md) 和 [Agent 接入说明](docs/agent-integration.md)。工作空间内各账户的记录由控制 API 提供。
