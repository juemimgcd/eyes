# Go 隧道接入

用户机器只运行 `eyes-tunnel` 和自己的 HTTP Agent，无需 Python、Eyes Python 包或 SDK。云端仍由 Eyes Runner 执行任务、上传证据及评分；`eyes-gateway` 只处理经过授权的 HTTP 转发。底层使用 Apache-2.0 的 [Rancher remotedialer](https://github.com/rancher/remotedialer)，版本固定在 `tunnel/go.mod`，不自行实现复用、背压或 WebSocket 帧协议。

本功能需要单独启用部署。实现和本地检查不等于真实 Agent、故障恢复或公开多租户安全验收。本阶段按要求不接入真实 Agent。

## 用户接入

在「目标 Agent → 接入 Agent」选择「HTTP 隧道 · 无需 Python」，填写本地完整任务地址、协议、机器系统。页面生成配对码和下载链接。下载后在 Agent 所在机器运行生成的命令。例如（地址和配对码均为占位）：

```sh
chmod +x eyes-tunnel
./eyes-tunnel --server https://example.com/eyes/api \
  --pair-code <页面配对码> --local http://127.0.0.1:9000/run --protocol json
```

Windows 下载 `eyes-tunnel.exe`，使用页面生成的 PowerShell 命令。macOS 首版为 Apple Silicon，Linux/Windows 为 amd64。二进制未进行 Apple 签名或公证；下载后的系统信任提示仍需用户处理，不能把跨平台编译称为各平台运行验收。下载目录的 `SHA256SUMS` 可用于核对传输完整性。

连接进程在前台持续运行，每个实例绑定一个固定接口；其他实例使用不同 `--state-dir`。机器休眠、退出程序或断网时无法调用。重新启动时在原目录运行 `./eyes-tunnel`（PowerShell 为 `.\eyes-tunnel.exe`），或指定原来的 `--state-dir`，不再传配对参数。已有状态优先于启动参数，不会因新参数静默切换目标。

首次配对前保存凭据和兑换请求，因此兑换响应丢失后可以使用原状态重启并重放同一个兑换。默认 `.eyes/tunnel/connection.json` 含敏感凭据，请勿分享或提交。POSIX 使用 0700 目录及 0600 文件；Windows 使用当前用户目录继承的 ACL，部署者需确保该目录不与其他用户共享。

Agent 的认证值从用户本地环境变量读取：首次命令添加 `--auth-env MY_AGENT_AUTH`，变量值为完整 `Authorization`（例如 `Bearer ...`）。配置仅保存变量名，每次重启前需设置变量。平台执行凭据不会转发到 Agent。

## 接口范围

- `json`：POST 测试用例的 input 对象，读取完整 JSON 响应。`--output-pointer` 可选取嵌套的输出对象。
- `chat_completions`：用例 input 自身包含 `model`、`messages` 等实际请求字段，不会把任意 prompt 自动转换为 messages。支持 JSON 和 SSE；SSE 汇总后作为最终评分输出，中断或缺少 `[DONE]` 不能当成完整成功结果。
- 转发地址和路径由用户启动配置固定，不允许云端指定其他目标；只开放 POST 任务接口，不暴露本地管理路径。支持明确指定的局域网地址。
- 不支持自动包装 CLI/Python 函数、Responses、Anthropic、任意 TCP/UDP 或客户端多端口管理。
- 默认每目标并发 1。提高并发必须另行确认 Agent 会话和环境隔离能力；网络复用不证明业务并发安全。

普通 JSON 不支持 SSE。当前 HTTP 适配器默认总响应上限 1 MiB，隧道目标单次网络读取超时默认 900 秒（原 HTTP 接入为 30 秒），且受任务截止时间限制；长任务需按目标配置调整 `request_timeout_seconds`。Gateway 不重放业务请求。连接断开只触发隧道重连；已开始但结果不明的执行由现有 Runner 保留 `unknown`，不自动重跑。不支持 SSE 断点续传、远端取消确认或客户端结果恢复。

## 模型内部通信观测

首次接入命令添加原模型服务地址：

```sh
./eyes-tunnel --server https://example.com/eyes/api \
  --pair-code <页面配对码> --local http://127.0.0.1:9000/run \
  --model-upstream https://model.example.com/v1
```

把 Agent 的模型 base URL 改为 `http://127.0.0.1:8787/v1`，模型名称与 API Key 保留原配置。Go 程序内置本地模型代理，无需额外 Python 进程。只监听回环地址，`--model-listen` 可调整本地端口。请求正文和响应流原样转发，不执行工具、不自动重试模型调用、不跟随上游重定向。

在「Agent 观测」中查看自动创建的「Agent 名称 · 模型通信」来源。每次模型请求是一条独立记录，显示请求内容、响应、SSE 汇总后的内容及工具调用参数、错误、耗时和覆盖范围。显式 `X-Eyes-Trace-ID` 可以关联会话，但不自动把观测绑定到测试 Attempt；没有此标识时保留独立记录，不依赖时间或文本猜测归属。任务接口携带的 Attempt 头不会自动传播到 Agent 内部的模型请求。

覆盖范围为 `model_protocol_only`：模型看到的上下文、提出的工具调用，以及后续请求中由 Agent 回传的结果。代理没有观察工具的真实执行、本地函数、子进程或未经过代理的模型请求。这些信息不能被当作工具执行证据或直接用于要求完整内部轨迹的评分。

每次响应采集最多 1 MiB，事件内容超过 16 KB 保存截断预览。认证值和敏感字段在采集副本中脱敏，实际转发不改变；业务内容仍可能包含隐私，启用前应确认数据适合上传。响应缺失、截断和流中断会显示部分证据。

采集批次先写入 `.eyes/tunnel/observations/`，每两秒尝试上传，服务端确认后删除。开始和终态使用不同文件，避免开始批次 ACK 删除尚未发送的终态。永久拒绝批次改为 `.rejected` 保留诊断日志，其余批次继续；网络故障保留待传文件。目录最多接受 1000 个文件，达到上限或写盘失败会日志报告丢失并继续转发。已经缺失的事件无法恢复，不能把代理转发成功当成证据完整。重启不补造历史请求的终态。

## 云端部署

1. 备份数据库并部署同版本 Eyes API、Runner 和前端，执行 `alembic upgrade head`；本次迁移为 `0013_tunnel_connections`。
2. 构建 Gateway 镜像：`docker build -f tunnel/Dockerfile -t eyes-gateway:local .`。镜像包含 Gateway、三个客户端和 SHA256 清单。
3. 将 `deploy/compose.tunnel.yaml` 合并到现有 Compose 文件之后，启用 `EYES_TUNNEL_ENABLED=true`、`EYES_TUNNEL_GATEWAY_URL=http://gateway:8090` 和 Gateway 服务。未启用时界面说明尚未部署，不生成隧道命令。
4. 云端已有执行 Runner 需要设置 `allowed_http_origins = ["http://gateway:8090"]` 和 `tunnel_gateway_url = "http://gateway:8090"`；保留其原有容量、凭据及评分配置。若 Runner 不在同一 Docker 网络，应配置可达的内部地址，并同步 API 配置。不能给 Gateway 的 `/invoke` 配置公网转发。
   若没有现成云端 Runner，可使用 `deploy/tunnel-runner.example.toml` 与 overlay 的 `tunnel-runner` profile。管理员用 `eyes-admin issue-token --project-id <项目ID> --role runner --all-targets --work-kind execute --work-kind score` 为该项目签发凭据，通过 `EYES_TUNNEL_RUNNER_TOKEN` 提供给服务，再启用 profile。该凭据仅限指定项目；不同账号的项目需要各自 Runner/凭据和独立状态卷，首版不会自动为所有注册账号配置云端执行容量。自定义 Python 评分器仍需管理员安装。
5. 前端 Nginx 仅公开 `/tunnel/connect` 与 `/tunnel/downloads/`。使用 `/eyes` 部署前缀时，更新宿主 Nginx 的 `deploy/nginx-eyes.conf` 中 WebSocket location，再验证和 reload。HTTPS 仍由现有入口终止。

Gateway 没有公开第三方调用 API；每次 `/invoke/{pairing_id}` 由控制 API 校验 Runner 的项目、目标和执行权限。隧道连接凭据的 role 为 `tunnel`，不能注册 Runner、领取工作或管理项目。连接约每 10 秒重新鉴权；撤销后禁止新调用，已建立连接最迟在下次鉴权完成时断开，但不证明已经运行的 Agent 停止。控制 API 不可用时断开连接，不继续使用缓存权限。

这是可选部署层，当前生产发布脚本不会自动启用此 overlay 或发布 Gateway 镜像。启用时需将 overlay 纳入实际发布配置；未发布的源码改动不改变现有线上环境。

## 开发与验证

Go 版本见 `tunnel/go.mod`，从仓库根目录执行：

```sh
cd tunnel
go build ./...
go vet ./...
sh package.sh # dist/tunnel/ 中生成 Gateway、三个客户端、校验清单和依赖许可
```

打包产物中的依赖许可需随客户端一起分发。

本地 Gateway 可指定 `--api http://127.0.0.1:8000 --listen 127.0.0.1:8090 --downloads <客户端产物目录>`。Vite 通过 `EYES_TUNNEL_PROXY_TARGET` 代理 WebSocket 和下载；控制 API 继续使用 `EYES_API_PROXY_TARGET`。首版仅允许回环地址使用明文 HTTP，远程控制端、网关和模型上游要求 HTTPS。

没有新增或修改测试文件，也没有真实 Agent 接入。跨平台编译不能替代 Windows/macOS 下载运行、公司代理网络、长时间断连、公开租户隔离和真实 SSE/评分闭环验收。

### 2026-11-07 本地检查记录

- `ruff check .`、`ruff format --check .`、`git diff --check` 通过。
- `go build`、`go vet` 通过；macOS arm64、Linux amd64、Windows amd64 客户端均成功交叉编译，产物位于 `dist/tunnel/`（不入 Git）。
- 前端 lint、typecheck、build 通过，Impeccable 对改动组件的检测未返回问题。
- 独立 PostgreSQL 18.6 从空库升级到 `0013_tunnel_connections`，`alembic check` 无新增操作；本地 API readiness 为 ready。
- Compose 合并配置（含可选 Runner profile）通过检查；没有执行镜像部署。
- 本地 Gateway 健康接口返回 200；经过 Vite 的客户端下载返回 200；无凭据连接返回 401。
- Chrome 自动化宿主不可用，未完成实际页面渲染检查。没有真实 Agent、成功隧道业务调用、模型代理真实上游、评分或断网恢复验收；没有新增或修改测试文件、测试用例、fixtures 或 mocks。
- 临时 API、前端、Gateway 和隔离数据库检查结束后已停止，未更改线上部署。

### 审查修复

- 控制镜像预建 `/app/data/tunnel-runner` 并授予 eyes 用户权限。新镜像加临时匿名卷核对结果为 UID/GID 10001、目录 10001:10001，修复新卷不可写的问题；旧的 root 所有者数据卷需由管理员单独修正权限，更新镜像不会覆盖已有卷权限。
- 模型响应开始后的上游读异常会中止下游 HTTP 响应，避免把断流变成正常 EOF；终态采集仍由 defer 保存，原始读异常写入日志。
- SSE 未覆盖字段只标记 `partial` 证据，并保留 `response_capture_issues`，不再把完整执行误判为 unknown；缺少结束标记、损坏帧和上游错误仍按错误处理。
- 每个活动模型请求独立发送心跳，不再等待历史观测文件上传完成；请求结束或客户端停止时终止对应心跳。
- 模型响应中的显式 error 独立判定为 failed/ERROR，不再混同于采集受限。
- 本轮 Ruff、Go build/vet、三个平台重新打包及控制镜像构建通过。未新增测试，未运行真实 Agent、故障注入或端到端联调；未部署线上。
