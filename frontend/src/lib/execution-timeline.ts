import type { Event } from "../types";

// Event payloads are open-ended JSON. Only SDK span records with actual numeric
// bounds can supply a duration; expired, custom or incomplete records cannot.
export function executionTimeline(events: Event[]) {
  const spanParents = new Map(
    events
      .filter((event) => event.content.type === "span")
      .map(({ content }) => [
        `${content.trace_id}:${content.span_id}`,
        content.parent_span_id,
      ]),
  );
  return events.map((event) => {
    const { data, type } = event.content;
    const truncated = !event.expired_at && data.truncated === true;
    const start = data.start_ns;
    const end = data.end_ns;
    const timed =
      !event.expired_at &&
      !truncated &&
      type === "span" &&
      typeof start === "number" &&
      typeof end === "number" &&
      Number.isFinite(start) &&
      Number.isFinite(end) &&
      end >= start;
    return {
      event,
      parentSpanId:
        event.content.parent_span_id ??
        spanParents.get(`${event.content.trace_id}:${event.content.span_id}`) ??
        null,
      truncated,
      truncationReason:
        truncated && typeof data.reason === "string" ? data.reason : null,
      title:
        !event.expired_at && typeof data.name === "string" ? data.name : type,
      start: timed ? start / 1e6 : null,
      end: timed ? end / 1e6 : null,
      duration: timed ? (end - start) / 1e6 : null,
      failed:
        !event.expired_at &&
        !truncated &&
        type === "span" &&
        data.status === "ERROR",
    };
  });
}
