import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useConnection } from "../connection";
import { formatDate } from "../format";
import { AgentDiscoveryDialog } from "./AgentDiscoveryDialog";
import { Dialog, ErrorNotice, Field } from "./ui";

type Pairing = {
  id: string;
  name: string;
  code?: string;
  expires_at: string;
  status: "pending" | "expired" | "online" | "offline" | "revoked";
  target_version_id: string | null;
  last_seen_at: string | null;
};
const labels = {
  pending: "等待配对",
  expired: "配对码已过期",
  online: "在线",
  offline: "已配对 · 离线",
  revoked: "已撤销",
};
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;

export function AgentConnectDialog({
  onClose,
  onManual,
}: {
  onClose: () => void;
  onManual: () => void;
}) {
  const { api } = useConnection();
  const client = useQueryClient();
  const [remote, setRemote] = useState(false);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"python" | "http" | "tunnel">("tunnel");
  const [platform, setPlatform] = useState("darwin-arm64");
  const [protocol, setProtocol] = useState("json");
  const [modelUpstream, setModelUpstream] = useState("");
  const tunnelConfig = useQuery({
    queryKey: ["tunnel-config"],
    queryFn: ({ signal }) =>
      api!.request<{ enabled: boolean }>("/v1/tunnel/config", { signal }),
    enabled: !!api,
  });
  const [entry, setEntry] = useState("");
  const [url, setUrl] = useState("");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const connections = useQuery({
    queryKey: ["agent-pairings"],
    queryFn: ({ signal }) =>
      api!.request<{ items: Pairing[] }>("/v1/agent-pairings", { signal }),
    enabled: !!api && !remote,
    refetchInterval: 3000,
  });
  const current =
    connections.data?.items.find((item) => item.id === pairing?.id) ?? pairing;
  const server = new URL(
    `${import.meta.env.BASE_URL}api`,
    window.location.origin,
  ).href;
  const windows = platform === "windows-amd64";
  const shellQuote = (value: string) =>
    windows ? `'${value.replaceAll("'", "''")}'` : quote(value);
  const binary = windows ? "eyes-tunnel.exe" : "eyes-tunnel";
  const download = new URL(
    `${import.meta.env.BASE_URL}tunnel/downloads/eyes-tunnel-${platform}${windows ? ".exe" : ""}`,
    window.location.origin,
  ).href;
  const command = pairing
    ? mode === "tunnel"
      ? `${windows ? ".\\" : "./"}${binary} --server ${shellQuote(server)} --pair-code ${shellQuote(pairing.code!)} --local ${shellQuote(url.trim())} --protocol ${protocol}${modelUpstream.trim() ? ` --model-upstream ${shellQuote(modelUpstream.trim())}` : ""}`
      : `eyes connect --server ${quote(server)} --pair-code ${quote(pairing.code!)} ${mode === "python" ? `--entry ${quote(entry.trim())}` : `--url ${quote(url.trim())}`}`
    : "";

  async function create(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setPairing(
        await api!.post<Pairing>("/v1/agent-pairings", { name: name.trim() }),
      );
      setCopied(false);
      await client.invalidateQueries({ queryKey: ["agent-pairings"] });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id: string) {
    setBusy(true);
    setError(null);
    try {
      await api!.post(`/v1/agent-pairings/${id}/revoke`);
      await client.invalidateQueries({ queryKey: ["agent-pairings"] });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch (cause) {
      setError(cause);
    }
  }
  async function finish() {
    await client.invalidateQueries({ queryKey: ["catalog", "targets"] });
    onClose();
  }
  if (remote)
    return <AgentDiscoveryDialog onClose={onClose} onManual={onManual} />;
  return (
    <Dialog
      wide
      title="连接你的 Agent"
      subtitle="在 Agent 所在机器主动连接 Eyes，无需公网地址或开放入站端口。"
      onClose={() => void finish()}
    >
      <form className="form" onSubmit={create}>
        <Field label="Agent 名称">
          <input
            required
            maxLength={200}
            value={name}
            disabled={busy || !!pairing}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label="本地调用方式">
          <select
            value={mode}
            disabled={busy || !!pairing}
            onChange={(e) =>
              setMode(e.target.value as "python" | "http" | "tunnel")
            }
          >
            <option value="tunnel">HTTP 隧道 · 无需 Python</option>
            <option value="python">Python 函数</option>
            <option value="http">HTTP 接口 · 原有 Runner</option>
          </select>
        </Field>
        {mode === "python" ? (
          <Field
            label="Python 入口"
            hint="函数签名 execute(input, context)，返回 JSON 对象；同步和 async 均可。"
          >
            <input
              required
              pattern="[A-Za-z_][\w.]*:[A-Za-z_][\w.]*"
              placeholder="my_agent.integration:execute"
              value={entry}
              disabled={busy || !!pairing}
              onChange={(e) => setEntry(e.target.value)}
            />
          </Field>
        ) : (
          <Field
            label="Agent 机器可访问的 HTTP 地址"
            hint="POST 输入 JSON，返回 JSON 对象。认证和嵌套输出可通过命令参数配置。"
          >
            <input
              required
              type="url"
              placeholder="http://127.0.0.1:9000/run"
              value={url}
              disabled={busy || !!pairing}
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
        )}
        {mode === "tunnel" && (
          <>
            <Field label="Agent 所在机器的系统">
              <select
                value={platform}
                disabled={busy || !!pairing}
                onChange={(e) => setPlatform(e.target.value)}
              >
                <option value="darwin-arm64">macOS · Apple Silicon</option>
                <option value="linux-amd64">Linux · amd64</option>
                <option value="windows-amd64">
                  Windows · amd64（PowerShell）
                </option>
              </select>
            </Field>
            <Field
              label="接口协议"
              hint="测试用例输入须符合所选接口的请求格式。"
            >
              <select
                value={protocol}
                disabled={busy || !!pairing}
                onChange={(e) => setProtocol(e.target.value)}
              >
                <option value="json">HTTP JSON</option>
                <option value="chat_completions">
                  OpenAI Chat Completions · 支持 SSE
                </option>
              </select>
            </Field>
            <Field
              label="原模型 API 地址（可选）"
              hint="填写后启用模型通信观测。将 Agent 的模型 base URL 改为 http://127.0.0.1:8787/v1；仅展示经过代理的通信，不能证明工具实际执行。"
            >
              <input
                type="url"
                value={modelUpstream}
                placeholder="https://模型服务地址/v1"
                disabled={busy || !!pairing}
                onChange={(e) => setModelUpstream(e.target.value)}
              />
            </Field>
            <p className="muted">
              下载独立连接程序，在 Agent 机器上保持运行。无需安装 Python 或 Eyes
              包；默认串行执行，密钥保留在本地。
            </p>
            {tunnelConfig.isPending && (
              <p role="status">正在检查隧道服务配置…</p>
            )}
            {tunnelConfig.error && (
              <ErrorNotice
                error={tunnelConfig.error}
                retry={() => void tunnelConfig.refetch()}
              />
            )}
            {tunnelConfig.data && !tunnelConfig.data.enabled && (
              <p role="status">
                管理员尚未启用隧道部署，可先使用原有接入方式。
              </p>
            )}
          </>
        )}
        {mode !== "tunnel" && (
          <p className="muted">
            在 macOS 或 Linux 上安装与平台同版本的 Eyes（Python ≥3.14），然后在
            Agent 项目目录执行命令。默认串行运行；模型密钥保留在你的机器。
          </p>
        )}
        {!!error && <ErrorNotice error={error} />}
        {!pairing && (
          <div className="form-actions">
            <button
              className="button primary"
              disabled={
                busy ||
                !name.trim() ||
                (mode === "tunnel" && !tunnelConfig.data?.enabled)
              }
            >
              {busy ? "正在生成…" : "生成连接命令"}
            </button>
          </div>
        )}
        {pairing && (
          <>
            <p role="status">
              {labels[current!.status]}
              {current?.status === "pending" &&
                ` · 配对码有效期至 ${formatDate(pairing.expires_at)}`}
            </p>
            {current?.status === "pending" && (
              <>
                {mode === "tunnel" && (
                  <p>
                    <a className="text-link" href={download} download={binary}>
                      下载 {binary}
                    </a>
                    {!windows && (
                      <>
                        {" "}
                        · 下载后先运行 <code>chmod +x eyes-tunnel</code>
                      </>
                    )}
                  </p>
                )}
                <Field
                  label="连接命令"
                  hint="配对码只能绑定一个连接，请勿分享。关闭后不再显示配对码。"
                >
                  <textarea
                    readOnly
                    rows={4}
                    value={command}
                    aria-label="连接命令"
                  />
                </Field>
                <button
                  type="button"
                  className="button"
                  onClick={() => void copy()}
                >
                  {copied ? "已复制" : "复制命令"}
                </button>
              </>
            )}
            {current?.target_version_id && (
              <p>
                目标已注册。在线只表示连接进程最近有活动；业务执行需在创建实验后验证。停止进程后，可用{" "}
                <code>
                  {mode === "tunnel"
                    ? `${windows ? ".\\" : "./"}${binary}`
                    : "eyes connect"}
                </code>{" "}
                在原目录重新连接。
              </p>
            )}
            {(current?.status === "expired" ||
              current?.status === "revoked") && (
              <button
                type="button"
                className="button"
                onClick={() => {
                  setPairing(null);
                  setError(null);
                }}
              >
                重新生成
              </button>
            )}
          </>
        )}
      </form>
      <section className="form" aria-label="已创建的连接">
        <h3>连接状态</h3>
        <p className="muted">
          每 3 秒更新，30
          秒无活动显示离线。撤销会禁止继续领取和上报，但不证明正在执行的 Agent
          已停止。
        </p>
        {connections.isPending && <p role="status">正在读取连接…</p>}
        {connections.error && (
          <ErrorNotice
            error={connections.error}
            retry={() => void connections.refetch()}
          />
        )}
        {connections.data?.items.length === 0 && (
          <p className="muted">尚未创建连接。</p>
        )}
        {connections.data?.items.map((item) => (
          <div
            className="flex items-center justify-between gap-4"
            key={item.id}
          >
            <span>
              {item.name} · {labels[item.status]}
            </span>
            {item.status !== "revoked" && item.status !== "expired" && (
              <button
                type="button"
                className="button small"
                disabled={busy}
                onClick={() => void revoke(item.id)}
              >
                撤销{item.target_version_id ? "连接" : "配对码"}
              </button>
            )}
          </div>
        ))}
      </section>
      <div className="form-actions">
        <button
          type="button"
          className="button"
          disabled={busy || !!pairing}
          onClick={() => setRemote(true)}
        >
          连接标准协议服务
        </button>
        <button
          type="button"
          className="button"
          disabled={busy || !!pairing}
          onClick={onManual}
        >
          手动配置
        </button>
        <button
          type="button"
          className="button primary"
          onClick={() => void finish()}
        >
          完成
        </button>
      </div>
    </Dialog>
  );
}
