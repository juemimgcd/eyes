# Eyes 统一 Agent 接入协议 v1

状态：**契约、异步客户端、Runner 执行/核对与持久化包装服务已实现，执行与故障联调尚未完成。**
本文中的 `/agent/v1/` 路由由接入端提供，Eyes 控制 API 不冒充目标 Agent。
旧 HTTP/Python 适配器保留兼容，标准协议使用独立的 `agent_protocol` 适配器。

## 1. 目标与边界

任何语言、框架实现的 Agent，只要实现相同的执行协议，就能通过同一种 Eyes 接入路径参加评测。Eyes 核心不导入外部 Agent 模块，也不根据 Pi、Deta、Zeta 等名称分支调用。

职责分为三部分：

- Eyes 控制端：保存实验、任务、配额、证据、评分与历史。
- Eyes Runner：领取工作，作为协议客户端提交任务、查询状态、接收证据并向控制端报告。
- Agent 接入端：实现本协议，将任务交给实际 Agent，并对自身的幂等、持久化、隔离、状态和停止确认负责。

接入端可以内置在 Agent 中，也可以是接入方部署的独立服务。私有调用方式仍需要在这个边界内转换，但实现一次即可复用；它不要求提交到 Eyes 仓库，也不要求使用 Python。已有 Deta/Zeta 桥接仅作为接入行为和验证案例参考。

协议统一任务封装、生命周期、资源和证据交换。业务输入仍由能力声明中的 `input_schema` 描述，Eyes 使用数据集提供该输入；不会要求所有 Agent 的业务参数相同。评分预期、评分器配置及 Eyes 管理/Runner 令牌不发送给目标。

## 2. 契约来源与版本

- 数据类型来源：[agent_protocol.py](../src/eyes/contracts/agent_protocol.py)。只依赖公共数据类型和 Pydantic，不依赖 ORM、Runner 或任一 Agent。
- 语言无关的类型定义：[agent-protocol-v1.schema.json](agent-protocol-v1.schema.json)，使用 JSON Schema 2020-12。每种消息通过 `#/$defs/AgentTask` 等引用单独校验；顶层是类型目录，不是一个通用消息的校验入口。
- HTTP 行为、状态转换、跨请求约束以本文为准。JSON Schema 覆盖字段、类型、边界和必填项；跨字段及跨请求语义还需接入方按本文实现。Python 类型提供部分跨字段校验，不以“Schema 校验成功”替代协议一致性验收。

重新生成类型定义：

```sh
uv run python -m eyes.contracts.agent_protocol > docs/agent-protocol-v1.schema.json
```

每个 JSON 消息显式携带 `protocol_version: "1.0"`，缺失版本不自动按当前版本解释。不认识的字段或版本明确拒绝；不静默忽略并继续运行。未来兼容性变化需发布明确的新契约，不能只修改某个接入端的私有行为。

## 3. HTTP 绑定

基地址由接入方配置，以下路径相对于基地址。JSON 使用 UTF-8，普通响应为 `application/json`；所有成功响应中的身份必须与请求一致。标准响应不返回实现栈、凭据或完整请求头。

| 必需 | 方法与路径 | 请求 / 响应 | 含义 |
| --- | --- | --- | --- |
| 是 | `GET /agent/v1/capabilities` | `AgentCapabilities` | 发现版本、输入格式、隔离、容量与可选能力 |
| 是 | `POST /agent/v1/runs` | `AgentTask` → `AgentRun` | 接受一次业务执行，首次成功为 202，同键重放为 200 |
| 是 | `GET /agent/v1/runs/by-key/{idempotency_key}` | `AgentRun` | 提交响应丢失后查询原执行，不新建工作 |
| 是 | `GET /agent/v1/runs/{run_id}` | `AgentRun` | 读取当前状态快照 |
| 是 | `GET /agent/v1/runs/{run_id}/result` | 200 `AgentResult`，或 202 `AgentRun` | 获取终态/未知结果；仍在执行时返回当前快照 |
| 否 | `POST /agent/v1/runs/{run_id}/cancel` | 无 body → `AgentCancelResult` | 请求取消；可安全重复调用 |
| 否 | `GET /agent/v1/runs/{run_id}/events?cursor=…&limit=…` | `AgentEventPage` | 读取统一事件封装，limit 为 1–200 |
| 否 | `PUT /agent/v1/resources/{resource_id}` | 文件字节 → `AgentResource` | 在提交前上传任务资源 |
| 否 | `GET /agent/v1/runs/{run_id}/artifacts/{artifact_id}` | 文件字节 | 下载该运行结果列出的产物 |

v1 先固定 HTTP+JSON，不同时引入 stdio、WebSocket 和多种推送路径。事件读取使用可重放分页，后续再根据实际需求增加流式传输。

认证使用接入端独立的 Bearer 凭据或部署约定的 mTLS。远程部署使用 HTTPS；可信本机调试可以使用 loopback HTTP。身份范围必须覆盖发现、提交、按键查询、状态、事件及文件读取，不能凭一个 UUID 访问其他调用方的数据。Eyes 的内部令牌不转发给 Agent。

## 4. 能力发现

`AgentCapabilities` 包含：

- `agent_id` 与 `agent_version`：接入端身份和可确定的版本；版本不明确时显式为 null。
- `input_schema`：业务输入的 JSON Schema 2020-12 对象；无额外约束时也应明确声明 `{"type":"object"}`。
- `max_concurrency`、`session_isolation`、`environment_isolation`：目标可兑现的共享容量及隔离边界。并发大于 1 要求两种隔离均成立。
- `cancellation`、`resources`、`events`、`artifacts`：可选端点是否可用。不支持的能力返回 false，对应操作返回明确的 `unsupported_capability`。
- `terminal_retention_seconds`：终态记录、去重记录及已发布结果/产物至少保留的秒数，最少 86400 秒。

幂等提交、按键查询和状态查询是核心要求，不作为可关闭的能力。一个只能临时调用函数、进程退出就丢失全部状态的服务不符合这个核心协议。

接入端还必须自行限制活动任务容量，多个 Eyes Runner 或其他客户端共同访问时不能超额。Eyes 的实验、项目和全局额度继续生效；能力声明不替代接入端的容量控制。能力或版本变化需要重新发现并发布目标版本；不能改写已冻结实验快照。

## 5. 任务、身份与幂等

`AgentTask` 包含以下字段：

| 字段 | 约定 |
| --- | --- |
| `task_id` | 对应逻辑测试任务，重试业务执行时可以保持不变 |
| `attempt_id` | 一次业务执行的唯一身份；新的业务执行必须使用新 ID |
| `idempotency_key` | 同一执行的传输重试复用原键；不得用新键盲目重试响应丢失的提交 |
| `deadline` | 带时区的绝对截止时间；重传不能重新获得一整段超时时间 |
| `input` | 符合已发布 `input_schema` 的业务输入 |
| `resources` | 已上传且当前身份有权读取的输入文件引用，名称在任务内唯一 |
| `traceparent` | W3C 格式的执行上下文，关联追踪，不替代业务身份 |

`run_id` 由接入端生成并稳定保存，一个调用方的一次 `attempt_id` 只能关联一个运行。同一 `attempt_id` 换键重复提交也必须拒绝为冲突，不能开启第二次业务调用。

接入端先校验请求、权限、资源、截止时间与容量，再原子保存请求、幂等映射和执行意图，成功持久化后才能返回接受响应。业务执行只能在记录接受之后开始；崩溃后不得因为“不记得返回过什么”而重新执行未知任务。

相同身份、相同键、相同结构化参数返回原 `run_id` 及当前快照；同键不同参数返回 409 `idempotency_conflict`。对象键顺序不影响比较，数组顺序影响比较，时间按同一 UTC 时刻解释，可选字段按本协议默认值解释。接入方不得通过简单比较原始 JSON 字节误判重传。

网络断开或响应丢失时，Runner 先按原键查询。查询也失败时保留未确认状态；在核心幂等保证有效的前提下才能重发同一个请求，不能创建新 Attempt 或新键。404 仅表示当前身份下没有可返回的记录，不证明远端从未执行过。

活动及 unknown 运行的记录不得按终态保留期删除。已完成记录最早在 `max(deadline, completed_at + terminal_retention_seconds)` 后过期；有过期标记时查询返回 410 `record_expired`。去重记录过期后的原请求已超过 deadline，提交必须拒绝，不能重新执行业务。截止时间已过但去重记录仍在时，可返回原结果。

## 6. 状态与结果

正常流程是 `accepted → queued → running → succeeded / failed`，可以省略 queued。运行中可以进入 cancel_requested。取消或超时只有确认任务及其副作用执行已经停止后，才能写为 cancelled 或 timed_out。

- `accepted`：请求已持久化，尚不代表调用 Agent。
- `queued`：等待接入端自己的执行资源。
- `running`：已开始执行。
- `cancel_requested`：已请求停止，仍未确认结束。
- `succeeded`：执行已结束并具有结构化 output；不表示评分通过。
- `failed`：有明确执行失败事实且已停止；需要错误原因。
- `cancelled` / `timed_out`：已确认停止，需要完成时间。
- `unknown`：执行结果无法确认，需要原因；不能自动当作失败或安排新的业务执行。

`AgentRun` 带递增 `revision`、`observed_at` 以及当前结果。结果只在终态或 unknown 时出现，必须匹配快照的 run_id 和 status。客户端丢弃旧 revision；对同 revision 的冲突内容记录协议错误，不自行选择较新的网络响应。

`AgentResult` 将 `output`、执行错误、`stopped_confirmed`、`completed_at`、清理状态、证据状态及产物分别表达。四种明确终态都要求已确认停止和完成时间。unknown 不填写已确认的结果完成时间，但可以单独报告“已知停止、结果仍丢失”；停止事实不等同于业务成功事实。

对于停止未确认的 unknown，Eyes 必须保留相应执行额度。已确认停止但结果未知的情况，只能通过明确核对路径释放额度，同时保留未知结果，不能伪造成功或失败。控制端已提供 Runner 自动核对映射；停止确认、原运行身份和远端版本校验通过后才更新状态与额度。

明确终态的结果不可覆盖，重新执行生成新 attempt/run。unknown 后若获得可靠事实，可以提升 revision 转为确定终态，并保留原观察与核对来源。证据后补不能覆盖旧结果引用的证据或旧评分，应走补充证据及显式重新评分流程。

## 7. 取消

重复取消同一个 run 不创建新工作。202 表示请求已接受但停止尚未确认，200 表示返回已确定结果；两者都返回 `AgentCancelResult`。

`accepted` 与 `stopped_confirmed` 是独立字段。已接受取消的运行应成为 cancel_requested、unknown 或明确终态；不能回复 accepted=true 却仍提供没有取消记录的 running 快照。停止确认必须与返回的运行结果一致。

取消可能与正常完成竞争。如果任务已成功完成，应返回原 succeeded 结果，不能改为 cancelled。接入端只有确认远端业务执行及相关后台任务停止，才能声明 stopped_confirmed；本地连接关闭、子进程退出或 API 返回“已收到请求”都不足以证明远端已停止。

## 8. 输入资源与输出产物

资源为可选能力。上传使用调用方生成的 UUID：`PUT /agent/v1/resources/{resource_id}`，请求头提供 `Content-Type`、`Content-Length`、`X-Eyes-Resource-Name`（UTF-8 百分号编码）和 `X-Eyes-SHA256`。接入端校验完整字节数和 SHA-256 后才发布可引用的资源，返回对应 `AgentResource`；同身份同 ID 同元数据/字节重传返回原引用，冲突返回 409。

资源与认证身份绑定，提交任务时再次验证引用的名称、大小、摘要和访问权。name 仅是逻辑名称，不直接作为可越界的文件路径。资源可以在提交前上传，未绑定资源的保留期限由接入端公开配置；已接受任务的输入在任务结束前不得被删除。没有完整上传确认时，不能把它作为已就绪资源使用。

协议不发送 Runner 的宿主机绝对路径，也不要求目标能访问 Eyes 的本地磁盘。输入业务数据不能包含模型密钥；模型凭据由 Agent 环境提供。v1 不支持任意远程 URL 拉取，避免把下载授权和模型输入混为一体。

结果中的每个 `AgentArtifact` 包含 artifact_id、name、media_type、size_bytes 和 sha256。下载路径由 run_id 与 artifact_id 构成，客户端不跟随任意重定向。Eyes 完整校验字节数和摘要后保存到自己的证据存储，并生成内部 Artifact ID；不能直接把外部引用当作已入库证据。下载失败保持证据缺口，不能静默跳过后继续声称完整。

不支持资源/产物的目标仍可参与结构化输入输出评测；包含文件要求的数据集在派发前明确判为不兼容，不伪造文件或通过结果。

## 9. 事件与证据

事件按 run_id 分流，每项包含稳定 event_id、运行内递增 sequence、带时区的 occurred_at、type 和 data。不同运行不共享 sequence；接入端负责给多个内部生产者汇总后的流排序，并保留内部来源信息。

分页 cursor 不透明且可重放。同一事件重传保持 ID 和内容不变；相同 ID 不同内容属于协议冲突。事件按 sequence 返回，每页最多 200 项。只要还可能有后续事件，就返回可用于继续查询的 next_cursor，即使本页为空；已经结束且没有后续事件时才返回 null。

标准事件封装可以携带目标原生事件，私有 type 使用 `custom.<namespace>.<name>`；不得把仅有模型提出调用的消息标成已真实执行的工具结果。更细的模型/工具语义事件需在后续契约中定义后才能声明支持，v1 不声称能自动还原任意 Agent 内部执行图。

`AgentEvidence` 独立报告 collecting、sealed、partial 或 unavailable；dropped_events 不知道时为 null，不能填写 0。sealed 要求已声明范围内采集结束且丢弃数为 0，不表示拥有全部内部行为。结果与事件页都保留证据状态；网络重试成功不能消除原本的采集缺失。

## 10. 错误与运行恢复

错误消息统一为 `AgentError`。错误码应稳定，message 为有界且脱敏的人类说明。

| HTTP 状态 | 常用错误码 | 客户端行为 |
| --- | --- | --- |
| 400 | `unsupported_protocol` / `invalid_request` | 修正协议或格式，不启动业务重试 |
| 401 / 403 | `unauthenticated` / `forbidden` | 修正凭据或范围，不重复派发 |
| 404 / 410 | `not_found` / `record_expired` | 记录缺失/过期，不能推断没有执行 |
| 409 | `idempotency_conflict` / `attempt_conflict` | 明确冲突，不生成新键绕过 |
| 413 | `payload_too_large` | 拒绝请求，不截断后继续 |
| 422 | `invalid_input` / `deadline_expired` / `unsupported_capability` | 修正输入或选择兼容目标 |
| 429 | `capacity_exceeded` | 未接受任务时明确返回，客户端按 Retry-After 延迟 |
| 503 | `temporarily_unavailable` | 提交是否生效不确定时先按键查询 |

接入端在重启后恢复原执行映射，查询到同一个 run_id；原业务是否运行不明时返回 unknown，不重放业务调用。协议要求持久化，但不规定必须使用哪一种数据库。没有这个保证的临时进程包装只能作为开发接入，不能标为符合核心协议。

## 11. 与当前 Eyes 的对应关系

| 当前内部数据 | 外部协议 |
| --- | --- |
| CaseRun ID | task_id |
| Attempt ID / idempotency_key / deadline | attempt_id / idempotency_key / deadline |
| 用例 input | AgentTask.input |
| 目标能力快照 | 发现结果经过验证后发布；不把 TargetPublish 或 Runner 配置直接发给 Agent |
| 外部 run_id | Attempt.remote_operation_id |
| ExecutionResult | 经协议状态、身份与停止事实校验后映射；不是直接复制未知 JSON |
| Event / Artifact / Manifest | 先认证、校验和持久化，再建立内部证据引用 |

现有 Python 和通用 HTTP 接入保留兼容。统一协议客户端落地后，新接入优先使用统一路径；不会要求外部 Agent 安装 Eyes SDK。SDK 埋点是可选增强，不是参加基础评测的前提。

## 12. 落地顺序与当前证据

当前提供独立数据契约、可导出的 JSON Schema、异步客户端与 Runner 标准协议执行路径。
客户端检查运行身份、revision 冲突、已确定结果不可变、产物长度与摘要，限制响应大小，
不跟随重定向且不读取代理环境变量。提交响应丢失后按原键查询，只有在协议幂等保证下
重发原请求。取消没有得到确定结果时上报 unknown；事件/产物传输错误保留证据缺口。

在 Runner 所在网络执行能力发现，输出可发布的目标 JSON：

```sh
# 在 runner.toml 中允许目标 origin；使用认证时把对应环境变量加入 secret_env_allowlist。
uv run eyes-runner --config runner.toml discover \
  --base-url http://127.0.0.1:9000 --name my-agent \
  --credential-env MY_AGENT_TOKEN --concurrency 1 > target.json
uv run eyes publish-target --file target.json
```

发现只查询能力，不启动业务执行。没有认证的本机服务可以省略 `--credential-env`。
发布版本冻结完整发现结果；输入 Schema 限制为本地 fragment 引用，避免验证过程主动访问
外部 Schema URL。创建实验时检查用例 input，非空 environment 暂不通过标准协议传输，
会明确拒绝；产物要求只可用于声明 artifacts 能力的目标。执行前发现能力变化时要求重新发布。

当前检查覆盖 Ruff、命令入口和前端类型检查；官方包装服务已在独立 PostgreSQL 18.6
上完成初始化、就绪检查、未认证 401 与 Runner 认证发现。尚未通过接入端执行完整任务，
也未完成服务重启、幂等冲突、并发和停止竞争的运行验收。没有新增测试文件、测试用例
或模拟服务。协议资源上传的产品入口与 Web 发现配置交互仍待完成。

两个真实 Agent 的接入按本轮用户要求暂缓；后续完成 Web 配置流程和协议运行验证。

已有 [Deta/Zeta 实测](../integrations/zeta/README.md) 可以核对协议是否覆盖并发、准备失败、未知执行、取消竞争和代码产物，但它使用的是现有 Python 适配器，**不是本统一协议的端到端验证**。Pi 的原生 RPC 到本协议的转换也尚未实现。

## 13. 官方持久化包装服务

`eyes-agent` 包装维护者安装的 Python 函数，不要求修改 Eyes 控制端。配置参考
[agent-service.example.toml](../deploy/agent-service.example.toml)。业务入口签名为
`execute(input: dict, context)`，可以同步返回 dict 或返回 awaitable；函数返回表示其业务
执行已经结束。`context.task` 含原始任务标识和 deadline，`context.workspace` 是该运行的
独立目录。调用 `context.emit(type, data)` 追加事件，返回是否成功记录；调用
`context.artifact(name, bytes, media_type)` 发布产物。事件采集失败会记录丢弃与诊断，
不覆盖业务输出。业务逻辑仍需自行完成远端资源清理，再返回输出。

配置 `entry_point`、可选 `python/import_paths`、输入 Schema、隔离范围和模型凭据环境变量
白名单。服务数据库凭据和调用方 Bearer 凭据不会传给业务子进程。独立进程用于故障隔离，
维护者安装的代码仍是可信代码；`execution_scope="process"` 仅适用于进程组停止即能停止
全部任务副作用的实现。只有这种声明才提供确认取消能力；external 模式异常或中断后保留 unknown。

```sh
cp deploy/agent-service.example.toml agent-service.toml
# 配置独立 PostgreSQL 数据库地址环境变量 EYES_AGENT_DATABASE_URL，
# 以及 clients 表引用的 MY_AGENT_TOKEN；不要把其值写入配置文件或目标快照。
uv run eyes-agent --config agent-service.toml init-db
uv run eyes-agent --config agent-service.toml serve --port 9000
```

包装服务使用独立 `eyes_agent_*` 表，建议独立数据库，当前包装表 revision 为 1。
`init-db` 仅初始化包装表，不修改控制端 Alembic 迁移。一个 service_id 只允许一个
supervisor，使用 PostgreSQL session advisory lock 持有所有权，并用持久 generation
阻止失去所有权的旧进程迟到写入。连接池预算包含这一条长期所有权连接，其余请求与
状态更新使用短事务；任务执行期间不持有业务行锁。

接收请求时在服务入队锁中原子校验幂等键、Attempt 身份、deadline 和共享容量。
提交之后才启动业务子进程。重启可以继续尚未开始的 accepted 任务；已经写入执行意图的
running/cancel_requested 任务转为 unknown，不重放函数，也不按保存的 PID 杀进程。
多个调用方用 `[clients]` 中不同的稳定 principal 区分，各自凭据只能读取自己的运行。

包装服务支持事件分页与输出产物，当前声明 `resources=false`：不接受输入资源上传。
事件、结果和产物受配置字节数/数量限制，工作目录保留至终态保留期结束。保留清理默认预览：

```sh
uv run eyes-agent --config agent-service.toml maintain
uv run eyes-agent --config agent-service.toml maintain --apply
```

只有明确终态，且超过 `max(deadline, completed_at + terminal_retention_seconds)`，才清除
正文、事件和文件。保留运行/幂等身份 tombstone，后续查询返回 410，不能复用旧键重跑。
unknown 与活动任务不会自动过期。备份需停止包装服务后一起备份其 PostgreSQL 数据库和
state_dir，再恢复两者；此包装服务备份流程尚未做恢复演练。

对已核实停止的 unknown，管理员可记录核对事实：

```sh
uv run eyes-agent --config agent-service.toml resolve RUN_UUID \
  --status unknown --stopped-confirmed --reason '填写实际核对依据'
```

这会释放包装服务容量，但保留结果未知。确认成功时使用 `--status succeeded --output output.json`；
其他终态同理。命令只接受 unknown，保留前一状态、结果和核对理由，不能改写已经确定的终态。

## 14. Runner 原运行核对

Runner 使用单独一个连接的恢复客户端，每 `reconcile_interval_seconds`（默认 10 秒）
检查平台预留及本地持久日志。只按原幂等键查询，不提交新的运行；404 或连接失败保留
未解决状态。平台要求停止或 deadline 已过时，按目标实际能力请求取消。
确定停止后的远端观察提交到独立核对接口，迟到事件与产物继续进入原 Attempt 的证据链。
停止已确认、结果未知时只释放容量；确定成功后按原评分规则创建固定清单与评分任务。
后补证据形成新清单，不替换已发布评分，超过证据等待窗口仍需显式重新评分。

核对使用本地 assignment 与原配置中的 secret_refs；日志丢失时不会猜测请求或凭据，
保留待处理预留。`remote-observation.json`、`reconciled.json` 和错误记录保留在隔离目录供排障。
这一恢复路径已有代码，但进程/网络中断后的端到端行为仍待运行验证。

容量拒绝的客户端行为：官方包装服务对未接受任务的 `429 capacity_exceeded` 返回
`Retry-After: 1`。客户端支持秒数或 HTTP-date，在原任务截止时间内等待，再复用原请求与幂等键；
未给出该响应头时按 1 秒处理。等待期间观察 Runner 停止/取消信号，不在取消后继续提交。
若等待预算耗尽，保留明确拒绝原因，记为执行失败并释放额度；不把“明确未接受”记作未知执行。
传输错误、服务端错误及不明确的协议冲突仍沿用原幂等键查询与保守 unknown 规则。
此容量拒绝分支已完成代码核对，尚未用真实 Agent 占满容量验证。
