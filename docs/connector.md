# 从 Agent 所在机器主动连接 Eyes

适用于公网 Eyes 评测用户本机或内网中的可信 Agent。连接进程复用现有 Runner，通过出站 HTTPS 领取任务、续租和上传证据；不要求 Agent 暴露入站端口，不创建隧道。连接进程必须保持运行，机器休眠或进程停止时不能领取新任务。

## 首次连接

1. 管理者在“目标 Agent → 接入 Agent”填写名称和本地 Python 入口或 HTTP 地址。
2. 生成连接命令，在 Agent 项目目录执行。页面中的 API 地址包含部署前缀，例如 `https://eyes.example.com/api`；直接访问独立 API 时使用实际 API 根地址。
3. 页面显示“在线”后，可以选择该目标创建实验。配对和入口解析不会执行业务任务；在线只代表 Runner 最近 30 秒有活动，不证明目标 HTTP 服务可用、Python 业务执行成功或隔离能力通过验证。

客户端需要 macOS/Linux 和 Python >=3.14，并安装维护者提供的、与服务端同版本的 Eyes 构建。当前尚未拆出轻量 SDK，也没有把安装命令指向公共 PyPI 同名包。在包含 Eyes 源码的环境中可用 `uv sync --frozen`；在独立 Agent 环境中安装维护者提供的 wheel，再安装该 Agent 自身依赖。源码开发可用 `uv pip install --python /path/to/agent/.venv/bin/python -e /path/to/eyes`。

以下是参数示例，配对码应使用页面实际生成值：

```sh
eyes connect --server https://eyes.example.com/api \
  --pair-code <页面配对码> --entry my_agent.integration:execute
```

Python 接入函数接收输入 JSON 对象及现有 SDK `AgentContext`，支持同步或 async，返回 JSON 对象或 `ExecutionResult`。入口模块可以使用当前 Agent 项目目录；也可以通过 `--python /absolute/path/to/.venv/bin/python` 指定安装了同版本 Eyes 的解释器。首次连接会导入入口模块检查依赖，因此模块顶层不能自行启动业务任务。

```python
async def execute(input, context):
    answer = await my_agent.run(input["prompt"], workspace=context.workspace)
    return {"answer": answer}
```

上例中的 `my_agent` 是项目自身入口。模型密钥通过显式映射传入 `context.secrets`，不会自动继承所有进程环境变量：

```sh
eyes connect --server https://eyes.example.com/api \
  --pair-code <页面配对码> --entry my_agent.integration:execute \
  --secret model=OPENAI_API_KEY
```

函数使用 `context.secrets["model"]`。平台只保存环境变量名；实际值需要在启动及每次重启连接进程前提供。保留原错误与真实输出；不要捕获所有异常后返回一个看似成功的对象。

已有 HTTP Agent：

```sh
eyes connect --server https://eyes.example.com/api \
  --pair-code <页面配对码> --url http://127.0.0.1:9000/run \
  --output-pointer /output --secret agent_auth=MY_AGENT_AUTH
```

HTTP 路径 POST 用例 `input` JSON，默认使用整个响应对象；`--output-pointer` 可选择嵌套对象。`MY_AGENT_AUTH` 保存完整 Authorization 值，例如 Bearer 值。连接进程只允许配置地址的 origin，不跟随重定向。这是用户侧 Runner 调用本地接口，不是平台直接访问用户提交的任意地址。

## 重启、权限和状态

默认状态目录是当前项目 `.eyes/connector`，其中 `connection.json` 包含执行凭据，文件原子写入且权限为 0600；目录按 0700 创建。请将 `.eyes/` 加入 Agent 项目的忽略规则，不提交、不上传整个状态目录。其他目录可通过 `--state-dir` 指定，每个连接使用独立目录。

在原目录重新运行即可恢复，已有连接不能通过新参数静默改绑：

```sh
eyes connect
# 或
eyes connect --state-dir /absolute/path/to/connector-state
```

配对码随机生成，只有 SHA-256 摘要入库，10 分钟有效，只绑定一个客户端凭据。客户端先持久化随机凭据再兑换；服务端在同一事务内发布目标、绑定执行权限并消费配对码。丢失兑换响应后，同一客户端可用完全相同请求重放，返回既有目标；不同请求不能再次兑换。过期且尚未兑换的连接需要重新生成配对码并使用新状态目录。旧目录可能含执行证据，不能随意删除。

网页“连接状态”列出当前项目最近 100 个配对记录，并每 3 秒更新。已兑换但 Runner 未注册或超过 30 秒未活动时显示离线。管理者可撤销配对码或连接凭据；撤销不能证明已开始任务停止，执行中的任务应先按既有取消/核对流程处理，必要时保留 unknown 状态。

配对只授予新目标版本的 `execute` 权限，既不能管理项目，也不能领取其他目标或评分任务。默认执行槽位为 1，不自动声明会话/环境隔离、取消、幂等或状态核对能力。Python 支持现有显式 SDK 事件，但没有埋点的内部行为不会自动出现。高级生命周期钩子、并发能力和复杂输入准备继续使用手动配置入口。

评分由平台已有评分 Runner 处理，连接进程不安装业务评分器。Python 最终结果使用现有 queued 链路，因此部署需运行 RabbitMQ 和 `eyes-result-consumer`；事件保持 direct。断线、未知结果、证据封存及重启恢复继续遵循 [Runner 接入约定](agent-integration.md)。

## 服务端部署

新增迁移 `0011_agent_pairings`。先备份，再执行 `alembic upgrade head`，部署匹配的 API/调度与前端版本。公网反向代理需要将现有 API 前缀下的 `/v1/agent-pairings` 转发到 API，包括公开兑换路由；兑换依赖配对码，其他配对管理接口仍要求项目 manage 凭据。远程 `eyes connect` 强制使用 HTTPS；仅本机回环地址允许 HTTP。

公开 API：

- `POST /v1/agent-pairings`：管理者生成配对码，只在响应中显示一次。
- `GET /v1/agent-pairings`、`GET /v1/agent-pairings/{id}`：查看本项目连接，不返回码或凭据。
- `POST /v1/agent-pairings/redeem`：持码客户端兑换、自动发布目标和绑定权限。
- `POST /v1/agent-pairings/{id}/revoke`：管理者撤销。

此功能延续单团队可信 Agent 的边界，不代表不同租户之间的完整安全隔离已验收。Agent 代码在用户机器运行；连接进程及插件仍应按可信本地代码管理。

## 本次验证范围

2026-11-05：隔离 PostgreSQL 18.6 数据库从空库迁移至新 head，`alembic check` 无模型差异；实际 API 配对、通过 `/api` 代理注册 Runner、重复兑换复用同一目标、进程重启复用 Runner、网页显示在线和撤销均完成检查。桌面 Google Chrome 查看了输入表单、命令生成、在线与撤销状态。Ruff、前端 lint/typecheck/build 通过。

没有创建或修改测试文件、用例或模拟 Agent。该检查没有运行真实业务 Agent、模型或完整评分流程，没有修改公网部署，不能作为公网 E2E 或多租户验收证据。

## 2026-11-05：公网配对与本机 pi 实测

用户授权升级后，公网 `https://www.mneme.com.cn/eyes/api` 已迁移至 `0011_agent_pairings`。使用新配对接口和 `eyes connect --entry pi_bridge:execute` 连接本机 pi 1.4.0，源码提交 `2c8d1feb147f6b3d97e855da803bfe7b19f3c403`。未改动 pi 源码或用户已打开的交互会话；每项任务启动独立 JSON CLI 会话，使用本机已有 DeepSeek 凭据、`deepseek-v4-pro`、thinking off，并关闭工具、扩展、MCP、skills 和上下文文件发现。

实验：[858bcad2-70b8-42cf-9e66-6741b6e0f3e9](https://www.mneme.com.cn/eyes/experiments/858bcad2-70b8-42cf-9e66-6741b6e0f3e9)。

| 用例 | 真实输出 | 独立规则评分 | 执行耗时 |
| --- | --- | --- | --- |
| 17 × 24，只输出整数 | `408` | pass | 2.353469 秒 |
| 原样输出标记 | `Eyes-cloud-pi` | pass | 2.377159 秒 |

耗时按云端 Attempt 的 `execution_intent_at` 到 `finished_at` 计算，包含结果传输/应用边界，不是纯模型延迟，也不是排队到评分的总耗时。计划 2、执行成功 2、有效评分 2、通过 2，失败/unknown 均为 0。评分在独立本机 score-only Runner 执行，云端持久化结果。两次最终结果经现有 RabbitMQ 链路消费，数据库 `result_submissions` 均为 persisted。每项上传 8 条事件，证据 sealed、报告 dropped_events=0；覆盖仅包括 Runner 阶段、pi 会话、最终模型响应和 settled 事件，没有上传推理正文或既有对话历史。

本机证据与接入入口保存在 Git 忽略目录 `data/pi-cloud-20261105/`：`summary.json`、`results.json`、`case-runs.json`、两份事件记录、`pi_bridge.py`、运行日志及部署日志。目录中的管理/执行/评分凭据为私有文件，不应提交。新配对流程和公网真实模型任务已跑通；本轮不证明工具执行、文件产物、并发、取消、故障恢复或任意 Agent 的通用接入。公网静态页面与 API 可访问；原生 Chrome 检查超时，未声称公网结果页面已完成视觉验收。
