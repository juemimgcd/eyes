# 统一接入、资源管理与实时观测：实现对照

范围：本轮按“先把功能写好，暂不接入真实 Agent”实现。下面区分代码实现、已有本地运行证据和
暂缓的真实运行验收。工作区尚未提交，记录日期为 2026-11-05，数据库 head 为
`0008_report_retention`。本文件不宣称生产容量或真实 Agent 完整闭环已验收。

| 要求 | 当前实现位置 | 当前证据 |
| --- | --- | --- |
| 版本化统一 HTTP 协议、输入 Schema、冻结能力 | `agent_protocol/client.py`、`schema.py`、`target.py`；catalog/experiments 服务 | 客户端与发布/实验入口已核对；实际包装服务能力发现、目标发布成功 |
| 指定 Runner 网络内检查接入，保留 HTTP/Python 兼容 | discovery API/服务、Runner discovery、`AgentDiscoveryDialog.tsx` | 实际 API → Runner → 包装服务 GET capabilities → completed → 发布 201；未执行业务任务 |
| 官方持久化包装服务 | `agent_service/`、`eyes-agent`、示例 TOML | 初始化、就绪、认证发现、重复 supervisor 拒绝已运行；任务与恢复联调暂缓 |
| 有界 PostgreSQL/Redis/HTTP 连接，独立控制/证据上传池 | server/runner 配置、storage/database、runner/client、AgentClients、Compose | 显式预算与错误路径已核对；本地数据库/API/包装服务启动与请求成功 |
| 全局/项目/目标/实验/Runner 额度与入队背压 | experiments/scheduling 服务、Runner run loop | 事务、锁顺序、持久预留和槽位核对完成；新协议多 Runner 压力验收暂缓 |
| 幂等提交、容量拒绝、取消、重启核对 | protocol client、Runner protocol、scheduling reconciliation | 代码核对：原键查询、429 等待、原始身份校验、停止确认后释放；真实执行故障验收暂缓 |
| 事务通知 Outbox 与独立发布器 | realtime/transactions、publisher、0005 迁移 | 实际目标发布产生 Outbox，Redis 订阅收到通知，发布后清空；事实与通知不同事务边界已核对 |
| SSE、慢客户端界限、再鉴权与周期补查 | realtime/hub、routes、`use-realtime.ts`、nginx | 实际认证 SSE；Redis 停止/恢复切换 polling/live；修复重试导致两秒刷新问题；等待期间无 idle transaction |
| 乱序安全接收游标与前端补查 | event_receipts、realtime routes、Observation/CaseWorkspace | 按接收版本读取，epoch 变化重置；前端类型检查与构建通过；浏览器事件展示未验收 |
| 可重建版本缓存与有界数据库降级 | realtime/cache、ShortReader、CacheEpoch、restore | 已验证真实排队实验的 miss/hit、取消后的版本失效、Redis 停止降级和恢复命中 |
| 诊断与测量边界 | operations API、diagnostics、SummaryCache | 实际返回命中/错误/降级计数、连接获取与响应头样本；窗口 P95 不作容量声明 |
| 独立事件、产物、通知、报告保留 | operations/evidence/evaluation/comparison 服务、配置、0008 | 预览与空库审计通过；部分清理保留身份、新建 partial 清单，禁止将已清理证据重新封存；有数据的清理运行验收尚未完成 |
| 部署、升级、回滚、备份恢复边界 | Compose、迁移、operations/backup、platform 文档 | 开发库迁移至 0008、Compose config、源码包与 wheel 构建通过；restore 轮换 epoch 已核对，恢复演练不冒充已完成 |

## 本地汇总缓存运行记录

复用仓库既有 `integrations/deta/coding-smoke.jsonl` 与既有评分器发布信息，在独立开发库建立
实验 `6b546eeb-f930-44f4-901d-c93bbe5fe347`。Runner 未启动，执行尝试数始终为 0。

- 3 个计划用例处于 queued；连续读取两次，统计时间一致，计数为 miss=1、hit=1、database_reads=1。
- 取消排队实验后，汇总变为 completed、cancelled_runs=3，统计时间更新，miss=2、database_reads=2。
  completed 是现有实验聚合状态，不表示执行成功；execution_successes 仍为 0。
- 停止独立 Redis 后读取两次均返回 200 和相同事实；cache_errors=1、bypasses=1、database_reads=4。
- 恢复 Redis 后再次读取，命中当前资源版本的缓存，hits 增为 3；未把旧 queued 汇总用于取消后的版本。

这些是顺序功能请求，不能证明四个并发槽位耗尽行为、性能提升或稳定吞吐。

## 检查与明确限制

已执行相关 Ruff、Python 编译、前端 TypeScript、ESLint、Vite 构建、差异检查及 `uv build`。
没有新增或修改测试文件、测试用例、fixtures、mocks 或 snapshots；未提交、推送或部署。

桌面 Chrome 访问宿主不可用，尝试未获得页面；静态设计检测只覆盖源码模式，不替代视觉与交互验证。
真实 Agent 执行、评分证据、取消竞争、Runner/包装服务重启、同条件性能对比、带数据的保留/恢复
演练仍属于后续运行验收。当前实现的功能不得被描述为已通过这些验收。


## 2026-11-05 后续：RabbitMQ 与缓存对照

用户进一步授权接入 RabbitMQ 并测试存储/缓存。当前迁移已更新为 `0009_event_submissions`，新增异步事件发布、消费回执及 Runner 持久化确认门槛。真实本地 PostgreSQL/Redis/RabbitMQ 功能检查与合成结果集 HTTP 对照见 [验证报告](event-queue.md)。这次没有运行真实 Agent 或模型，不改变上文其他功能的验收边界。


## 最终结果链路修正

用户进一步澄清要承接的是 Python 执行完的最终结果。新增 `0010_result_submissions` 与 `eyes-result-consumer`，默认示例改为结果 queued、事件 direct。消费者提交 PostgreSQL 后 ACK，业务拒绝结果保留死信消息；见 [结果队列及验证](result-queue.md)。缓存代码未修改，原 ABBA 数据仍只代表当时的本地实验汇总查询。
