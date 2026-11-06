# 控制后端开发说明

当前实现 Eyes 的控制后端基础及配套 Runner。Agent 端入口、HTTP 协议、SDK 与评分器说明见 [接入文档](agent-integration.md)。源码按 AGENT.md 的模块边界组织；HTTP 路由负责认证和协议转换，领域模块负责校验、状态及事务。当前不具备 M1 真实闭环验收结果，具体证据见 [验证记录](backend-validation.md)。

## 当前实现与后续工作

| 范围 | 当前情况 |
| --- | --- |
| 公共协议 | 目标、JSONL 用例、评分、事件与产物的 Pydantic 模型及 JSON Schema |
| 基础认证 | 项目管理、只读和 Runner 令牌；数据库保存 SHA-256 摘要；可撤销 |
| 版本与实验 | 发布不可变版本；冻结实验；幂等创建；同事务创建用例运行和工作项 |
| 调度 | 主动领取、插件匹配、执行/评分容量、租约、心跳、令牌校验、取消及到期扫描 |
| 证据 | 事件去重、冲突诊断、固定清单、迟到清单、受控产物上传及查询 |
| 评分 | 固定清单绑定、前置证据检查、结果校验、重新评分历史及明确分母的结果查询 |
| 部署 | Alembic 初始迁移、配置样例、Compose 的 PostgreSQL/API/调度进程/迁移服务 |
| 实际执行组件 | 宿主机 Runner、HTTP/Python 适配器、独立规则/Python 评分进程、SDK、插件摘要与本地状态 CLI 已实现；真实目标联调暂缓 |
| Web 控制台 | React/TypeScript/Vite 页面及公开 API 操作已有实现，结果比较为事实并列视图；验证边界见 [前端说明](frontend.md) |
| 回归与运维 | 固定回归报告、质量门槛、开发者 CLI、证据等待/封存、保留删除/文件回收、审计及备份恢复已有实现；用法见 [平台说明](platform.md) |

本轮 PostgreSQL 验证覆盖迁移、报告持久化、排队取消和数据库备份恢复，见 [平台验证记录](platform-validation.md)。多 Runner、活动任务取消、含执行证据的恢复及真实 Agent 链路仍待验收。

## 本机运行

需要 Python >=3.14、uv 和可连接的 PostgreSQL。依赖锁定在根目录 `uv.lock`；本次在 macOS ARM64、CPython 3.14.7 上安装成功。

```sh
uv sync --frozen
cp .env.example .env
```

编辑 `.env`，设置 `EYES_DATABASE_URL=postgresql+psycopg://用户名:密码@主机:端口/数据库`。特殊字符需按 URL 规则编码。服务只接受 PostgreSQL/psycopg 连接。

```sh
uv run alembic upgrade head
uv run eyes-admin bootstrap --name default
uv run eyes-server
```

`bootstrap` 创建一个项目并输出 `project_id`、`credential_id`、管理 `token`。输出发生在事务提交后，令牌只能在签发时取得。保存该令牌，使用 `Authorization: Bearer <token>` 调用业务 API。

另一个终端使用相同配置运行：

```sh
uv run eyes-scheduler
```

`uv run eyes-scheduler --once` 执行一次到期扫描。默认每 5 秒扫描；租约 60 秒、停止请求协调期限 60 秒；全局执行容量 16、评分容量 4。项目默认执行容量 8、评分容量 2。这些是开发默认值，尚无容量实测依据。API 和调度进程必须使用一致的配置及数据库。

API 默认监听 `127.0.0.1:8000`。更改监听参数可直接运行：

```sh
uv run uvicorn eyes.server.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`/docs` 和 `/openapi.json` 提供接口说明。`/health/live` 检查进程；`/health/ready` 检查数据库连接和迁移版本，未就绪返回 503。就绪检查同时验证产物卷可写性。

## Compose

```sh
cp deploy/.env.example deploy/.env
openssl rand -hex 32
```

分别生成十六进制密码，写入 `deploy/.env` 的 `POSTGRES_PASSWORD` 和 `RABBITMQ_PASSWORD`，然后运行：

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yaml exec api eyes-admin bootstrap --name default
```

Compose 等待 PostgreSQL 健康后执行迁移，再启动 API 和调度进程。相关依赖条件见 [Docker Compose 文档](https://docs.docker.com/compose/how-tos/startup-order)。PostgreSQL 18 的持久卷挂载在 `/var/lib/postgresql`，与 [官方镜像的目录约定](https://hub.docker.com/_/postgres)一致。

数据库和 API 端口都只映射到本机。数据库与证据分别使用独立数据卷。当前 Compose 包含控制端和 Web 控制台，本机前端端口为 8080；Runner 在目标宿主机独立运行。本轮部署的实际验证范围见 [平台验证记录](platform-validation.md)。

## 令牌及项目范围

| 角色 | 权限 |
| --- | --- |
| read | 查询版本、实验、结果、证据及运行状态 |
| manage | 读取、发布版本、导入数据集、创建/取消实验、核对未知状态、重新评分 |
| runner | 注册、领取授权工作、续租、上报；按获授权的 Attempt 上传证据 |

签发只读令牌：

```sh
uv run eyes-admin issue-token --project-id <项目UUID> --role read
```

签发仅允许某个目标版本的 Runner 令牌：

```sh
uv run eyes-admin issue-token --project-id <项目UUID> --role runner \
  --target-id <TargetVersion UUID> --work-kind execute --work-kind score
```

`--target-id` 可重复传入；允许项目所有目标版本时显式使用 `--all-targets`。Runner 令牌必须指定工作类型。首版一份 Runner 凭据对应一个注册身份，多 Runner 使用不同凭据。令牌撤销命令为：

```sh
uv run eyes-admin revoke-token --credential-id <凭据UUID>
```

项目身份从令牌取得，API 请求不接受客户端指定的 `project_id`。Runner 通过 API 工作，不连接数据库。评分 Runner 可读取其获授权评分清单中的产物；执行 Runner 可访问自己执行的 Attempt 的产物。

## 公共契约

模型位于 `src/eyes/contracts/`，不依赖后端 ORM。协议当前为 `1.0`，缺省 `schema_version` 按 `1.0` 解析，其他版本返回校验错误；未知字段被拒绝。`GET /v1/contracts` 返回四类协议的 JSON Schema。

目标版本的 `name` 是项目内稳定的目标名称。发布相同名称会建立新 TargetVersion，并关联同一 Target 容量池。它们共享固定的 `concurrency_limit`；此轮没有容量编辑接口，改变同名目标的容量会返回 409。接入方须用同一名称表示同一目标。并发大于 1 要同时声明会话和环境隔离。

目标协议包含 `capabilities/prepare/execute/cleanup`；可选取消与状态核对由能力声明控制。`adapter` 和评分器 `plugin` 是 Runner 上已安装插件的标识。领取时匹配已注册的 Python 别名/能力和 HTTP 地址 origin，本机 TOML 配置可信入口与地址白名单；评分运行前由 Runner 核对实现摘要。敏感头值使用 `secret_refs`，发布配置拒绝已知敏感字段，包含 `X-API-Key` 等常用头名。

测试集通过 `DatasetImport.jsonl` 整体导入。每行格式例如：

```json
{"schema_version":"1.0","case_id":"answer-001","input":{"message":"说明该项目的用途"},"expectations":{"required_terms":["Agent"]},"tags":["smoke"],"environment":{},"artifact_requirements":[],"steps":[]}
```

`input` 是结构化对象。支持预期、标签、产物要求、准备逻辑版本和外部环境限制；具体产物内容或业务状态由已安装评分器判断。空行、无效 JSON、重复 `case_id`、错误字段和非空 `steps` 都报告行号，整个版本不发布。当前仅支持单轮。

ScorerVersion 冻结插件标识、实现摘要、配置、证据要求、超时及可选数值语义。评分状态和质量结论分别记录；数值范围、方向、阈值及结论一致性由后端校验。规则与可信 Python 评分器在 Runner 的独立进程中执行。

执行事件包含生产者、序号、发生时间、Attempt、trace/span、来源及数据。生产者序号仅用于该生产者的顺序；API 列表按接收时间和记录 ID 返回，不表示全局执行顺序。外部 trace 须通过 `linked_trace_ids` 显式链接该 Attempt 的执行 trace。

## API 清单

除健康检查、OpenAPI 和交互文档外，下面接口均需对应角色的 Bearer 令牌。

| 操作 | API | 角色 |
| --- | --- | --- |
| 发布目标/评分器 | `POST /v1/targets`、`POST /v1/scorers` | manage |
| 整体导入 JSONL | `POST /v1/datasets/import` | manage |
| 版本列表与详情 | `GET /v1/catalog/{targets,datasets,scorers}[/{id}]` | read/manage |
| 契约 Schema | `GET /v1/contracts` | read/manage |
| 创建实验 | `POST /v1/experiments`，必需 `Idempotency-Key` | manage |
| 实验列表/详情 | `GET /v1/experiments[/{id}]` | read/manage |
| 汇总及用例详情 | `GET /v1/experiments/{id}/results`、`/case-runs` | read/manage |
| 取消实验 | `POST /v1/experiments/{id}/cancel` | manage |
| 注册与领取 | `POST /v1/runners/register`、`POST /v1/work/claim` | runner |
| 固定评分输入 | `POST /v1/work/{id}/score-input`（身份与有效 lease token） | runner |
| 心跳与进度 | `POST /v1/work/{id}/heartbeat`、`/progress` | runner |
| 执行/评分结果 | `POST /v1/work/{id}/execution-result`、`/score-result` | runner |
| 未知执行核对 | `POST /v1/attempts/{id}/resolve` | manage |
| 新建评分记录 | `POST /v1/attempts/{id}/rescore` | manage |
| Attempt 与核对来源 | `GET /v1/attempts/{id}` | read/manage |
| 事件批量接收 | `POST /v1/events` | runner |
| 事件及清单 | `GET /v1/attempts/{id}/events`、`/manifests` | read/manage |
| 初始化产物 | `POST /v1/attempts/{id}/artifacts` | runner |
| 上传/下载产物内容 | `PUT/GET /v1/artifacts/{id}/content` | 授权 runner；下载亦允许 read/manage |
| 工作状态及 Runner 心跳 | `GET /v1/operations` | read/manage |

列表接口采用 `limit/offset`，默认 50 条（事件 100 条），上限 200。未找到项目内记录返回 404，权限不足 403，冲突 409，契约错误 422，载荷超限 413，数据库不可用 503。事件接收和状态变更在事务提交后才确认。

## Runner 必须遵守的执行顺序

1. 注册 `supported_schema_versions`、可执行的 `adapters/scorers`、Python 别名及能力 `python_agents` 和规范化 `http_origins`。收到协商版本和 `runner_id` 后领取兼容的 `execute` 或 `score` 工作；无可领取工作返回 `work: null`。
2. 保存工作 ID、领取令牌和有效期。在有效期内持续心跳；所有状态上报带 `runner_id/lease_token`。
3. 执行工作先上报 `preparing`，完成准备后上报 `running`。只有服务端确认 `running` 写入后才能调用目标，这条确认是执行意图边界。
4. 使用分配的独立上下文、截止时间、幂等键和 trace context；上传事件与产物，并执行清理。
5. 上报 `ExecutionResult`，明确输出/错误、清理状态、证据状态和 `dropped_events`。清理当前随最终执行上报一次；后续独立清理记录接口待实现。
6. 评分领取获得小型 `ScoreAssignment`，通过有效租约读取完整固定 `ScoreInput`，使用私有只读文件传入独立进程；输入默认上限 32 MiB，独立于进程控制消息上限。评分返回时记录 `completed_at`，落实本地超时及清理后上报 `ScoreOutput`。Runner 已实现进程组控制、墙钟超时及可配置 POSIX 资源限制，具体范围见接入文档。

同一次操作的传输重试复用幂等键；新业务执行使用新 Attempt。目标声明不支持幂等时不得自动重复未知调用。Runner 也必须实际落实其声明的隔离及插件行为，注册能力不是执行证明。

## 状态与恢复

| 触发 | 控制端行为 |
| --- | --- |
| 正常执行 | `claimed → preparing → running → succeeded/failed` |
| 取消排队工作 | 取消该工作及用例运行，不制造实际执行 Attempt |
| 取消活动执行或截止到达 | 请求停止；心跳返回 `cancel_requested` |
| 上报 cancelled/timed_out | 必须声明已确认目标停止 |
| 执行意图之前失联 | 保留失败 Attempt；允许新工作重新排队，默认最多 3 个准备尝试 |
| 执行意图之后租约失效 | `unknown`，旧令牌失效，额度继续占用 |
| 停止请求超过协调期限 | `unknown`，继续保留额度 |
| 已知未知状态的人工核对 | 管理角色提供停止确认及原因；追加来源记录，才释放额度 |
| 评分超时或失联 | 计算截止后停止评分，默认另有 60 秒提交按时结果/错误诊断；租约失效或提交窗口结束时记 error；显式 rescore 创建新记录 |
| 重复最终结果 | 同领取令牌、同内容返回原接收结果；不同内容返回冲突 |
| 旧 Runner 迟到状态 | 失效令牌不能修改调度；已授权 Attempt 的迟到事件仍可保存 |

领取、额度判断、令牌及恢复使用同一组按固定顺序获取的 PostgreSQL 事务 advisory lock。额度从 `reserved` 工作项计算；未知执行仍计入全局、项目和稳定目标的执行容量。符合条件的队列使用 `FOR UPDATE OF work_items SKIP LOCKED` 领取。初版将这些事务串行协调，吞吐需实测；不承诺公平性或外部副作用恰好一次执行。

完整评分输入读取只校验项目、身份、工作范围、有效租约与计算期限，不持有全局调度锁；只读取已分配的固定清单。`EYES_MAX_SCORE_INPUT_BYTES` 控制响应大小，Runner 使用相同的 `max_score_input_bytes`。API 与 scheduler 的 `EYES_SCORE_SUBMISSION_GRACE_SECONDS` 必须一致；提交宽限不延长计算期限，也不恢复已失效租约。完成结果与证据在 Runner 中分别发送，证据的重试不会阻塞结果发送锁。上述评分输入协议变更要求三类进程同步升级；最新平台功能另需 `0002_platform`，详见 [平台升级说明](platform.md) 及 [接入兼容性](agent-integration.md#6-兼容性与验收边界)。

数据库触发器保护版本、事件、清单、核对记录、实验配置、已发布评分和回归报告；事件与清单仅允许按严格过期规则清除载荷，保留原摘要与标识。Attempt 的核对会保留先前结果及来源，已固定清单保存自己的执行结果副本。

实验 `completed` 表示调度工作已结束；可能仍有执行失败、评分错误或证据不足。存在未知工作时为 `completed_with_unresolved`。结果查询同时展示计划运行数、成功数、评分数、通过数及未解决数，比例返回明确的 numerator/denominator。初版汇总选首个成功 Attempt 和其最早的 ScoreRun；重新评分的完整历史在用例详情中读取，汇总不会自动换成新分数。comparison 模块可选择固定 ScoreRun 并生成不可变回归报告。

## 证据、产物与敏感内容

事件按项目、生产者和 event_id 去重，对原始规范化内容计算摘要。相同 ID、不同内容返回 409 并记录不含载荷的诊断。输入输出、事件、产物均通过清单引用关联；清单冻结执行结果。后到事件或产物产生新清单，原评分继续引用原清单。未报告的丢弃数量为未知，不能默认当作零。

完成执行时按当前清单检查评分前置证据。新实验按 `evidence_wait_seconds` 等候缺失证据，到期仍不足则记录 `insufficient_evidence`。Runner 将待确认或被拒绝的证据标为 partial，迟到上传完成后通过 evidence-close 追加封存清单。超过服务端等待期的证据需显式重新评分。评分的引用必须属于其清单，引用的产物还需可读取。`sealed` 仅表示声明的采集范围封存。

产物以 Attempt 和完整元数据的摘要确定稳定 UUID，同内容登记重传复用已有记录；先以名称、大小和 SHA-256 登记为 pending，再向服务端指定的 UUID 上传二进制内容。服务端验证大小和摘要，写临时文件、fsync、发布后才登记 ready。原始文件名不参与存储路径，下载采用附件形式。文件已发布但事务未提交时可重传同一 pending 上传；`eyes-ops maintain` 提供未登记文件、遗留临时文件和长期 pending 的预览与回收，执行前保护活动工作和证据等待。

目标与评分器配置拒绝已知密钥字段及 URL 内嵌凭据，改用 `secret_refs` 保存运行环境引用。执行结果和事件递归掩码已知敏感字段。自由文本、用例输入和二进制产物需要生产者在上传前脱敏；当前没有通用秘密识别器。JSON 请求默认上限 8 MiB，产物默认 32 MiB。保留与删除策略由 `eyes-ops` 执行，默认不按年龄删除完整证据；范围见 [平台说明](platform.md)。

API 使用 OTel SDK 创建实际 HTTP 请求 span，接收 W3C Trace Context，并返回 `X-Eyes-Trace-Id`。设置 `EYES_TRACE_CONSOLE_EXPORT=true` 可将这些 span 输出到控制台。Attempt 和 ScoreRun 各有独立 trace context，Runner 创建实际执行根 span，评分根通过 OTel Link 关联执行。执行 span 进入证据接口，评分 trace 当前保留在本机。当前没有 OTLP exporter、查询后端或完整 Agent 内部链路证明。

## 迁移、备份与后续验收

迁移入口是 `alembic.ini` 和 `migrations/versions/`，当前 head 为 `0011_agent_pairings`。迁移只在显式命令/Compose migrate 服务中运行，API 启动不自动建表。升级前备份数据库和证据卷，并记录应用版本与迁移版本。恢复时需要检查每个 ready 产物的路径、大小和摘要，以及评分引用的清单。`eyes-ops backup/restore/audit` 提供可执行流程，命令和验证边界见 [平台说明](platform.md)。

已提供 `eyes-server`、`eyes-scheduler`、`eyes-admin`、`eyes-runner`、开发者 CLI `eyes` 和运维 CLI `eyes-ops`。新增回归、封存和评分重试端点见 [平台说明](platform.md)。

HTTP 与 Python 通用接入功能已有实现，按本次要求暂缓真实 Agent 联调。后续恢复验收时，在可运行的 PostgreSQL 环境执行迁移，接入两个真实 Agent，运行导入、领取、执行、证据查询和独立评分。之后再验证多 Runner 限额、租约过期、取消、迟到上报和评分故障。原始结果须按 [AGENT.md](../AGENT.md) 的阶段要求保存。

多 Agent 批次新增公开 API 和 CLI，在同一事务创建多份独立实验，详见 [多 Agent 批次](multi-agent-batches.md)。

## 连接预算与执行积压（2026-11-04）

每个控制进程建立一个 PostgreSQL Engine。`EYES_DATABASE_POOL_SIZE` 默认 8，
`EYES_DATABASE_MAX_OVERFLOW` 默认 0，`EYES_DATABASE_POOL_TIMEOUT_SECONDS` 默认 5 秒。
池耗尽返回 503 `database_busy`；数据库连接失败返回 503 `database_unavailable`。
两者保留原异常 cause。部署预算需相加 API worker、scheduler、维护命令各自的池上限，
另预留迁移和管理连接。连接上限不等同于 Agent 任务并发数。

`EYES_MAX_PENDING_EXECUTION_RUNS` 默认 100000，限制每个项目中 queued、claimed，以及仍
占用额度的 unknown 执行工作总数。已确认停止但结果未知的工作保留为 unknown，不再占积压。
创建实验在项目行锁内检查已有积压与新增用例数；超过返回 429
`execution_backlog_full`，details 给出 pending、incoming 和 limit。多 Agent 批次任一成员
超限时整批事务回滚。已成功创建请求的幂等重放仍返回原记录。该限制控制新的执行入队，
不会丢弃已接受任务，也不占用评分容量；内部准备重试在同一事务替换原工作，不增加净积压。

Runner 注册可声明 `execution_slots`、`score_slots`，服务端领取在现有调度锁内同时核对
该 Runner 的持久预留。重启后的未知工作仍占槽位，不能仅凭本地任务列表为空继续超配。
旧注册未声明槽位时维持原有全局、项目、目标和实验限制；新 Runner 总是提交槽位声明。
`GET /v1/operations` 的 `resource_limits` 返回当前服务配置；数据库池配置按进程解释，
不是整个部署的实时连接计数。

本轮现有检查：Ruff lint、受影响 Python 文件格式检查、`git diff --check`、Compose 配置解析。
独立 PostgreSQL 18.6 数据库从空库迁移到 `0004_experiment_batches`，API `/health/ready`
返回 ready，`eyes-scheduler --once` 完成空队列扫描。这些结果证明启动与迁移可用，
尚未证明池耗尽、并发入队或重启故障场景；未新增或修改测试文件。

标准协议 Runner 使用 `GET /v1/runners/{runner_id}/reservations` 查询自己持有的预留和
未确定结果；`POST /v1/work/{work_id}/reconcile-agent` 提交带版本的远端运行观察。
服务端验证 Runner、项目/目标范围、原 Attempt/CaseRun、远端运行 ID 和停止确认，
只允许核对未解决状态。来源保存在 ResolutionRecord 和新清单中，不修改旧评分。
Runner 注册后先上传本地待交付结果并完成一轮恢复清单核对，再开始领取；清单读取失败时等待重试。
尚未过期的旧租约继续保留服务端额度，进入 unknown 后由后续后台核对处理。
同 revision 同观察重放不生成重复核对，同 revision 内容冲突返回 409。
`Resolution.status` 新增 unknown，用于已确认停止但结果仍无法恢复的情形：释放额度，
保留 unknown 与未解决统计，不伪造成成功或失败。

## PostgreSQL 事务通知与 Redis 发布（2026-11-04）

`0005_realtime` 新增资源版本、通知 Outbox 和事件接收游标表。控制端 Session 在事务提交前
汇总实际变化，为实验、批次、Attempt、观测运行、目录和运维视图更新版本，同时写入
Outbox；提交失败时业务数据、版本和通知一起回滚。版本锁按稳定顺序获取，网络请求不在
事务内执行。事件游标单独存储，不修改不可变原始事件。

每个资源只保留一条待发布通知，多次变化合并为最新版本；原始事件仍全部保存在 PostgreSQL。
`eyes-publisher` 用短事务领取通知，事务外先删除实验汇总缓存，再发布 Redis Pub/Sub，
最后按领取 token 和版本确认；非实验通知不执行汇总缓存删除。
发布期间若资源再次变化，确认不会删除新版本。中断可能导致重复通知；Redis 没有订阅者时
也可完成 publish，因此“发布成功”不等于所有页面已收到。删除或发布失败保留原记录并有界退避。

```sh
uv run alembic upgrade head
# 设置 EYES_REDIS_URL 后，在独立进程启动：
uv run eyes-publisher
```

Compose 已加入 Redis 7.4.9 和 publisher；Redis 不暴露宿主机端口，配置 256 MiB 内存上限
与 allkeys-lru。发布进程默认各最多两条 PostgreSQL/Redis 连接。Redis 只承载可重建通知和查询缓存，
不是任务、租约、额度或证据的权威来源。`/v1/operations.notifications` 提供待发布资源数、
最早积压时间和最大重试次数；这些值按当前项目过滤。

`GET /v1/realtime/{kind}/{resource_id}/events?after=0&limit=200` 提供接收顺序分页，kind 为
attempt 或 observed_run。调用者必须具有该项目的读取权限。响应包括 receive_version、
next_cursor、has_more 和资源版本；客户端补查使用 next_cursor，展示顺序仍可使用生产者
sequence。晚到的小 sequence 不会被接收游标跳过。历史事件在迁移时按存储顺序分配游标。
响应同时带有恢复代次 `epoch`；后续请求携带该值，数据库恢复后的代次变化会返回
`reset=true` 并从头读取。客户端此时替换旧事件列表，避免旧游标跳过恢复后的记录。
被动观测详情页已改为每次最多补查 200 条，并按 Agent 序号展示；“加载后续事件”继续补查，
通知与定期刷新复用已读取的游标。执行详情页也使用接收游标累积读取，并保留每页 50 条的展示；
跳转到尚未读取的页时按需补查，评分引用定位使用相同接收顺序。恢复后的 epoch 变化会清空
原游标前缀，避免把旧时间线事件混入恢复后的记录。

本轮已在独立 PostgreSQL 上从 0004 升级至 0005，并通过真实目标版本发布操作观察到
两条 Outbox 记录；`eyes-publisher --once` 发布后，Redis 订阅端收到 catalog/operations
版本通知，Outbox 归零。此证据只覆盖通知正常路径。随后升级至 0006，API 就绪与 SSE 认证/连接检查通过；
该阶段开发库没有实验数据；后续已验证排队实验的缓存命中、版本失效及 Redis 降级，见
[本轮实现对照](implementation-status.md)。并发一致性未完成运行验收。Redis 断开/恢复的检查见下文；
通知遗漏后的事件补齐和桌面浏览器验收仍待完成，不能据此声称实时产品链路已完成验收。

`GET /v1/realtime/stream` 通过 Bearer 认证建立 SSE。每个 API 进程使用一条 Redis 订阅连接，
限制 128 个客户端及每客户端 64 条待发通知；队列溢出要求重新查询。默认每 30 秒重新
验证凭据并要求补查，Redis 断开/重连也触发补查。前端合并 500 ms 内的查询失效通知，
每 30 秒保底刷新活动查询；已移除各页面原有的 2～15 秒重复轮询。令牌不进入 URL。

### 汇总缓存：先更新数据库，再删除缓存（2026-11-06）

实验 `/results` 使用项目、数据库时间线和实验 ID 组成的固定缓存键
`{namespace}:summary:v3:{epoch}:{project_id}:{experiment_id}`，不再包含资源版本号。
响应的 `computed_at` 记录实际计算统计的时间，缓存命中保留该时间，控制台显示统计更新时间。
每次查询先用短事务确认项目归属与数据库时间线，Redis I/O 不占用 PostgreSQL 事务。
未命中时在 REPEATABLE READ 快照内读取时间线和汇总，回填 Redis。缓存默认保留 60 秒，
每进程最多 4 个并发汇总计算，超出返回 `503 summary_busy`；Redis 连接失败后暂停
访问 5 秒，继续使用同一计算上限。可通过 `EYES_CACHE_TTL_SECONDS` 和
`EYES_CACHE_QUERY_CONCURRENCY` 调整。`0006_cache_epoch` 增加时间线标识，
`eyes-ops restore` 恢复后更换标识，避免恢复后的查询使用旧时间线缓存。
手工恢复数据库时，应在开放 API 前清空专用 Redis 缓存或更新 `cache_epoch.epoch`。

业务更新和实验 Outbox 记录在同一个 PostgreSQL 事务提交；`eyes-publisher`
默认每 0.5 秒轮询已提交记录，依次执行 Redis `DEL`、Pub/Sub 发布，再确认 Outbox。
删除失败会保留记录，按既有退避机制重试；删除成功后进程中断也可安全重复删除。
发布期间出现的新版本不会被旧批次确认清除，下一轮仍会再次删除缓存。
资源版本继续用于通知和事件补查，不再用于汇总缓存寻址。

这是异步最终一致性，不保证写接口返回时已完成删缓存。启用 Redis 缓存时需同时运行
`eyes-publisher`；进程停机或发生积压时缓存仍可能命中旧值，TTL 保留为兜底。
读请求在删除完成后仍可能回填此前快照的旧值，此时旧值可保留到回填后 TTL 到期，
因此 60 秒不是从业务提交开始计算的严格延迟上限。本次未增加分布式锁或延迟双删。

升级时 API 和 publisher 应一起更新；v3 key 与旧 v2 版本缓存隔离，旧 key 自然过期，
不需要清空整个 Redis。此改动无需数据库迁移。
本次已通过全仓 Ruff lint、涉及文件的格式检查和 diff 检查；未完成实时故障回归。
既有 `validate_storage.py` 的版本失效检查要求提交后立即读取新状态，未启动 publisher，
不适用于这次异步失效语义；本次没有修改该脚本，也不将旧缓存压测视为新版验证。

`GET /v1/operations` 的 `summary_cache` 提供当前 API 进程自 `since` 起的诊断计数：
`hits`/`misses` 是成功读取 Redis 后的命中/未命中；`bypasses` 是未配置 Redis 或处于
故障冷却期时跳过查询的次数；`cache_errors` 包括读取、解析及写入失败。
`database_reads` 记录获准开始的汇总计算次数（包含后续失败），`active_reads` 是当前占用的
计算槽位，`busy_rejections` 是因槽位耗尽拒绝的请求数。`hit_ratio` 仅以成功查询次数为分母，
尚无成功查询时返回 null。还提供计算并发上限和剩余冷却秒数。这些是进程级基础设施计数，
不含项目内容或凭据；进程重启后重置，多 API 部署时不能把单次响应当作全服务统计。
开发库上已确认该接口返回 200 和初始诊断结构；缓存命中与故障计数仍需有实验数据时验证。

实时链路的本地故障检查（2026-11-05）：在独立 PostgreSQL/Redis 环境建立真实 SSE 连接，
停止 Redis 后收到一次 `redis_unavailable`，之后只收到周期 `polling` 补查；恢复 Redis 后
收到 `redis_connected` 并回到 `live`。检查将补查周期设为 5 秒以便观察，部署默认仍为
30 秒。修复了重连失败每 2 秒重复通知、导致数据库刷新过频的问题。连接保持期间查询
`pg_stat_activity`，除查询本身外只有 idle 连接，没有 idle in transaction。
此检查覆盖 Redis 断开/恢复通知，不证明浏览器渲染、事件乱序或全链路吞吐。

### 进程内耗时诊断

`/v1/operations.runtime_metrics` 给出当前 API 进程的累计次数、失败次数，以及每项最近
最多 512 个样本的均值、P95 和最大值。无样本返回 null；P95 使用最近窗口的 nearest-rank，
不是全历史分位数，也不能视作负载测试结论。进程重启会重置，多个 API 进程不自动汇总。

- `database_checkout`：从连接池获取连接的总耗时，包含排队、必要的新建连接与 pre-ping，
  **不是纯排队时间**；失败计数包括获取超时或连接失败。
- `scheduling_lock`：已获取数据库连接后执行两个调度 advisory-lock SQL 的总耗时，
  包含锁等待及 SQL 往返，不包括之后持锁处理业务的时间。
- `http_response_headers`：进入请求边界至发送响应头，包含请求体接收；HTTP 4xx/5xx 计为失败。
  SSE 只计握手，不把长连接持续时间混入。没有发出响应头的断开或未处理异常不在此窗口中。
- `claim_response_headers`：相同响应头边界下的工作领取请求子集。

本地已确认服务启动、健康检查和运维接口返回实际的连接获取及响应头样本；空的领取/锁窗口
保持 null。未据此发布任何并发容量或性能提升数字。

独立证据保留期已接入：`EYES_EVENT_RETENTION_DAYS`、`EYES_ARTIFACT_RETENTION_DAYS`
分别控制事件正文和 ready 产物，默认 0，不改变原整体过期配置。维护预览新增 `events` 和
`artifacts` ID 列表；仍保护排队/活动/未知工作及等待评分的执行。部分清理生成新的 partial
清单，旧清单保持原摘要；引用已过期内容的旧评分重试返回 410，新评分按剩余证据判定。
使用新配置在独立开发库运行维护预览和 audit 均通过；库中无执行证据，本轮没有实施正文
删除或文件回收，不能据此宣称重新评分与部分清理已通过运行验收。


## RabbitMQ 异步事件入库（2026-11-05）

新增 `POST /v1/event-submissions` 与 `GET /v1/event-submissions/{id}`（Runner 认证），以及 `eyes-event-consumer` 进程。提交返回回执状态，pending 仅表示尚未确认 PostgreSQL 持久化，不能作为证据封存依据。原 `POST /v1/events` 同步接口保留。完整启动方式、重投与错误语义、存储性能数据见 [异步事件与存储验证](event-queue.md)。


## 最终执行结果队列（2026-11-05）

用户澄清 RabbitMQ 应接住 Python 执行完后的最终结果。新增 `POST /v1/work/{work_id}/result-submissions`、`GET /v1/result-submissions/{id}` 和 `eyes-result-consumer`。消费者复用 `scheduling.finish`，结果、状态、额度与回执同事务提交后 ACK；不可应用的结果转入确认式死信队列。示例 Runner 默认启用结果队列，过程事件默认 direct。配置、租约边界和实测见 [结果队列说明](result-queue.md)。
