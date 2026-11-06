import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Api } from "../api";

type Mode = "connecting" | "live" | "polling";
type Notice = {
  event: "update" | "resync" | "auth_error" | "unavailable";
  mode?: "live" | "polling";
  kind?: string;
  resource_id?: string;
  version?: number;
};

export function useRealtime(api: Api | null, onExpired: () => void) {
  const client = useQueryClient();
  const [state, setState] = useState<{ api: Api | null; mode: Mode }>({
    api: null,
    mode: "connecting",
  });
  useEffect(() => {
    if (!api) return;
    const controller = new AbortController();
    const signal = controller.signal;
    const versions = new Map<string, number>();
    const pending = new Map<string, readonly unknown[]>();
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    let lastSync = Date.now();
    const queue = (key: readonly unknown[]) => {
      if (signal.aborted || pending.has("[]")) return;
      if (key.length === 0 || pending.size >= 2048) {
        pending.clear();
        pending.set("[]", []);
      } else {
        pending.set(JSON.stringify(key), key);
      }
      if (flushTimer !== undefined) return;
      flushTimer = setTimeout(() => {
        flushTimer = undefined;
        if (signal.aborted) return;
        for (const queryKey of pending.values())
          void client.invalidateQueries({ queryKey, refetchType: "active" });
        pending.clear();
      }, 500);
    };
    const resync = () => {
      lastSync = Date.now();
      versions.clear();
      queue([]);
    };
    const handle = (notice: Notice) => {
      if (signal.aborted) return;
      if (notice.event === "auth_error") {
        controller.abort();
        onExpired();
        return;
      }
      setState({ api, mode: notice.mode ?? "polling" });
      if (notice.event !== "update") {
        resync();
        return;
      }
      const id = notice.resource_id;
      const version = notice.version;
      if (!id || typeof version !== "number") throw new Error("无效的更新通知");
      const key = `${notice.kind}:${id}`;
      if ((versions.get(key) ?? 0) >= version) return;
      if (versions.size >= 2048 && !versions.has(key)) resync();
      versions.set(key, version);
      // Only retain deduplication state for the current resync window.
      switch (notice.kind) {
        case "experiment":
          for (const prefix of [
            "experiment",
            "summary",
            "case-runs",
            "case-run",
          ])
            queue([prefix, id]);
          queue(["experiments"]);
          queue(["comparison-experiments"]);
          break;
        case "batch":
          queue(["batch", id]);
          queue(["batches"]);
          break;
        case "attempt":
          for (const prefix of ["attempt", "events", "manifests"])
            queue([prefix, id]);
          break;
        case "observed_run":
          queue(["observed-run", id]);
          queue(["observed-runs"]);
          break;
        case "catalog":
          queue(["catalog"]);
          queue(["observation-sources"]);
          break;
        case "comparison":
          queue(["comparison-reports"]);
          queue(["comparison-report", id]);
          break;
        case "operations":
          queue(["operations"]);
          break;
      }
    };
    const fallback = setInterval(() => {
      if (Date.now() - lastSync >= 30000) resync();
    }, 30000);
    const delay = () =>
      new Promise<void>((resolve) => {
        if (signal.aborted) {
          resolve();
          return;
        }
        const timer = setTimeout(done, 5000);
        function done() {
          clearTimeout(timer);
          signal.removeEventListener("abort", done);
          resolve();
        }
        signal.addEventListener("abort", done, { once: true });
      });
    async function listen() {
      while (!signal.aborted) {
        try {
          // Same-origin fetch carries the HttpOnly session cookie.
          const response = await fetch(
            `${import.meta.env.BASE_URL}api/v1/realtime/stream`,
            {
              headers: {
                ...api!.headers,
                Accept: "text/event-stream",
              },
              signal,
            },
          );
          if (response.status === 401 || response.status === 403) {
            controller.abort();
            onExpired();
            return;
          }
          if (!response.ok || !response.body) throw new Error("实时连接不可用");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          try {
            while (!signal.aborted) {
              const { value, done } = await reader.read();
              if (done) break;
              buffer += decoder
                .decode(value, { stream: true })
                .replace(/\r\n/g, "\n");
              if (buffer.length > 65536)
                throw new Error("更新通知超出读取上限");
              let boundary: number;
              while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                const frame = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                const data = frame
                  .split("\n")
                  .filter((line) => line.startsWith("data: "))
                  .map((line) => line.slice(6))
                  .join("\n");
                if (data) handle(JSON.parse(data) as Notice);
              }
            }
          } finally {
            await reader.cancel();
          }
        } catch {
          if (signal.aborted) return;
          setState({ api, mode: "polling" });
        }
        if (!signal.aborted) {
          setState({ api, mode: "polling" });
          await delay();
        }
      }
    }
    void listen();
    return () => {
      controller.abort();
      clearInterval(fallback);
      clearTimeout(flushTimer);
    };
  }, [api, client, onExpired]);
  return !api ? "offline" : state.api === api ? state.mode : "connecting";
}
