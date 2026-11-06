# RabbitMQ 异步测试事件与存储验证

> 此页记录先前的事件链路与缓存实测。用户随后明确要求最终执行结果进入 RabbitMQ，当前默认接入见 [结果队列说明](result-queue.md)，迁移已更新为 `0010_result_submissions`，事件异步消费改为可选。

> 2026-11-06 汇总缓存改为固定 key、数据库提交后通过 Outbox 删除缓存，详见
> [当前缓存说明](backend.md#汇总缓存先更新数据库再删除缓存2026-11-06)。下文版本缓存的验证与性能数据属于旧实现，不代表新版已通过同样验证。

2026-11-05，用户授权将 RabbitMQ 用于服务端异步接收测试事件，消费后写入 PostgreSQL；Runner 保留本地暂存，Redis 继续负责查询缓存。本文记录当前实现和本地实测，不代表生产容量或真实 Agent 全链路验收。

## 数据链路与确认语义

```mermaid
flowchart LR
    R[Runner 本地持久化队列] --> A[认证 API]
    A --> Q[RabbitMQ 持久 quorum 队列]
    Q --> C[eyes-event-consumer]
    C --> P[(PostgreSQL 事件与回执)]
    P --> G[回执查询]
    G --> R
    P --> S[实验汇总计算]
    S --> Cache[(Redis 版本缓存)]
```

- `POST /v1/event-submissions` 接收现有 EventBatch，先校验 Runner 身份和 Attempt 归属；在 PostgreSQL 登记内容摘要与 pending 回执，再在数据库事务外发布持久消息。发布使用 mandatory 与 publisher confirms，队列满或发布失败返回 503，Runner 保留本地事件重试。回执表不保存事件正文。
- 相同 Runner 凭据和规范化内容生成相同回执 ID。重复提交 pending 回执可能再次投递，属于至少一次传输；不承诺消息只投递一次。
- 独立消费者在 PostgreSQL 中锁定回执，重新检查当前凭据与证据权限，复用 `evidence.ingest` 的事件去重、冲突检查和脱敏。事件与 persisted 回执同事务提交，之后 ACK。提交后、ACK 前断连会重投；已完成回执直接确认，避免重复写入。
- 消费期间数据库错误关闭连接并保留未确认消息，消费者等待两秒后重新连接；单个消费者每次处理一个批次。合法消息不受 quorum 默认投递次数上限淘汰，永久业务错误保存 rejected 回执及原始错误码。非法格式或摘要不匹配的 broker 消息拒绝到 `.rejected` 队列并记录堆栈，需人工检查。
- `GET /v1/event-submissions/{id}` 限制到原 Runner 凭据。Runner 只在 persisted 后删除本地文件；pending 继续等待，rejected 移到本地拒绝目录并保留原因。这样证据封存不会仅因 broker 接收成功而提前进行。
- 原 `POST /v1/events` 保持同步持久化语义。任务调度、完成上报、文件产物、评分与被动观测接口未迁入 RabbitMQ；任务并发仍由执行槽位和调度额度控制。

Publisher confirms 只确认 broker 接收，消费 ACK 确认处理结束，两者作用不同，参见 [RabbitMQ 官方确认语义](https://www.rabbitmq.com/docs/confirms)。队列、消息持久化与确认的组合参见 [官方 Work Queues 教程](https://www.rabbitmq.com/tutorials/tutorial-two-python)。

## 启动与配置

依赖已锁定到 Pika 1.4.4，Python 仍为 >=3.14。迁移为 `0009_event_submissions`。

```sh
uv sync --frozen
uv run alembic upgrade head
# 配置 EYES_RABBITMQ_URL，再分别启动 API 与消费者。
uv run eyes-server
# 另一个终端：
uv run eyes-event-consumer
```

`eyes-event-consumer --once` 最多消费一个批次，适合检查；常驻服务不传该参数。RabbitMQ URL 只配置在服务端，Runner 仍通过 HTTP 认证访问 API。

在 Runner TOML 中启用：

```toml
event_delivery = "queued"
```

示例配置已启用 queued。旧配置未设置此字段时仍为 direct，升级不会自动改变已有 Runner 的上报方式。本地队列里已有同步任务也继续使用其保存的原接口路径。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `EYES_RABBITMQ_URL` | 未配置 | 不启用新提交接口，返回明确 503 |
| `EYES_EVENT_QUEUE` | `eyes.events.v1` | 隔离应用/环境的队列名 |
| `EYES_EVENT_QUEUE_MAX_BYTES` | 268435456 | 256 MiB 消息正文上限，超限拒绝新发布 |
| `EYES_EVENT_PUBLISH_CONCURRENCY` | 4 | 每 API 进程最多四个并行发布 |
| `EYES_EVENT_BROKER_TIMEOUT_SECONDS` | 3 | socket/阻塞连接超时；连接建立总预算另加一秒，心跳 30 秒 |

发布器每次独占一个短生命周期连接，避免跨线程共享 Pika BlockingConnection。该实现优先控制连接预算和清晰确认边界，尚未做事件吞吐压测。调整已有队列参数需要按 RabbitMQ 队列声明约束迁移队列，不能直接以不同参数重声明。

Compose 新增 RabbitMQ 持久卷和 event-consumer 服务，RabbitMQ 镜像为 `rabbitmq:4.4.0-management-alpine`，不暴露宿主机端口。配置独立十六进制 `RABBITMQ_PASSWORD` 后运行原 Compose 启动命令。单节点 quorum 队列不代表跨节点高可用。

备份前暂停新上报、排空 pending 回执。现有数据库/产物备份不包含 broker 正文，backup 发现 pending 会拒绝；迁移降级也要求先排空 pending。回执元数据暂无自动清理，事件正文继续受既有保留规则控制。积压期间证据等待期限仍生效，不能把已入队但尚未入库的事件算作完整评分证据。

## 本地验证环境与方法

- macOS ARM64、CPython 3.14.7；隔离 Docker PostgreSQL 18.6、Redis 7.4.9、RabbitMQ 4.4.0。
- API 为本机单进程 Uvicorn，两个实例分别关闭/启用 Redis，其他缓存测试参数一致：数据库连接池 8、overflow 0、汇总计算并发上限 4。
- 数据为明确标记的合成存储记录：1,000 CaseRun、1,000 Attempt、1,000 ScoreRun；800 pass、200 fail。未运行真实 Agent 或模型，不把这些分数当作 Agent 效果。
- 测量公开 `GET /v1/experiments/{id}/results`，包含 HTTP、Bearer 鉴权、资源版本查询、汇总计算或 Redis 读取、响应序列化；不含浏览器渲染、SSE 或 Agent 执行耗时。
- ABBA 顺序：无缓存 A1、预热缓存 B1、预热缓存 B2、无缓存 A2。每阶段先做 10 次不计入统计的预热，再以固定 4 并发闭环发送 200 请求，合计 800 个计时样本。并非固定 RPS 的开放负载，不能把观测吞吐当容量上限。
- 两端均使用相同 PostgreSQL 数据；这里的“无缓存”仅指无 Redis 汇总缓存，数据库/操作系统缓存仍经过预热。B 使用 3,600 秒 TTL 保持阶段内热缓存，生产默认 TTL 仍为 60 秒。
- P50/P95 对原始逐请求耗时使用 nearest-rank 计算；每次请求同时校验计划数、成功执行数和通过评分数。

| 阶段 | 请求数 | P50 | P95 | 错误数 | 新增汇总计算 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A1 无 Redis | 200 | 141.45 ms | 161.00 ms | 0 | 200 |
| B1 预热 Redis | 200 | 12.84 ms | 16.34 ms | 0 | 0 |
| B2 预热 Redis | 200 | 12.61 ms | 16.00 ms | 0 | 0 |
| A2 无 Redis | 200 | 139.92 ms | 158.26 ms | 0 | 200 |

按 A1/B1、A2/B2 配对，接口 P95 分别降低 **89.85% / 89.89%**。两组缓存测量各命中 200 次。`database_reads` 计的是汇总计算次数，命中时鉴权、项目归属和资源版本仍访问 PostgreSQL，因此不能写成“数据库访问减少 100%”。

## 功能与故障检查

| 场景 | 已观察结果 |
| --- | --- |
| 发布确认与持久化区分 | 提交后 pending，消费前事件表无记录 |
| 重复提交、提交后未 ACK | 同内容返回同回执；数据库提交后关闭未 ACK 通道再消费，最终只有一条事件 |
| 同事件标识不同正文 | 回执 rejected、错误码 event_conflict，原事件不覆盖 |
| Runner 回执门槛与 broker 重启 | 入队后本地事件仍 pending；RabbitMQ 重启后消费，重新构造 Runner Outbox 并查询 persisted 后才清除本地文件 |
| broker 停机 | 新发布返回 503，重启后原内容重试成功 |
| 消费时数据库停机 | 消费事务失败，通道关闭后重投；数据库恢复后成功持久化 |
| 独立消费者进程 | 实际 `python -m eyes.server.event_queue.worker` 消费含 100 个事件的批次，全部入库；加前序 3 个事件共 103 条 |
| 认证、Schema 与归属 | 未认证 401、非法请求 422、其他 Runner 的提交/回执读取 403 |
| 批次原子性 | 批次中后续事件冲突时，前面已插入的事件一并回滚，保存 rejected 回执 |
| 消费前撤销凭据 | 消费拒绝并保留 invalid_runner 原因 |
| 非法 broker 消息 | 异步转入 rejected 队列，经 broker 重新声明查询确认 |
| pending 备份门槛 | 有待入库事件时拒绝数据库/产物备份 |
| 缓存版本失效 | 通过应用事务更新实验状态后，下次读取返回新状态和新 computed_at |
| Redis 停机顺序读 | 连续两次返回 200 且状态正确 |
| Redis 停机突发读 | 16 并发、32 请求，11 次 200、21 次 503 summary_busy；体现受限回源，不是全部请求成功 |
| Redis 恢复 | 冷却结束后重新填充当前版本缓存，下一请求命中 |

消费者数据库提交后未 ACK 的窗口通过实际 AMQP 通道关闭模拟；数据库和 broker 停机通过隔离容器停止/启动完成。此处不是操作系统断电、磁盘损坏、多节点故障或全网网络分区验收。

## 可复现脚本与原始记录

- [存储验证脚本](../scripts/validate_storage.py)：事件链路、故障注入、缓存 ABBA 和逐请求 CSV。
- [边界验证脚本](../scripts/validate_event_boundaries.py)：认证、跨 Runner 权限、批次冲突回滚、撤销凭据、非法消息与备份边界。
- 本地原始记录（Git 忽略）：`data/storage-validation-20261105/run1/report.json`、`run1/requests.csv`，新消费者进程验证为 `events-final/report.json`，边界检查为 `boundaries-final/report.json`。包含服务日志，不保存明文认证令牌。

在专用隔离环境复现（不得指向业务库）：

```sh
docker run -d --name eyes-mq-validation-pg -e POSTGRES_USER=eyes -e POSTGRES_PASSWORD=eyes -e POSTGRES_DB=eyes -p 127.0.0.1:55439:5432 postgres:18.6
docker run -d --name eyes-mq-validation-redis -p 127.0.0.1:56389:6379 redis:7.4.9-alpine
docker run -d --name eyes-mq-validation-rabbit -e RABBITMQ_DEFAULT_USER=eyes -e RABBITMQ_DEFAULT_PASS=eyes -p 127.0.0.1:55679:5672 rabbitmq:4.4.0-management-alpine
export EYES_DATABASE_URL='postgresql+psycopg://eyes:eyes@127.0.0.1:55439/eyes'
export EYES_REDIS_URL='redis://127.0.0.1:56389/0'
export EYES_RABBITMQ_URL='amqp://eyes:eyes@127.0.0.1:55679/%2F'
export EYES_EVENT_QUEUE='eyes.validation.unique-run-name'
uv run alembic upgrade head
uv run python scripts/validate_storage.py --output data/storage-validation/new-run --redis-container eyes-mq-validation-redis --rabbit-container eyes-mq-validation-rabbit --postgres-container eyes-mq-validation-pg
# 使用新的空队列和新输出目录运行额外边界检查。
export EYES_EVENT_QUEUE='eyes.validation.unique-boundaries'
uv run python scripts/validate_event_boundaries.py --output data/storage-validation/new-boundaries
```

脚本以 58081/58082 启动实际 HTTP API，结束后停止 API 和消费者进程；用于故障检查的三个容器需由调用者停止。第一次运行前等待基础服务就绪。当前运行只验证本地服务，Compose 配置及 Python 构建已检查；没有生产部署或真实 Agent 业务执行。

## 2026-11-05 Locust 复测

按用户要求，用 Locust 2.47.0 的 `HttpUser` 重测实验汇总接口。负载脚本为 [locust_summary.py](../scripts/locust_summary.py)，环境准备和分阶段运行脚本为 [benchmark_summary_locust.py](../scripts/benchmark_summary_locust.py)。本轮未修改应用代码、项目依赖或简历。

### 方法与环境

- 专用数据库 `eyes_locust_20261105`，迁移 `0010_result_submissions`；macOS ARM64、API Python 3.14.7、PostgreSQL 18.6、Redis 7.4.9。Locust 运行于独立的 Python 3.13.14 工具环境，不改变项目 Python 版本。
- 两个单进程 API 访问同一份数据：一个关闭 Redis 缓存，一个开启；各自数据库连接池为 8、overflow 为 0、汇总计算额度为 4。
- 固定已完成实验，包含 1,000 条合成 CaseRun/Attempt/ScoreRun，800 pass、200 fail。每次成功响应均核对计划数、执行成功数与评分通过数。未运行 Agent/模型，不包含浏览器渲染或 SSE。
- 测量 `GET /v1/experiments/{id}/results` 的客户端 HTTP 响应时间，包含鉴权、数据库/缓存访问及服务端序列化。Locust `HttpUser` 无思考时间，每用户同一时刻一个请求，属于闭环负载；用户数不是固定 RPS。
- 每阶段先运行 10 秒预热，再清空 Locust 统计并开始记录原始请求。4 用户按 A1→B1→B2→A2 顺序，每阶段正式测量约 60 秒；10、20 用户各按 A→B 顺序，每阶段约 30 秒。结束时允许在途请求完成。
- 缓存 TTL 为 3,600 秒以保持热缓存，与前次对照一致；应用默认值仍为 60 秒。单独首次冷缓存请求为 65.49ms，仅一个样本且为单请求，不能作为冷缓存 P95 或与 4 用户 P95 直接比较。

### 结果

下表 P95 使用 Locust 原生分桶统计；失败率分母为该阶段全部正式请求。高负载无缓存组的 P95 包含失败请求，应同时查看失败率。

| 阶段 | 用户数 | 模式 | 请求数 | 失败数/率 | P95 | 实际 RPS |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| A1 | 4 | 无 Redis | 1,731 | 0 / 0% | 170ms | 28.86 |
| B1 | 4 | 热缓存 | 19,236 | 0 / 0% | 14ms | 321.30 |
| B2 | 4 | 热缓存 | 19,234 | 0 / 0% | 14ms | 321.11 |
| A2 | 4 | 无 Redis | 1,723 | 0 / 0% | 170ms | 28.72 |
| A10 | 10 | 无 Redis | 1,815 | 1,096 / 60.39% | 290ms | 60.42 |
| B10 | 10 | 热缓存 | 8,926 | 0 / 0% | 41ms | 298.46 |
| A20 | 20 | 无 Redis | 1,776 | 1,058 / 59.57% | 560ms | 58.79 |
| B20 | 20 | 热缓存 | 8,526 | 0 / 0% | 100ms | 285.18 |

共记录 62,967 次正式请求。4 用户两组对照均为 170ms→14ms，按原生分桶值计算降幅约 91.8%；逐请求原始数据的成功请求 nearest-rank P95 分别为 A1 168.96ms、B1 14.38ms、B2 14.17ms、A2 168.16ms，细微差异来自统计分桶与分位数算法，不混用为同一精度。

A10/A20 的失败响应均为 HTTP 503，服务端同时记录汇总预算拒绝；仅成功请求的原始 P95 分别为 308.65ms / 619.66ms。其总 RPS 包含快速失败，不能当成成功吞吐。缓存各阶段的汇总计算增量为 0；鉴权、归属及版本查询仍访问数据库。缓存计数器增量包含预热，正式请求数不包含预热，二者不能直接作为同一分母比较。

更高用户数下缓存组的吞吐没有继续提升，延迟反而增大。因此本轮仅支持“固定数据热缓存能减少汇总计算、加快读取”的结论，不提供容量上限、线上 SLA、动态实验更新速度或长期稳定性承诺。

### 报告与复现

本地 Git 忽略目录 `data/locust-summary-20261105/run1/` 保存：

- `report.json`：完整方法、各阶段最终统计、缓存计数器及实际 Locust 命令。
- `A1.html` 至 `B20.html`：每阶段 Locust 原生最终 HTML 报告。
- `*.requests.csv`：正式阶段逐请求耗时、HTTP 状态及错误；`*.measurement.json`：结束时的最终统计。
- `*_stats.csv`、`*_stats_history.csv`、`*_failures.csv`：Locust 原生周期快照；周期 CSV 的最后一次刷新可能早于停止时间，最终总数以 HTML、measurement JSON 和逐请求 CSV 为准，三者已核对一致。
- `benchmark.log`、阶段日志、API 日志和 `source-manifest.json`：运行过程、Git HEAD 和当前源码 SHA256。

启动专用 PostgreSQL/Redis 验证容器、创建独立数据库并迁移后，设置对应 `EYES_DATABASE_URL`、`EYES_REDIS_URL` 和 `EYES_RABBITMQ_URL`，运行：

```sh
uv run python scripts/benchmark_summary_locust.py --output data/locust-summary-20261105/new-run
```

该脚本复用已有 API 启动帮助函数，要求提供 RabbitMQ URL，但本轮不发布消息且不需要启动 broker。Locust 通过 `uv tool run --python 3.13 --from locust==2.47.0` 启动。必须使用全新输出目录，脚本自动生成新的实验记录，不清空共享数据或缓存。测试结束后已停止两个 API、Locust 进程及本轮启动的 PostgreSQL/Redis 验证容器，数据库与报告保留。
