import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  ChevronRight,
  Copy,
  Cpu,
  Plug,
  RefreshCw,
  Radio,
  Info,
  Wrench,
} from "lucide-react";
import { useConnection } from "../connection";
import { formatDate, shortId } from "../format";
import {
  Dialog,
  Disconnected,
  Empty,
  ErrorNotice,
  JsonBlock,
  Loading,
  Metrics,
  PageHeading,
  Pagination,
} from "../components/ui";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { AgentConnectDialog } from "@/components/AgentConnectDialog";
import { ObservationFlow } from "@/components/ObservationFlow";
import {
  eventLabels as labels,
  type ObservationEvent,
} from "@/lib/observation-flow";
import { Input } from "@/components/ui/input";
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldDescription,
} from "@/components/ui/field";

const recordDate = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const recordTime = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

type Source = {
  id: string;
  name: string;
  revoked: boolean;
  last_seen_at: string | null;
};
type MetricsData = {
  events?: number;
  turns?: number;
  model_calls?: number;
  tool_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  usage_responses?: number;
  unknown_usage?: number;
  started_at?: string;
  ended_at?: string;
  dropped?: number;
  truncated?: number;
};
type ObservedRun = {
  id: string;
  source_id: string;
  source_name: string;
  external_id: string;
  session_id: string;
  agent: string;
  model: string;
  capture_body: boolean;
  status: string;
  prompt: string | null;
  created_at: string;
  last_seen_at: string;
  evidence_status: string;
  sequence_gaps: number;
  metrics: MetricsData;
};
type EventContent = ObservationEvent["content"];
type EventRecord = ObservationEvent;
type RunDetail = ObservedRun & {
  events: EventRecord[];
  has_more: boolean;
  next_cursor: number;
  epoch: string;
};
type ReceivedEvents = {
  items: EventRecord[];
  has_more: boolean;
  next_cursor: number;
  epoch: string;
  reset: boolean;
};

function Status({
  value,
  evidence = false,
}: {
  value: string;
  evidence?: boolean;
}) {
  const names: Record<string, string> = {
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    limited: "达到限制",
    cancelled: "已取消",
    disconnected: "观测中断",
    complete: "链路已收齐",
    partial: "链路不完整",
    collecting: "采集中",
  };
  const variant =
    value === "running" || value === "collecting"
      ? "active"
      : value === "completed" || value === "complete"
        ? "success"
        : value === "failed"
          ? "destructive"
          : "warning";
  return (
    <Badge
      variant={variant}
      className={evidence ? "evidence-state" : "run-state"}
    >
      {evidence && (
        <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      )}
      {names[value] || value}
    </Badge>
  );
}

export function ObservationPage({
  scrollPositions,
}: {
  scrollPositions: Map<string, number>;
}) {
  const { api } = useConnection();
  const { key: entryKey } = useLocation();
  const restoreTableScroll = useCallback(
    (element: HTMLDivElement | null) => {
      if (!element) return;
      element.scrollTop = scrollPositions.get(entryKey) ?? 0;
    },
    [entryKey, scrollPositions],
  );
  const [setup, setSetup] = useState(false);
  const [legacySetup, setLegacySetup] = useState(false);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const pageValue = Number(params.get("page"));
  const page =
    Number.isSafeInteger(pageValue) && pageValue >= 0 ? pageValue : 0;
  const source = params.get("source") || "";
  const session = params.get("session") || "";
  const query = useQuery({
    queryKey: ["observed-runs", source, session, page],
    queryFn: ({ signal }) =>
      api!.request<{ items: ObservedRun[] }>(
        `/v1/observation/runs?limit=51&offset=${page * 50}${source ? `&source_id=${encodeURIComponent(source)}` : ""}${session ? `&session_id=${encodeURIComponent(session)}` : ""}`,
        { signal },
      ),
    enabled: !!api,
  });
  const sources = useQuery({
    queryKey: ["observation-sources"],
    queryFn: ({ signal }) =>
      api!.request<{ items: Source[] }>("/v1/observation/sources?limit=200", {
        signal,
      }),
    enabled: !!api,
  });
  const rows = query.data?.items.slice(0, 50) || [];
  const filtered = Boolean(session || source);
  const firstEmpty =
    query.isSuccess && rows.length === 0 && page === 0 && !filtered;
  function changePage(value: number) {
    const next = new URLSearchParams(params);
    next.set("page", String(value));
    setParams(next);
  }
  return (
    <>
      <PageHeading
        title="Agent 观测"
        action={
          !firstEmpty && (
            <Button disabled={!api} onClick={() => setSetup(true)}>
              <Plug data-icon="inline-start" />
              接入 Agent
            </Button>
          )
        }
      />
      {!api ? (
        <section className="panel">
          <Disconnected />
        </section>
      ) : (
        <>
          <section className="panel" aria-labelledby="recent-runs-title">
            <div className="observation-toolbar">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id="recent-runs-title">
                  {session
                    ? `会话 ${shortId(session)}`
                    : source
                      ? "来源任务"
                      : "最近任务"}
                </h2>
                {query.data && (
                  <span className="record-count">本页 {rows.length} 条</span>
                )}
                {filtered && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setParams({})}
                  >
                    清除筛选
                  </Button>
                )}
              </div>
              <div className="observation-refresh">
                {query.dataUpdatedAt > 0 && (
                  <span>
                    最近同步{" "}
                    {new Date(query.dataUpdatedAt).toLocaleTimeString("zh-CN", {
                      hour12: false,
                    })}
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="刷新观测记录"
                  disabled={query.isFetching}
                  onClick={() => void query.refetch()}
                >
                  <RefreshCw
                    className={query.isFetching ? "spin" : undefined}
                  />
                </Button>
              </div>
            </div>
            {query.error && (
              <ErrorNotice
                error={query.error}
                retry={() => void query.refetch()}
              />
            )}
            {query.isPending ? (
              <Loading label="正在读取 Agent 运行记录" />
            ) : firstEmpty ? (
              <Empty
                className="observation-start"
                icon={Radio}
                title="暂无任务记录"
                description="接入 Agent 后，运行一次任务即可看到记录。"
                action={
                  <Button onClick={() => setSetup(true)}>
                    <Plug data-icon="inline-start" />
                    接入 Agent
                  </Button>
                }
              />
            ) : !rows.length && !query.error ? (
              <Empty
                title={page > 0 ? "本页没有任务记录" : "当前筛选下没有任务"}
                description={
                  page > 0
                    ? "返回上一页查看其他记录。"
                    : "清除筛选可查看所有来源和会话。"
                }
                action={
                  <Button
                    variant="outline"
                    onClick={() =>
                      page > 0 ? changePage(page - 1) : setParams({})
                    }
                  >
                    {page > 0 ? "上一页" : "查看全部任务"}
                  </Button>
                }
              />
            ) : (
              rows.length > 0 && (
                <>
                  <Table
                    className="run-index"
                    containerProps={{
                      role: "region",
                      "aria-labelledby": "recent-runs-title",
                      tabIndex: 0,
                      className: "run-index-scroll",
                      ref: restoreTableScroll,
                      onScroll: (event) =>
                        scrollPositions.set(
                          entryKey,
                          event.currentTarget.scrollTop,
                        ),
                    }}
                  >
                    <caption className="sr-only">
                      Agent 任务记录。按任务开始记录的时间从新到旧排列。
                    </caption>
                    <colgroup>
                      <col className="run-subject-col" />
                      <col className="run-time-col" />
                      <col className="run-calls-col" />
                      <col className="run-state-col" />
                      <col className="run-evidence-col" />
                    </colgroup>
                    <TableHeader>
                      <TableRow>
                        <TableHead scope="col">任务 / 会话</TableHead>
                        <TableHead scope="col">记录时间</TableHead>
                        <TableHead scope="col">轮次 / 工具</TableHead>
                        <TableHead scope="col">执行状态</TableHead>
                        <TableHead scope="col">证据状态</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rows.map((run) => {
                        const recordedAt = new Date(run.created_at);
                        return (
                          <TableRow key={run.id}>
                            <th scope="row" className="run-subject">
                              <Link
                                className="observation-run-title"
                                to={`/observe/${run.id}`}
                                state={{ fromObservationList: true }}
                              >
                                {run.prompt ||
                                  (run.capture_body
                                    ? "继续会话"
                                    : "任务正文未采集")}
                              </Link>
                              <div className="observation-run-meta">
                                <span>{run.source_name}</span>
                                <span>{run.model}</span>
                                <button
                                  className="text-button"
                                  onClick={() =>
                                    setParams({
                                      source: run.source_id,
                                      session: run.session_id,
                                    })
                                  }
                                >
                                  会话 {shortId(run.session_id)}
                                </button>
                              </div>
                            </th>
                            <TableCell>
                              <time
                                className="record-time"
                                dateTime={run.created_at}
                              >
                                {recordTime.format(recordedAt)}
                                <small>{recordDate.format(recordedAt)}</small>
                              </time>
                            </TableCell>
                            <TableCell className="record-numbers">
                              {run.metrics.turns ?? 0}
                              <span aria-hidden="true"> / </span>
                              <span className="sr-only"> 轮，工具调用 </span>
                              {run.metrics.tool_calls ?? 0}
                            </TableCell>
                            <TableCell>
                              <Status value={run.status} />
                            </TableCell>
                            <TableCell>
                              <Status evidence value={run.evidence_status} />
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                  <Pagination
                    page={page}
                    next={(query.data?.items.length || 0) > 50}
                    onPage={changePage}
                  />
                </>
              )
            )}
          </section>
          <p className="observation-footnote observation-limit">
            <Info size={15} aria-hidden="true" />
            仅显示已上报的事件。观测中断时，任务可能仍在运行。
          </p>
          <details className="observation-legacy">
            <summary>已有埋点或旧版上报接入</summary>
            <p className="muted">
              统一连接程序会自动创建来源。已有埋点可在这里管理上报令牌。
            </p>
            <Button variant="ghost" onClick={() => setLegacySetup(true)}>
              管理事件上报来源
            </Button>
          </details>
        </>
      )}
      {setup && (
        <AgentConnectDialog
          defaultPurpose="observe"
          onClose={() => setSetup(false)}
          onManual={() => {
            setSetup(false);
            navigate("/targets");
          }}
        />
      )}
      {legacySetup && (
        <SourceSetup
          onClose={() => setLegacySetup(false)}
          sources={sources.data?.items || []}
          error={sources.error}
          loading={sources.isPending}
        />
      )}
    </>
  );
}

function SourceSetup({
  onClose,
  sources,
  error,
  loading,
}: {
  onClose: () => void;
  sources: Source[];
  error: Error | null;
  loading: boolean;
}) {
  const { api } = useConnection();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<(Source & { token: string }) | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);
  const config = created
    ? `EYES_OBSERVATION_URL=${new URL(`${import.meta.env.BASE_URL}api`, window.location.origin).href}\nEYES_OBSERVATION_TOKEN=${created.token}\nEYES_CAPTURE_BODY=true`
    : "";
  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFailure(null);
    try {
      setCreated(
        await api!.post<Source & { token: string }>("/v1/observation/sources", {
          schema_version: "1.0",
          name: name.trim(),
        }),
      );
      await client.invalidateQueries({ queryKey: ["observation-sources"] });
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: string) {
    setBusy(true);
    setFailure(null);
    try {
      await api!.post(`/v1/observation/sources/${id}/revoke`);
      await client.invalidateQueries({ queryKey: ["observation-sources"] });
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title="观测接入"
      subtitle="Agent 自己执行，Eyes 接收运行链路。"
      onClose={onClose}
      wide
    >
      <div className="observation-setup">
        {failure != null && <ErrorNotice error={failure} />}
        {created ? (
          <>
            <h3>为 {created.name} 配置观测上报</h3>
            <p className="muted">
              令牌只显示一次，仅允许上报这个接入的事件。正文采集包含任务、模型输入输出和工具参数；改为
              false 可只采集运行信息。
            </p>
            <pre
              className="observation-config"
              tabIndex={0}
              aria-label={`${created.name} 的观测配置`}
            >
              {config}
            </pre>
            <Button
              variant="outline"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(config);
                  setCopied(true);
                } catch (e) {
                  setFailure(e);
                }
              }}
            >
              <Copy data-icon="inline-start" />
              {copied ? "已复制" : "复制配置"}
            </Button>
            <p>
              将配置加入 {created.name}{" "}
              的运行环境，再用它原有的方式启动并执行任务。 若启动程序读取
              .env，也可保存到该文件。Agent 需要已实现 Eyes
              事件上报，仅设置环境变量不会自动采集内部调用。
            </p>
            <Button onClick={onClose}>完成</Button>
          </>
        ) : (
          <>
            <h3>配置 Agent 观测上报</h3>
            <p>
              为接入命名并生成专用配置。Agent 需要实现 Eyes 事件上报；
              仅提供进程地址或设置环境变量，无法自动观测内部调用。
            </p>
            <form onSubmit={(event) => void create(event)}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="source-name">Agent 名称</FieldLabel>
                  <Input
                    id="source-name"
                    value={name}
                    placeholder="例如：我的编程助手"
                    maxLength={200}
                    required
                    data-autofocus
                    onChange={(event) => setName(event.target.value)}
                    disabled={busy}
                  />
                  <FieldDescription>
                    此名称会显示在接入配置、任务列表和运行详情中。
                  </FieldDescription>
                </Field>
              </FieldGroup>
              <Button type="submit" disabled={busy || !name.trim()}>
                <Plug data-icon="inline-start" />
                {busy ? "处理中…" : "生成接入配置"}
              </Button>
            </form>
            <details>
              <summary>Deta 接入示例</summary>
              <p>
                Deta 已支持 Eyes 事件上报。将生成的配置保存到 Deta 项目的
                .env，再运行：
              </p>
              <pre className="observation-config">
                uv run --env-file .env deta -i
              </pre>
              <p className="muted">
                连接本机 Eyes 时，Deta
                也支持本机自动登记；此方式不使用上方创建的接入名称。
                要使用自定义名称，请使用生成的地址和令牌。
              </p>
            </details>
            {error && <ErrorNotice error={error} />}
            {loading ? (
              <Loading label="读取现有接入" />
            ) : (
              sources.length > 0 && (
                <div>
                  <h3>已有接入</h3>
                  {sources.map((source) => (
                    <div className="observation-source" key={source.id}>
                      <div>
                        <strong>{source.name}</strong>
                        <small className="muted">
                          {source.revoked
                            ? "已停用"
                            : source.last_seen_at
                              ? `最近上报 ${formatDate(source.last_seen_at)}`
                              : "等待首次上报"}
                        </small>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={source.revoked || busy}
                        onClick={() => void revoke(source.id)}
                      >
                        停用上报
                      </Button>
                    </div>
                  ))}
                </div>
              )
            )}
          </>
        )}
      </div>
    </Dialog>
  );
}

export function ObservationRunPage() {
  const { id } = useParams();
  const { state } = useLocation();
  const navigate = useNavigate();
  const { api } = useConnection();
  const client = useQueryClient();
  const query = useQuery<RunDetail>({
    queryKey: ["observed-run", id],
    queryFn: async ({ signal }) => {
      const previous = client.getQueryData<RunDetail>(["observed-run", id]);
      const [run, received] = await Promise.all([
        api!.request<ObservedRun>(`/v1/observation/runs/${id}?limit=1`, {
          signal,
        }),
        api!.request<ReceivedEvents>(
          `/v1/realtime/observed_run/${id}/events?after=${previous?.next_cursor ?? 0}&limit=200${previous ? `&epoch=${previous.epoch}` : ""}`,
          { signal },
        ),
      ]);
      // Receipt order drives retrieval; producer order only controls display.
      // Late events with a smaller sequence still arrive after this cursor.
      const events = [
        ...(received.reset ? [] : (previous?.events ?? [])),
        ...received.items,
      ].sort((a, b) => a.content.sequence - b.content.sequence);
      return {
        ...run,
        events,
        has_more: received.has_more,
        next_cursor: received.next_cursor,
        epoch: received.epoch,
      };
    },
    enabled: !!api && !!id,
  });
  if (!api) return <Disconnected />;
  if (query.isPending) return <Loading label="正在读取执行链路" />;
  if (!query.data)
    return (
      <ErrorNotice error={query.error} retry={() => void query.refetch()} />
    );
  const run = query.data;
  const groups = new Map<number, EventRecord[]>();
  for (const event of run.events) {
    const number = event.content.turn || 0;
    groups.set(number, [...(groups.get(number) || []), event]);
  }
  const elapsed =
    run.metrics.started_at && run.metrics.ended_at
      ? (
          (new Date(run.metrics.ended_at).getTime() -
            new Date(run.metrics.started_at).getTime()) /
          1000
        ).toFixed(1) + " s"
      : "—";
  return (
    <>
      {state?.fromObservationList ? (
        <button
          type="button"
          className="back-link"
          onClick={() => navigate(-1)}
        >
          <ArrowLeft size={15} />
          返回任务列表
        </button>
      ) : (
        <Link className="back-link" to="/observe">
          <ArrowLeft size={15} />
          全部观测任务
        </Link>
      )}
      <PageHeading
        title={run.source_name}
        description={`${run.model} · 会话 ${shortId(run.session_id)}`}
        action={
          <Button
            variant="outline"
            nativeButton={false}
            render={
              <Link
                to={`/observe?source=${run.source_id}&session=${encodeURIComponent(run.session_id)}`}
              />
            }
          >
            查看同一会话
          </Button>
        }
      />
      {query.error && (
        <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      )}
      <div className="run-summary">
        <section className="observation-task" aria-label="本次任务">
          <span className="section-label">用户任务</span>
          <p>
            {run.prompt ||
              (run.capture_body
                ? "继续既有会话，查看下方输入事件。"
                : "未启用正文采集。可在 Agent 的上报配置中设置 EYES_CAPTURE_BODY=true 后开始新任务。")}
          </p>
          <div className="observation-run-meta">
            <span>
              {run.metrics.usage_responses
                ? `${run.metrics.total_tokens?.toLocaleString()} tokens（已报告部分）`
                : "Token 用量未知"}
            </span>
            <span>
              {run.metrics.usage_responses ?? 0} 个响应提供用量 ·{" "}
              {run.metrics.unknown_usage ?? 0} 个请求用量未知
            </span>
          </div>
        </section>
        <div className="observation-status">
          <Status value={run.status} />
          <Status evidence value={run.evidence_status} />
          <span className="muted small-text">
            最近上报 {formatDate(run.last_seen_at)}
          </span>
        </div>
        <Metrics
          items={[
            {
              label: "循环轮次",
              value: run.metrics.turns ?? 0,
              note: "已观察到的轮次开始",
            },
            {
              label: "模型调用",
              value: run.metrics.model_calls ?? 0,
              note: "实际请求尝试，包含重试与摘要",
            },
            {
              label: "工具调用",
              value: run.metrics.tool_calls ?? 0,
              note: "实际进入工具执行的次数",
            },
            {
              label: "任务耗时",
              value: elapsed,
              note: "Agent 开始到结束，不含上传",
            },
          ]}
        />
      </div>
      {run.evidence_status === "partial" && (
        <div className="notice" role="status">
          <p>
            链路不完整：序号缺口 {run.sequence_gaps}，报告丢失{" "}
            {run.metrics.dropped ?? 0}，截断 {run.metrics.truncated ?? 0}。
            {run.status === "disconnected" &&
              "超过 30 秒未收到活动心跳，无法据此判断 Agent 是否仍在运行。"}
          </p>
        </div>
      )}
      <div className="observation-chain-heading">
        <h2>执行链路</h2>
        <span className="muted small-text">
          按 Agent 事件序号排列 · {run.metrics.events ?? 0} 条事件
        </span>
      </div>
      {!run.events.length ? (
        <Empty
          title="等待执行事件"
          description="Agent 上报后会自动显示在这里。"
        />
      ) : (
        <ObservationFlow
          key={run.id}
          events={run.events}
          status={run.status}
          evidenceStatus={run.evidence_status}
          hasMore={run.has_more}
          captureBody={run.capture_body}
        >
          <div className="observation-chain">
            {[...groups.entries()].map(([turn, events]) => (
              <section key={turn} className="observation-turn">
                <h3>{turn ? `第 ${turn} 轮` : "任务开始"}</h3>
                <ol>
                  {events.map(({ id: eventId, content: event }) => (
                    <TraceEvent key={eventId} event={event} />
                  ))}
                </ol>
              </section>
            ))}
          </div>
        </ObservationFlow>
      )}
      {run.has_more && (
        <Button
          variant="outline"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {query.isFetching ? "正在读取事件…" : "加载后续 200 条事件"}
        </Button>
      )}
      <p className="observation-footnote">
        完整性仅针对本接入声明并实际上传的事件；不代表未接入观测的内部过程或外部服务。
      </p>
    </>
  );
}

function TraceEvent({ event }: { event: EventContent }) {
  const data = event.data;
  const Icon = event.type.startsWith("tool")
    ? Wrench
    : event.type.startsWith("model") || event.type === "message_end"
      ? Cpu
      : Activity;
  const title = labels[event.type] || event.type;
  return (
    <li className="observation-event">
      <Icon size={16} className="observation-event-icon" />
      <details>
        <summary>
          <span className="observation-event-name">
            {title}
            {typeof data.name === "string" && event.type.startsWith("tool")
              ? ` · ${data.name}`
              : ""}
          </span>
          <span className="observation-event-meta">
            {typeof data.duration_ms === "number" && (
              <span>{data.duration_ms.toFixed(0)} ms</span>
            )}
            {typeof data.status === "string" && (
              <span>
                {data.status === "success" || data.status === "UNSET"
                  ? "完成"
                  : data.status}
              </span>
            )}
            {event.truncated && <Badge variant="warning">已截断</Badge>}
            <span>#{event.sequence}</span>
            <ChevronRight size={14} />
          </span>
        </summary>
        <div className="observation-event-body">
          {typeof data.text === "string" && <pre>{data.text}</pre>}
          {typeof data.answer === "string" && data.answer && (
            <pre>{data.answer}</pre>
          )}
          <JsonBlock title="事件数据" value={data} />
          <details className="observation-identifiers">
            <summary>关联标识与发生时间</summary>
            <JsonBlock
              value={{
                occurred_at: event.occurred_at,
                tool_call_id: event.tool_call_id,
                trace_id: event.trace_id,
                span_id: event.span_id,
                parent_span_id: event.parent_span_id,
              }}
            />
          </details>
        </div>
      </details>
    </li>
  );
}
