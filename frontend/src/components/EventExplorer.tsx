import { Activity, CircleAlert, GitBranch, List, Search } from "lucide-react";
import { useRef, useState } from "react";
import { formatDate, shortId } from "../format";
import type { Event } from "../types";
import { Empty, JsonBlock } from "./ui";
import { ToggleGroup, ToggleGroupItem } from "./ui/toggle-group";
import { executionTimeline } from "../lib/execution-timeline";
import { formatFlowDuration } from "../lib/observation-flow";

export function EventExplorer({
  events,
  selectedId,
  onSelect,
}: {
  events: Event[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [view, setView] = useState("timeline");
  const inspector = useRef<HTMLElement>(null);
  const timeline = executionTimeline(events);
  const timed = timeline.filter((item) => item.start !== null);
  const origin = Math.min(...timed.map((item) => item.start!));
  const finish = Math.max(...timed.map((item) => item.end!));
  const visible = timeline.filter(({ event, title }) =>
    `${title} ${event.content.type} ${event.content.source} ${event.producer_id} ${event.content.span_id || ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const selected = events.find((event) => event.id === selectedId);
  const current = selected || (!selectedId ? visible[0]?.event : undefined);
  const currentItem = timeline.find((item) => item.event.id === current?.id);
  const related = current
    ? timeline.filter(
        ({ event, parentSpanId }) =>
          event.id !== current.id &&
          event.content.trace_id === current.content.trace_id &&
          (event.content.span_id === current.content.span_id ||
            event.content.span_id === currentItem?.parentSpanId ||
            parentSpanId === current.content.span_id),
      )
    : [];
  return (
    <section aria-label="执行过程与证据">
      <div className="execution-view-toolbar">
        <ToggleGroup
          value={[view]}
          onValueChange={(values) => {
            if (values.length) setView(values[0]);
          }}
          aria-label="执行事件视图"
          size="sm"
        >
          <ToggleGroupItem value="timeline">
            <Activity size={15} />
            时间线
          </ToggleGroupItem>
          <ToggleGroupItem value="list">
            <List size={15} />
            事件列表
          </ToggleGroupItem>
        </ToggleGroup>
        <span className="muted small-text">
          本页 {events.length} 条事件 · {timed.length} 条含耗时
        </span>
      </div>
      {view === "timeline" && (
        <p className="muted small-text">
          节点按接收顺序排列，耗时条按本页 Span
          起止时间绘制；连线仅用于阅读，不代表调用依赖。未上报的内部步骤不会推断补齐。
        </p>
      )}
      <div
        className={`event-explorer ${view === "timeline" ? "execution-timeline" : ""}`}
      >
        <div className="event-index">
          <div className="event-index-heading">
            <span className="section-label">已采集事件</span>
            <span className="count">{events.length} / 本页</span>
          </div>
          <div className="search">
            <Search size={15} />
            <input
              type="search"
              aria-label="搜索当前页事件"
              placeholder="类型、来源或 Span…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="event-list" aria-label="选择执行事件">
            {visible.length ? (
              visible.map(
                ({ event, title, start, end, duration, failed, truncated }) => (
                  <button
                    key={event.id}
                    className={`event-choice ${current?.id === event.id ? "selected" : ""}`}
                    data-failed={failed}
                    aria-pressed={current?.id === event.id}
                    onClick={() => {
                      onSelect(event.id);
                      if (window.matchMedia("(max-width: 700px)").matches)
                        inspector.current?.focus();
                    }}
                  >
                    <span className="event-order">
                      {view === "timeline" ? (
                        failed ? (
                          <CircleAlert size={16} aria-label="Span 错误" />
                        ) : (
                          <Activity size={16} />
                        )
                      ) : (
                        String(events.indexOf(event) + 1).padStart(2, "0")
                      )}
                    </span>
                    <span className="event-choice-copy">
                      <strong>{title}</strong>
                      {truncated && <small>正文已截断</small>}
                      {view === "timeline" && (
                        <>
                          <small>
                            {event.expired_at
                              ? "正文已过期"
                              : failed
                                ? "Span 错误"
                                : event.content.type}{" "}
                            ·{" "}
                            {duration === null
                              ? "耗时未提供"
                              : formatFlowDuration(duration)}
                          </small>
                          {start !== null && end !== null && (
                            <span
                              className="execution-duration-track"
                              aria-hidden="true"
                            >
                              <span
                                style={{
                                  left: `${finish > origin ? ((start - origin) / (finish - origin)) * 100 : 0}%`,
                                  width: `${finish > origin ? ((end - start) / (finish - origin)) * 100 : 100}%`,
                                }}
                              />
                            </span>
                          )}
                        </>
                      )}
                      <small>
                        {event.content.source} · #{event.content.sequence}
                      </small>
                      <small>{formatDate(event.content.occurred_at)}</small>
                    </span>
                  </button>
                ),
              )
            ) : (
              <Empty
                compact
                title="没有匹配的事件"
                description="搜索范围为当前页，调整关键词或翻页。"
              />
            )}
          </div>
        </div>
        <section
          className="event-inspector"
          aria-label="选中事件的证据详情"
          ref={inspector}
          tabIndex={-1}
        >
          {current ? (
            <>
              <div className="section-label">EVENT / {shortId(current.id)}</div>
              <h3>{currentItem?.title}</h3>
              <p className="muted small-text">
                {current.content.type} ·{" "}
                {currentItem?.duration == null
                  ? "耗时未提供"
                  : formatFlowDuration(currentItem.duration)}
                {currentItem?.failed && " · Span 错误"}
              </p>
              {currentItem?.truncated && (
                <p className="notice" role="status">
                  正文已截断 ·{" "}
                  {currentItem.truncationReason || "未提供截断原因"}
                  。原始内容不完整，耗时与执行结果保持未知。
                </p>
              )}
              <div className="key-values">
                <div>
                  <span>采集来源</span>
                  <code>{current.content.source}</code>
                </div>
                <div>
                  <span>生产者 / 序号</span>
                  <code>
                    {current.producer_id} / {current.content.sequence}
                  </code>
                </div>
                <div>
                  <span>发生时间</span>
                  <span>{formatDate(current.content.occurred_at)}</span>
                </div>
                <div>
                  <span>接收时间</span>
                  <span>{formatDate(current.created_at)}</span>
                </div>
                <div>
                  <span>Span</span>
                  <code>{current.content.span_id || "未提供"}</code>
                </div>
                <div>
                  <span>父 Span</span>
                  <code>{currentItem?.parentSpanId || "未提供"}</code>
                </div>
              </div>
              {!current.content.parent_span_id && currentItem?.parentSpanId && (
                <p className="muted small-text">
                  父 Span 来自本页同一 Trace、同一 Span 的已采集 Span 记录。
                </p>
              )}
              <section
                className="execution-relations"
                aria-label="调用与事件关联"
              >
                <h4>
                  <GitBranch size={15} />
                  关联事件 · 本页
                </h4>
                {related.length ? (
                  related.map(({ event }) => (
                    <button
                      key={event.id}
                      className="text-button"
                      onClick={() => onSelect(event.id)}
                    >
                      {event.content.span_id === current.content.span_id
                        ? "同 Span"
                        : event.content.span_id === currentItem?.parentSpanId
                          ? "父 Span"
                          : "子 Span"}{" "}
                      · {event.content.type} · #{event.content.sequence}
                    </button>
                  ))
                ) : (
                  <p className="muted small-text">
                    本页没有关联事件。其他页或未采集的节点不代表不存在。
                  </p>
                )}
              </section>
              {current.expired_at ? (
                <p className="muted" role="status">
                  事件正文已于 {formatDate(current.expired_at)}{" "}
                  按保留策略清理。事件身份和引用仍保留。
                </p>
              ) : (
                <JsonBlock title="事件内容" value={current.content.data} />
              )}
              <JsonBlock title="证据引用" value={`event:${current.id}`} />
            </>
          ) : (
            <Empty
              compact
              title={selectedId ? "该事件不在当前页" : "尚无已采集事件"}
              description={
                selectedId
                  ? "切换事件页，或选择本页的事件继续审阅。"
                  : "无事件不表示没有内部执行，采集范围取决于目标接入。"
              }
            />
          )}
        </section>
      </div>
    </section>
  );
}
