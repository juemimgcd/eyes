import { useEffect, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { ArrowRight, Eye as EyeIcon, EyeOff, LoaderCircle } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Api, ApiError } from "../api";
import { ErrorNotice, Eye, Loading } from "./ui";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "./ui/input-group";
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

function PasswordInput({
  toggleLabel,
  ...props
}: ComponentProps<typeof Input> & { toggleLabel: string }) {
  const [visible, setVisible] = useState(false);
  return (
    <InputGroup>
      <InputGroupInput {...props} type={visible ? "text" : "password"} />
      <InputGroupAddon align="inline-end">
        <InputGroupButton
          size="icon-sm"
          aria-label={toggleLabel}
          aria-pressed={visible}
          aria-controls={props.id}
          disabled={props.disabled}
          onClick={() => setVisible(!visible)}
        >
          {visible ? <EyeOff /> : <EyeIcon />}
        </InputGroupButton>
      </InputGroupAddon>
    </InputGroup>
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
        <header className="account-heading">
          <div className="console-brand" aria-label="Eyes">
            <Eye />
            <span>eyes.</span>
          </div>
          <h1 id="account-title">{register ? "注册账号" : "登录"}</h1>
        </header>
        <form className="form" onSubmit={submit}>
          <FieldGroup>
            <Field data-disabled={busy}>
              <FieldLabel htmlFor="username">用户名</FieldLabel>
              <Input
                id="username"
                autoComplete="username"
                required
                aria-describedby={register ? "username-help" : undefined}
                minLength={3}
                maxLength={40}
                pattern="[a-zA-Z0-9_][a-zA-Z0-9_.\-]*"
                value={username}
                disabled={busy}
                onChange={(event) => setUsername(event.target.value)}
              />
              {register && (
                <FieldDescription id="username-help">
                  3–40
                  位，可用英文字母、数字、下划线、点和短横线。点和短横线不能开头。
                </FieldDescription>
              )}
            </Field>
            <Field data-disabled={busy}>
              <FieldLabel htmlFor="password">密码</FieldLabel>
              <PasswordInput
                key={register ? "new" : "current"}
                toggleLabel="显示密码"
                id="password"
                autoComplete={register ? "new-password" : "current-password"}
                required
                aria-describedby={register ? "password-help" : undefined}
                minLength={register ? 12 : 1}
                maxLength={128}
                value={password}
                disabled={busy}
                onChange={(event) => setPassword(event.target.value)}
              />
              {register && (
                <FieldDescription id="password-help">
                  12–128 个字符。
                </FieldDescription>
              )}
            </Field>
            {register && (
              <Field data-disabled={busy}>
                <FieldLabel htmlFor="confirm-password">确认密码</FieldLabel>
                <PasswordInput
                  toggleLabel="显示确认密码"
                  id="confirm-password"
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
          <Button
            className="account-submit"
            type="submit"
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? "正在处理…" : register ? "创建账号" : "登录"}
            {busy ? <LoaderCircle className="spin" /> : <ArrowRight />}
          </Button>
          <div className="account-switch">
            <span>{register ? "已有账号？" : "还没有账号？"}</span>
            <Button
              type="button"
              variant="link"
              disabled={busy}
              onClick={() => {
                setRegister(!register);
                setError(null);
                setPassword("");
                setConfirm("");
              }}
            >
              {register ? "返回登录" : "注册"}
            </Button>
          </div>
        </form>
      </section>
    </main>
  );
}
