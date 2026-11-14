import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Info, Radio, SlidersHorizontal } from "lucide-react";
import { useConnection } from "../connection";
import { formatDate } from "../format";
import { AgentDiscoveryDialog } from "./AgentDiscoveryDialog";
import { Dialog, ErrorNotice, Field } from "./ui";
import { FieldLegend, FieldSet } from "./ui/field";

type Purpose = "observe" | "evaluate";
type Pairing = {
  id: string;
  name: string;
  purpose: Purpose;
  code?: string;
  expires_at: string;
  status: "pending" | "expired" | "online" | "offline" | "revoked";
  target_version_id: string | null;
  observation_source_id: string | null;
  last_seen_at: string | null;
};
const connectionPurposes = [
  {
    value: "observe",
    icon: Radio,
    title: "仅观测",
    description: "仅上传模型通信，Agent 自己执行任务。",
  },
  {
    value: "evaluate",
    icon: SlidersHorizontal,
    title: "允许评测",
    description: "允许 Eyes 调用任务接口，可同时开启模型观测。",
  },
] as const;
const labels = {
  pending: "等待配对",
  expired: "配对码已过期",
  online: "在线",
  offline: "已配对 · 离线",
  revoked: "已撤销",
};

export function AgentConnectDialog({
  onClose,
  onManual,
  defaultPurpose = "evaluate",
}: {
  onClose: () => void;
  onManual: () => void;
  defaultPurpose?: Purpose;
}) {
  const { api } = useConnection();
  const client = useQueryClient();
  const [remote, setRemote] = useState(false);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState<Purpose>(defaultPurpose);
  const [platform, setPlatform] = useState("darwin-arm64");
  const [protocol, setProtocol] = useState("json");
  const [modelUpstream, setModelUpstream] = useState("");
  const [url, setUrl] = useState("");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const tunnelConfig = useQuery({
    queryKey: ["tunnel-config"],
    queryFn: ({ signal }) =>
      api!.request<{ enabled: boolean }>("/v1/tunnel/config", { signal }),
    enabled: !!api,
  });
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
    windows
      ? `'${value.replaceAll("'", "''")}'`
      : `'${value.replaceAll("'", "'\\''")}'`;
  const binary = windows ? "eyes-tunnel.exe" : "eyes-tunnel";
  const executable = `${windows ? ".\\" : "./"}${binary}`;
  const download = new URL(
    `${import.meta.env.BASE_URL}tunnel/downloads/eyes-tunnel-${platform}${windows ? ".exe" : ""}`,
    window.location.origin,
  ).href;
  const command = pairing
    ? `${executable} --server ${shellQuote(server)} --pair-code ${shellQuote(pairing.code!)}${purpose === "observe" ? " --observe-only" : ` --local ${shellQuote(url.trim())} --protocol ${protocol}`}${modelUpstream.trim() ? ` --model-upstream ${shellQuote(modelUpstream.trim())}` : ""}`
    : "";

  async function create(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setPairing(
        await api!.post<Pairing>("/v1/agent-pairings", {
          name: name.trim(),
          purpose,
        }),
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
      await Promise.all([
        client.invalidateQueries({ queryKey: ["agent-pairings"] }),
        client.invalidateQueries({ queryKey: ["observation-sources"] }),
      ]);
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
    await Promise.all([
      client.invalidateQueries({ queryKey: ["catalog", "targets"] }),
      client.invalidateQueries({ queryKey: ["observation-sources"] }),
    ]);
    onClose();
  }
  if (remote)
    return <AgentDiscoveryDialog onClose={onClose} onManual={onManual} />;
  return (
    <Dialog
      wide
      title="连接你的 Agent"
      subtitle="同一个连接程序，按用途选择权限。配对后自动关联名称与观测来源。"
      onClose={() => void finish()}
    >
      <div className="connect-body">
        <form className="connect-form" onSubmit={create}>
          <FieldSet className="purpose-fieldset" disabled={busy || !!pairing}>
            <FieldLegend variant="label">接入用途</FieldLegend>
            <div className="connect-fields purpose-options">
              {connectionPurposes.map(
                ({ value, icon: Icon, title, description }) => (
                  <label className="purpose-option" key={value}>
                    <Icon size={18} aria-hidden="true" />
                    <strong>{title}</strong>
                    <input
                      type="radio"
                      name="connection-purpose"
                      value={value}
                      checked={purpose === value}
                      onChange={() => setPurpose(value)}
                      aria-label={title}
                      aria-describedby={`purpose-${value}-hint`}
                    />
                    <small id={`purpose-${value}-hint`}>{description}</small>
                  </label>
                ),
              )}
            </div>
          </FieldSet>
          <div className="connect-fields">
            <Field
              label="Agent 名称"
              hint="观测记录、连接状态和评测目标共用此名称。"
            >
              <input
                required
                maxLength={200}
                value={name}
                disabled={busy || !!pairing}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field
              label="Agent 所在机器的系统"
              hint="在 Agent 所在机器持续运行连接程序，无需安装 Python 或 Eyes 包。每个 Agent 使用独立目录保存连接状态。"
            >
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
          </div>
          {purpose === "evaluate" && (
            <div className="connect-fields">
              <Field
                label="Agent 任务接口"
                hint="填写 Agent 所在机器可访问的完整 HTTP 地址。此地址接收评测任务，不是模型 API 地址。"
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
              <Field
                label="任务接口协议"
                hint="测试用例输入须符合该接口的请求格式。"
              >
                <select
                  value={protocol}
                  disabled={busy || !!pairing}
                  onChange={(e) => setProtocol(e.target.value)}
                >
                  <option value="json">HTTP JSON</option>
                  <option value="chat_completions">
                    Chat Completions · 支持 SSE
                  </option>
                </select>
              </Field>
            </div>
          )}
          <Field
            label={
              purpose === "observe"
                ? "原模型 API 地址"
                : "原模型 API 地址（可选，同时开启观测）"
            }
            hint="填写当前使用的 Chat Completions API 根地址。连接后将模型 base URL 改为 http://127.0.0.1:8787/v1，保留原模型名称与 API Key。"
          >
            <input
              type="url"
              required={purpose === "observe"}
              value={modelUpstream}
              placeholder="https://模型服务地址/v1"
              disabled={busy || !!pairing}
              onChange={(e) => setModelUpstream(e.target.value)}
            />
          </Field>
          <p className="connect-scope">
            <Info size={16} aria-hidden="true" />
            <span>
              模型观测会上传经过代理的请求与响应正文。工具执行过程需由 Agent
              单独上报。
            </span>
          </p>
          {tunnelConfig.isPending && <p role="status">正在检查连接服务…</p>}
          {tunnelConfig.error && (
            <ErrorNotice
              error={tunnelConfig.error}
              retry={() => void tunnelConfig.refetch()}
            />
          )}
          {tunnelConfig.data && !tunnelConfig.data.enabled && (
            <p role="status">管理员尚未启用连接服务。</p>
          )}
          {!!error && <ErrorNotice error={error} />}
          {!pairing && (
            <div className="connect-actions">
              <button
                className="button primary"
                disabled={busy || !name.trim() || !tunnelConfig.data?.enabled}
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
              {current?.observation_source_id && (
                <p>
                  已关联观测来源，无需再创建令牌。
                  <Link
                    className="text-link"
                    to={`/observe?source=${current.observation_source_id}`}
                    onClick={() => void finish()}
                  >
                    查看 {current.name} 的观测
                  </Link>
                </p>
              )}
              {current?.status === "online" || current?.status === "offline" ? (
                <p>
                  停止连接程序后，在原目录运行 <code>{executable}</code>{" "}
                  即可重新连接。在线仅表示连接有活动，不代表已采集模型通信或完成评测。
                </p>
              ) : null}
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
        {connections.error && (
          <ErrorNotice
            error={connections.error}
            retry={() => void connections.refetch()}
          />
        )}
        <details className="connect-disclosure">
          <summary>配对记录</summary>
          <div className="connect-history">
            <p className="muted">
              每 3 秒更新，30
              秒无活动显示离线。撤销同时停用该连接及其观测上报，不代表 Agent
              已停止。
            </p>
            {connections.isPending && <p role="status">正在读取配对记录…</p>}
            {connections.data?.items.length === 0 && (
              <p className="muted">暂无配对记录。</p>
            )}
            {connections.data?.items.map((item) => (
              <div
                className="flex items-center justify-between gap-4"
                key={item.id}
              >
                <div>
                  <span>
                    {item.name} ·{" "}
                    {item.purpose === "observe" ? "仅观测" : "允许评测"} ·{" "}
                    {labels[item.status]}
                  </span>
                  {item.observation_source_id && (
                    <p>
                      <Link
                        className="text-link"
                        to={`/observe?source=${item.observation_source_id}`}
                        onClick={() => void finish()}
                      >
                        查看观测
                      </Link>
                    </p>
                  )}
                </div>
                {item.status !== "revoked" && item.status !== "expired" && (
                  <button
                    type="button"
                    className="button small"
                    disabled={busy}
                    onClick={() => void revoke(item.id)}
                  >
                    撤销{item.status === "pending" ? "配对码" : "连接"}
                  </button>
                )}
              </div>
            ))}
          </div>
        </details>
        <details className="connect-disclosure">
          <summary>高级接入与旧版兼容</summary>
          <div className="connect-options">
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
              手动配置评测目标
            </button>
          </div>
        </details>
        {pairing && (
          <div className="connect-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => void finish()}
            >
              完成
            </button>
          </div>
        )}
      </div>
    </Dialog>
  );
}
