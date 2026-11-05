import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useConnection } from "../connection";
import { formatDate } from "../format";
import type { Operations, TargetContent } from "../types";
import { Dialog, ErrorNotice, Field, JsonBlock } from "./ui";

type Check = {
  id: string;
  expires_at: string;
  status: "pending" | "completed" | "failed" | "expired";
  result: { target: TargetContent | null; error: string | null } | null;
};

export function AgentDiscoveryDialog({
  onClose,
  onManual,
}: {
  onClose: () => void;
  onManual: () => void;
}) {
  const { api } = useConnection();
  const client = useQueryClient();
  const [runner, setRunner] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [credential, setCredential] = useState("");
  const [concurrency, setConcurrency] = useState(1);
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const runners = useQuery({
    queryKey: ["operations"],
    queryFn: ({ signal }) =>
      api!.request<Operations>("/v1/operations", { signal }),
    enabled: !!api,
  });
  const status = useQuery({
    queryKey: ["catalog", "discovery", check?.id],
    queryFn: ({ signal }) =>
      api!.request<Check>(`/v1/agent-discoveries/${check!.id}`, { signal }),
    enabled: !!api && !!check,
    initialData: check ?? undefined,
  });
  const current = status.data ?? check;
  const available =
    runners.data?.runners.filter((r) =>
      r.capabilities.adapters?.includes("agent_protocol"),
    ) ?? [];
  async function inspect(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCheck(
        await api!.post<Check>("/v1/agent-discoveries", {
          name: name.trim(),
          runner_id: runner,
          base_url: url.trim(),
          credential_env: credential.trim() || null,
          concurrency,
        }),
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  async function publish() {
    setBusy(true);
    setError(null);
    try {
      await api!.post("/v1/targets", current!.result!.target);
      await client.invalidateQueries({ queryKey: ["catalog", "targets"] });
      onClose();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      wide
      title="接入标准协议 Agent"
      subtitle="从 Runner 所在网络检查连接，只读取能力，不执行任务。"
      onClose={onClose}
    >
      <form className="form" onSubmit={inspect}>
        <Field label="目标名称">
          <input
            required
            maxLength={200}
            value={name}
            disabled={busy || !!check}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field
          label="执行 Runner"
          hint="仅列出支持标准协议的 Runner；请确认该进程在线。"
        >
          <select
            required
            value={runner}
            disabled={busy || !!check || runners.isPending}
            onChange={(e) => setRunner(e.target.value)}
          >
            <option value="">选择 Runner</option>
            {available.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name} · 最近活动 {formatDate(r.last_seen_at)}
              </option>
            ))}
          </select>
        </Field>
        {runners.isPending && <p role="status">正在读取 Runner…</p>}
        {runners.error && (
          <ErrorNotice
            error={runners.error}
            retry={() => void runners.refetch()}
          />
        )}
        {runners.data && !available.length && (
          <p className="muted">
            尚无支持标准协议的 Runner。启动已配置目标 origin 白名单的 Runner
            后刷新。
          </p>
        )}
        <Field
          label="Agent 服务地址"
          hint="必须属于所选 Runner 的 origin 白名单。"
        >
          <input
            required
            type="url"
            maxLength={2000}
            placeholder="https://agent.example.com"
            value={url}
            disabled={busy || !!check}
            onChange={(e) => setUrl(e.target.value)}
          />
        </Field>
        <Field
          label="凭据环境变量名（可选）"
          hint="填写 Runner 本机已授权的变量名，如 MY_AGENT_TOKEN；不要粘贴密钥。"
        >
          <input
            pattern="[A-Za-z_][A-Za-z0-9_]*"
            value={credential}
            disabled={busy || !!check}
            onChange={(e) => setCredential(e.target.value)}
          />
        </Field>
        <Field label="目标并发上限">
          <input
            required
            type="number"
            min={1}
            max={1000}
            value={concurrency}
            disabled={busy || !!check}
            onChange={(e) => setConcurrency(Number(e.target.value))}
          />
        </Field>
        {!!error && <ErrorNotice error={error} />}
        {status.error && (
          <ErrorNotice
            error={status.error}
            retry={() => void status.refetch()}
          />
        )}
        {current?.status === "pending" && (
          <p role="status">
            等待 Runner 检查。有效期至 {formatDate(current.expires_at)}
            ；结果会自动更新。
          </p>
        )}
        {current?.status === "expired" && (
          <p role="status">检查已超时。确认 Runner 在线后重新检查。</p>
        )}
        {current?.result?.error && (
          <ErrorNotice error={new Error(current.result.error)} />
        )}
        {current?.status === "completed" && (
          <>
            <p>
              连接检查完成。以下能力将冻结在新版本中；这不代表业务执行已验证。
            </p>
            <JsonBlock
              title="待发布的目标配置"
              value={current.result?.target}
            />
          </>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={onManual}
          >
            手动配置 HTTP / Python
          </button>
          {!check ? (
            <button
              className="button primary"
              disabled={busy || !available.length}
            >
              {busy ? "正在提交…" : "检查连接"}
            </button>
          ) : (
            <>
              {current?.status === "pending" ? (
                <button
                  type="button"
                  className="button"
                  disabled={status.isFetching}
                  onClick={() => void status.refetch()}
                >
                  刷新状态
                </button>
              ) : (
                <button
                  type="button"
                  className="button"
                  disabled={busy}
                  onClick={() => {
                    setCheck(null);
                    setError(null);
                  }}
                >
                  重新检查
                </button>
              )}
              {current?.status === "completed" && (
                <button
                  type="button"
                  className="button primary"
                  disabled={busy}
                  onClick={() => void publish()}
                >
                  {busy ? "正在发布…" : "发布目标版本"}
                </button>
              )}
            </>
          )}
        </div>
      </form>
    </Dialog>
  );
}
