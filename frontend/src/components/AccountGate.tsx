import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Api, ApiError } from "../api";
import { ErrorNotice, Eye, Loading } from "./ui";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "./ui/field";

export type Account = { id: string; username: string; project_id: string };
type Session = { account: Account; expires_at: string };
const anonymous = new Api();

export function AccountGate({
  children,
}: {
  children: (
    api: Api,
    account: Account,
    onLogout: () => Promise<void>,
  ) => ReactNode;
}) {
  const cache = useQueryClient();
  const [session, setSession] = useState<{ api: Api; account: Account } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const clear = () => {
      cache.clear();
      setSession(null);
    };
    const changed = () => {
      clear();
      setLoading(true);
      setRetry((value) => value + 1);
    };
    const channel = new BroadcastChannel("eyes-account");
    channel.onmessage = changed;
    window.addEventListener("eyes-session-expired", clear);
    anonymous
      .request<Session>("/v1/auth/me", { signal: controller.signal })
      .then(({ account }) => {
        if (!controller.signal.aborted) {
          cache.clear();
          setSession({ api: new Api("", account.id), account });
          setError(null);
        }
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401)
          setError(null);
        else setError(failure);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      channel.close();
      window.removeEventListener("eyes-session-expired", clear);
    };
  }, [cache, retry]);
  function announce() {
    const channel = new BroadcastChannel("eyes-account");
    channel.postMessage("changed");
    channel.close();
  }
  async function logout() {
    await session!.api.post("/v1/auth/logout");
    cache.clear();
    setSession(null);
    announce();
  }
  if (loading)
    return (
      <main className="account-page">
        <Loading label="正在恢复登录状态" />
      </main>
    );
  if (session) return children(session.api, session.account, logout);
  return (
    <LoginForm
      error={error}
      onRetry={() => {
        setLoading(true);
        setRetry((value) => value + 1);
      }}
      onAuthenticated={({ account }) => {
        cache.clear();
        setError(null);
        setSession({ api: new Api("", account.id), account });
        announce();
      }}
    />
  );
}

function LoginForm({
  error: connectionError,
  onRetry,
  onAuthenticated,
}: {
  error: unknown;
  onRetry: () => void;
  onAuthenticated: (value: Session) => void;
}) {
  const [register, setRegister] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (register && password !== confirm) {
      setError(new Error("两次输入的密码不一致。"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await anonymous.post<Session>(
        `/v1/auth/${register ? "register" : "login"}`,
        { username, password },
      );
      setPassword("");
      setConfirm("");
      onAuthenticated(result);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="account-page">
      <section className="account-form" aria-labelledby="account-title">
        <div className="console-brand">
          <Eye />
          <span>eyes.</span>
        </div>
        <header className="account-heading">
          <p className="account-eyebrow">AGENT 测试与执行观测</p>
          <h1 id="account-title">
            {register ? "创建你的工作空间" : "登录 Eyes"}
          </h1>
          <p>
            {register
              ? "注册个人账号，开始接入 Agent 和记录实验。"
              : "回到你的 Agent、实验和执行记录。"}
          </p>
        </header>
        <form className="form" onSubmit={submit}>
          <FieldGroup>
            <Field data-disabled={busy}>
              <FieldLabel htmlFor="username">用户名</FieldLabel>
              <Input
                id="username"
                autoComplete="username"
                required
                minLength={3}
                maxLength={40}
                pattern="[a-zA-Z0-9_][a-zA-Z0-9_.\-]*"
                value={username}
                disabled={busy}
                onChange={(event) => setUsername(event.target.value)}
              />
              {register && (
                <FieldDescription>
                  3–40 位字母、数字、下划线，可包含点和短横线；不区分大小写。
                </FieldDescription>
              )}
            </Field>
            <Field data-disabled={busy}>
              <FieldLabel htmlFor="password">密码</FieldLabel>
              <Input
                id="password"
                type="password"
                autoComplete={register ? "new-password" : "current-password"}
                required
                minLength={register ? 12 : 1}
                maxLength={128}
                value={password}
                disabled={busy}
                onChange={(event) => setPassword(event.target.value)}
              />
              {register && (
                <FieldDescription>
                  使用 12–128 位密码，建议使用密码管理器生成。
                </FieldDescription>
              )}
            </Field>
            {register && (
              <Field data-disabled={busy}>
                <FieldLabel htmlFor="confirm-password">确认密码</FieldLabel>
                <Input
                  id="confirm-password"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirm}
                  disabled={busy}
                  onChange={(event) => setConfirm(event.target.value)}
                />
              </Field>
            )}
          </FieldGroup>
          {error != null && <ErrorNotice error={error} />}
          {connectionError != null && (
            <>
              <ErrorNotice error={connectionError} />
              <Button variant="outline" type="button" onClick={onRetry}>
                重新连接服务
              </Button>
            </>
          )}
          <Button type="submit" disabled={busy}>
            {busy ? "正在处理…" : register ? "创建账号" : "登录"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setRegister(!register);
              setError(null);
              setPassword("");
              setConfirm("");
            }}
          >
            {register ? "已有账号？返回登录" : "还没有账号？注册"}
          </Button>
        </form>
        <p className="account-note">
          每个账号拥有独立工作空间，数据仅对该账号及其授权的接入凭据开放。
        </p>
      </section>
    </main>
  );
}
