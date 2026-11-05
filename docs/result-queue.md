# Python 最终执行结果 → RabbitMQ → PostgreSQL

本次按用户澄清，将 RabbitMQ 接到 Python 测试任务执行完后的最终结果上报入口。执行过程事件是另一条可选链路，默认示例已改为结果 queued、事件 direct。

## 运行流程

1. Python 子进程执行结束，Runner 构造 ExecutionResult（成功输出或失败原因），先保存 result.json 和本地待传任务。
2. Runner 使用认证 HTTP API 提交到 `POST /v1/work/{work_id}/result-submissions`。服务端核对 Runner、工作归属与领取令牌，登记不含结果正文的回执，在事务外向 RabbitMQ 发布持久消息，等待 publisher confirm。未确认则返回 503，本地任务保留重试。
3. `eyes-result-consumer` 消费独立结果队列，调用既有 `scheduling.finish`。结果、执行状态、证据清单、评分入队、并发额度变化以及 persisted 回执在同一 PostgreSQL 事务提交，提交成功后 ACK。
4. Runner 查询 `GET /v1/result-submissions/{id}`，确认 persisted 后才清除本地待传文件并生成 ack.json。这样本地证据收尾也不会把“broker 已接收”当成“结果已落库”。

相同凭据、work_id 和请求正文生成相同回执。重复投递由回执状态与既有 completion_digest/lease token 机制去重。数据库已提交但 ACK 丢失时可以安全重投。

Python 执行失败本身是合法结果：`status=failed` 与错误信息正常落库并 ACK，不会仅因测试失败而进入死信队列。

## 故障与死信

- RabbitMQ 不可用：生产者未取得确认，本地队列保留并重传。
- PostgreSQL 不可用：消费者不 ACK，连接关闭使消息重新入队，常驻消费者间隔两秒重连。
- 永久业务拒绝（例如当前租约已失效、结果与已保存结果冲突）：回执记录具体错误原因，原始结果消息拒绝到 `eyes.results.v1.rejected`。
- 结果队列及死信队列均为持久 quorum 队列，消息 delivery_mode=2；结果源队列使用 `x-dead-letter-strategy=at-least-once` 和 `x-overflow=reject-publish`。这使死信转移也使用确认机制，不将普通 DLX 当作天然无损传输。
- 源队列没有消息 TTL，暂时性故障重投不受默认 delivery-limit 淘汰。队列正文上限沿用共享的 256 MiB 配置，满队列拒绝新发布并让生产者保留本地副本。

发布确认与消费 ACK 分别覆盖生产者→broker、消费者→broker，见 [RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms)；确认式死信转移见 [RabbitMQ DLX safety](https://www.rabbitmq.com/docs/dlx#safety)。

既有租约和取消语义保持有效。入队后长时间积压、直到租约被回收的结果，不会绕过状态校验强行覆盖任务，而会保留在死信队列并显示 rejected 原因；它没有自动变成任务最终状态。此类结果需人工核对并使用现有明确的状态协调操作，当前未提供自动死信重放命令。

PostgreSQL/产物备份不包含 RabbitMQ 消息正文，存在 pending 结果时会拒绝备份。备份前暂停新结果上报并排空消费者；死信正文需要单独保管。单节点 quorum 不代表跨节点高可用。

## 配置与升级

迁移：`0010_result_submissions`，新增 `result_submissions` 回执表。已存在的事件回执表保持不变。

```sh
uv sync --frozen
uv run alembic upgrade head
# 服务端配置 EYES_RABBITMQ_URL；默认 EYES_RESULT_QUEUE=eyes.results.v1。
uv run eyes-result-consumer
```

Runner 示例：

```toml
result_delivery = "queued"
event_delivery = "direct"
```

旧配置未指定 result_delivery 时保持 direct，仍使用原 execution-result 接口。评分结果保持既有 score-result 接口。RabbitMQ 凭据仅在服务端配置，Runner 无需直连 broker。

Compose 默认新增 result-consumer；先前的 event-consumer 移到 `queued-events` profile。若需要异步事件消费，可使用 `--profile queued-events` 单独启用，并将 Runner 的 event_delivery 改为 queued。两个消费者共用镜像，各自消费独立队列。

发布器共享 `EYES_EVENT_QUEUE_MAX_BYTES`、`EYES_EVENT_PUBLISH_CONCURRENCY`、`EYES_EVENT_BROKER_TIMEOUT_SECONDS` 这组已有预算配置；并发额度是每个发布器实例独立计算。配置字段暂未重命名，以保留先前配置兼容性。

## 2026-11-05 本地验证

使用隔离 PostgreSQL 18.6、Redis 7.4.9、RabbitMQ 4.4.0；API 是实际本机 HTTP 服务，Python 为 3.14.7。任务调度上下文由明确标记的测试 fixture 创建，结果来自真实 Python 子进程，未调用真实 Agent 或模型。

| 场景 | 结果 |
| --- | --- |
| Python 计算 `sum(range(1, 101))` | 输出 5050，经 RabbitMQ 入库；任务 completed，额度释放 |
| 重复提交、数据库提交后未 ACK | 关闭实际未 ACK 通道再消费，输出不重复应用，证据清单只新增一版 |
| Python 抛出 ValueError | failed 结果和原异常文本正常入库，回执 persisted |
| RabbitMQ 停机 | Runner 发布得到 503，本地任务保留，恢复后重传成功 |
| PostgreSQL 停机 | 消费事务失败且未 ACK，恢复后重投成功；随后本地生成 ack.json |
| 租约失效并被调度器回收 | 回执 rejected/stale_lease，死信队列保留包含 5050 的完整结果正文 |
| 独立消费者进程 | 实际启动 `python -m eyes.server.result_queue.worker`，完成结果入库 |

故障检查同时发现并修复既有 sweep 的更新顺序问题：不能在 WorkItem 仍为 claimed 时先清空 lease_token，再触发 ORM 自动 flush。现在状态与令牌一起更新，避免 ck_work_claimed_lease 约束失败。

验证脚本：[validate_result_queue.py](../scripts/validate_result_queue.py)。原始结果位于 Git 忽略目录 `data/result-validation-20261105/run3/report.json`，包含 API/消费者日志和本地 Runner 队列记录。run1/run2 为保留的失败检查记录，不作为通过证据。

基础服务可复用 [存储验证的隔离启动命令](event-queue.md#可复现脚本与原始记录)。待基础服务就绪后设置独立队列、升级数据库并执行：

```sh
export EYES_RESULT_QUEUE='eyes.results.unique-validation-run'
uv run alembic upgrade head
uv run python scripts/validate_result_queue.py --output data/result-validation/new-run
```

脚本故障注入只操作 `eyes-mq-validation-rabbit` 和 `eyes-mq-validation-pg`，必须使用隔离测试服务与全新输出目录。检查结束后停止 API/消费者进程，基础容器由调用方停止。

先前缓存 ABBA 结果仍记录在 [缓存报告](event-queue.md)，本轮没有修改缓存实现或重新测量，因此不把原数据说成本轮重新测得。

## 2026-11-05 批量可靠性量化

专项脚本：[benchmark_result_delivery.py](../scripts/benchmark_result_delivery.py)。本轮只新增验证脚本，没有修改结果传输实现。

复用专用验证容器，创建独立数据库 `eyes_result_benchmark_20261105`（迁移 `0010_result_submissions`）和独立 quorum 队列 `eyes.results.benchmark.20261105.run1`。环境为 macOS ARM64、Python 3.14.7、PostgreSQL 18.6、RabbitMQ 4.4.0、Redis 7.4.9。每场景 100 条结果，来自真实 Python 子进程计算 `sum(range(1, 101))`；调度上下文为合成记录，没有调用真实 Agent/模型。

| 场景 | 注入与样本 | 核对结果 |
| --- | --- | --- |
| 重复提交及提交后 ACK 丢失 | 100 条结果各提交 3 次；消费 300 条消息并提交数据库后不 ACK，关闭通道，再确认实际重投 300 条 | 最终 100 个 persisted 回执、100 份匹配的结果、100 份证据清单；无重复应用，100 个任务完成且额度释放 |
| RabbitMQ 停机 | 停机后尝试发布 100 条本地待传结果，停机保持约 6.23 秒；每条均保留本地文件且无 ACK | 恢复启动后 **7.522 秒**，100/100 落库并生成本地 ACK；pending/rejected 均为 0 |
| PostgreSQL 停机 | 先将 100 条结果全部发布至 RabbitMQ，再停库约 5.44 秒；独立消费者实际记录数据库连接失败 | 恢复启动后 **3.715 秒**，100/100 落库并生成本地 ACK；pending/rejected 均为 0 |

恢复计时从调用 `docker start` **之前**开始，包含容器启动、消费者重连、补传/消费、数据库提交、回执轮询和本地 ACK 写入；轮询由脚本以约 0.2 秒间隔驱动，每轮最多 4 个并发 Outbox 请求，消费为一个独立进程。逐条 ACK 时间在每轮 flush 完成后观察，因此是带轮询粒度的上界观测，不是数据库提交精确时间。100 个独立 Outbox 是测试驱动，不代表启动了 100 个完整 Runner。

三组共 300 条独立结果均核对了完整规范化结果正文、回执唯一性、证据清单唯一性、任务完成与额度释放。两个恢复场景各只运行一轮；这不是恢复时间 P95、生产 SLA、吞吐容量或真实 Agent 全链路验收，也不覆盖断电、磁盘损坏或多节点故障。样本中未发现丢失/重复应用，不外推为任意故障下零丢失。

原始记录位于 Git 忽略目录 `data/result-benchmark-20261105/run1/`：`report.json`、300 行 `results.csv`、`benchmark.log`、API/消费者日志、Runner Outbox/ACK 文件以及 `source-manifest.json`（Git HEAD 与测试时源码摘要）。结束后已停止本轮 API、消费者及三个专用验证容器，保留数据供复核。

复现时启动上述专用容器并准备新的数据库、队列和输出目录，设置 `EYES_DATABASE_URL`、`EYES_REDIS_URL`、`EYES_RABBITMQ_URL`、`EYES_RESULT_QUEUE`，迁移后执行：

```sh
uv run python scripts/benchmark_result_delivery.py --output data/result-benchmark-20261105/new-run
```

脚本故障注入固定操作 `eyes-mq-validation-rabbit` 与 `eyes-mq-validation-pg`，仅可用于专用隔离环境。脚本环境摘要从实际连接配置读取数据库名称。脚本不会自动删除数据库或队列，基础容器由调用方在结束后停止。

## 2026-11-05 三轮故障注入复测

为避免简历只引用单次恢复时间，使用同一专项脚本完成三轮独立数据库、独立队列的验证。应用代码未变；脚本只补充实际数据库名称和测量工具元数据。

测试方法为 **Python 集成验证脚本 + HTTPX + Docker CLI 故障注入**：HTTPX/生产 ControlClient 驱动认证 API，调用生产 Runner Outbox、结果消费者和 PostgreSQL 落库逻辑；`docker stop/start` 停启专用 RabbitMQ/PostgreSQL 容器；`time.monotonic()` 从恢复启动命令前计到所有本地 ACK 文件出现。检查数据库中的结果正文、回执、证据清单、任务完成状态及额度释放。本次没有使用 Locust、pytest 或 Toxiproxy，不能将报告描述成这些工具的测试结果。

每轮每类故障 100 条结果，4 个并发 Outbox 请求、1 个独立消费者进程；保留约 0.2 秒轮询间隔。恢复时间包含容器启动、消费者重连、补传/消费、落库和本地确认，不是单条请求延迟，也不是数据库 ready 后单独排空队列的耗时。

| 场景 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 三轮中位数 | 逐条核对 |
| --- | ---: | ---: | ---: | ---: | --- |
| RabbitMQ 停机后恢复 | 7.853s | 7.540s | 7.710s | 7.710s | 每轮 100/100，共 300/300 完整入库并确认 |
| PostgreSQL 停机后恢复 | 3.677s | 3.962s | 4.262s | 3.962s | 每轮 100/100，共 300/300 完整入库并确认 |

每轮另用 100 条结果各提交 3 次，再关闭实际 AMQP 通道模拟数据库提交后 ACK 丢失：三轮共 900 次 HTTP 提交和 900 次实际重投，最终仅 300 个回执、300 份结果及 300 份证据清单，没有重复应用。三个场景合计 900 条独立结果逐条核对通过，额度均已释放。

原始记录：`data/result-reliability-20261105/summary.json`，以及 `trial1/`、`trial2/`、`trial3/` 中的 `report.json`、`results.csv`、API/消费者日志和本地 ACK 文件；根目录另有各轮控制日志、迁移日志和 `source-manifest.json`。各轮数据库为 `eyes_reliability_20261105_1` 至 `_3`，队列为 `eyes.results.reliability.20261105.trial1` 至 `trial3`，迁移均为 `0010_result_submissions`。

仍为 macOS ARM64、Python 3.14.7、HTTPX 0.28.1、PostgreSQL 18.6、RabbitMQ 4.4.0 的本地验证。实际结果来自 Python 计算子进程，调度上下文为合成记录，没有真实 Agent/模型执行。仅覆盖容器停启和明确的 ACK 丢失窗口，不覆盖断电、磁盘损坏、网络分区或长期大流量。三个恢复时间样本不足以宣称 P95 或生产 SLA；采用实际范围或三轮中位数描述。结束后已停止本轮进程及专用容器，保留数据。
