"""本地 Chat Completions 代理：固定上游、原样转发业务载荷、旁路记录协议事实。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from eyes.tracing.chat_protocol import capture_arguments, capture_response, tool_requests
from eyes.tracing.recorder import ExecutionRecorder, is_sensitive_field

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


class ProtocolLinks:
    """有界、进程内关联；只使用明确标识，不用相似文本猜 Query 身份。"""

    def __init__(self) -> None:
        self._salt = secrets.token_bytes(32)
        self._calls: OrderedDict[str, tuple[str, str] | None] = OrderedDict()

    def digest(self, value: Any) -> str:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
        return hmac.new(self._salt, raw, hashlib.sha256).hexdigest()

    def request_context(
        self, payload: dict[str, Any], scope: str, explicit: str | None, request_id: str
    ) -> dict[str, Any]:
        messages = payload.get("messages", [])
        # 仅当前尾部工具结果参与父请求关联；历史工具消息仍在 request 中完整记录。
        results = []
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                break
            results.append(message)
        calls_by_id = {}
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and isinstance(call.get("id"), str):
                        calls_by_id[call["id"]] = call
        explicit_session = self.digest([scope, explicit]) if explicit else None
        matches = []
        for result in results:
            call = calls_by_id.get(result.get("tool_call_id"))
            if call is not None:
                match = self._calls.get(self.digest([scope, call]))
                if match is not None and (explicit_session is None or match[1] == explicit_session):
                    matches.append(match)
        parent_ids = sorted({item[0] for item in matches})
        sessions = {item[1] for item in matches}
        fully_linked = bool(results) and len(matches) == len(results) and len(sessions) == 1
        if explicit:
            session_id = explicit_session
            basis = "explicit_header"
        elif fully_linked:
            session_id = next(iter(sessions))
            basis = "tool_call_reference"
        else:
            session_id = request_id
            basis = "independent_request"
        return {
            "request_id": request_id,
            "session_id": session_id,
            "association": basis,
            "parent_request_ids": parent_ids,
            "agent_reported_tool_results": list(reversed(results)),
            "unlinked_tool_results": len(results) - len(matches),
        }

    def remember(self, scope: str, response: Any, request_id: str, session_id: str) -> None:
        for call in tool_requests(response):
            if not isinstance(call.get("id"), str):
                continue
            key = self.digest([scope, call])
            value = (request_id, session_id)
            if key in self._calls and self._calls[key] != value:
                self._calls[key] = None  # 重复 ID/内容的来源不唯一，不选一个猜测。
            else:
                self._calls[key] = value
            self._calls.move_to_end(key)
            while len(self._calls) > 10_000:
                self._calls.popitem(last=False)


def create_proxy_app(
    *,
    upstream_base_url: str,
    output_dir: Path,
    timeout: float = 120,
    max_request_bytes: int = 8_388_608,
    max_capture_bytes: int = 1_048_576,
    redacted_fields: set[str] | None = None,
) -> FastAPI:
    parsed = urlsplit(upstream_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "upstream must be an HTTP(S) base URL without credentials, query or fragment"
        )
    if timeout <= 0 or max_request_bytes < 1 or max_capture_bytes < 128:
        raise ValueError("timeout and capture limits must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    upstream = upstream_base_url.rstrip("/") + "/chat/completions"
    # 让非法端口、控制字符等配置在启动时失败，而非留下未结束的请求轨迹。
    httpx.URL(upstream)
    links = ProtocolLinks()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Eyes Model Trace Proxy", lifespan=lifespan)
    app.state.active_runs = set()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "protocol": "chat_completions", "coverage": "model_protocol_only"}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> StreamingResponse:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_request_bytes:
                raise HTTPException(413, "request exceeds proxy body limit")
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise HTTPException(400, "request must be JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise HTTPException(400, "Chat Completions requires a messages array")
        explicit = request.headers.get("x-eyes-trace-id")
        if explicit is not None and (not explicit.strip() or len(explicit) > 128):
            raise HTTPException(400, "X-Eyes-Trace-ID must contain 1..128 characters")
        authorization = request.headers.get("authorization", "")
        scope = links.digest(
            [
                authorization,
                request.headers.get("openai-project"),
                request.headers.get("openai-organization"),
            ]
        )
        request_id = uuid4().hex
        try:
            context = links.request_context(payload, scope, explicit, request_id)
        except TypeError, AttributeError:
            context = {
                "request_id": request_id,
                "session_id": request_id,
                "association": "invalid_tool_history",
                "parent_request_ids": [],
            }
        auth_parts = authorization.split(maxsplit=1)
        credential = auth_parts[-1] if auth_parts else ""
        known_secrets = {authorization, credential} - {""}
        known_secrets.update(
            value
            for key, value in request.query_params.multi_items()
            if value and is_sensitive_field(key, value, redacted_fields or ())
        )
        try:
            recorder = ExecutionRecorder(
                output=output_dir / f"{request_id}.jsonl",
                source="model_proxy",
                active_runs=app.state.active_runs,
                secret_values=known_secrets,
                redacted_fields=redacted_fields,
                max_data_chars=max_capture_bytes,
            )
        except OSError as exc:
            raise HTTPException(503, "cannot initialize trace capture") from exc
        fields = {
            "span_id": request_id,
            "parent_span_id": None,
            "kind": "model",
            "name": "chat.completions",
        }
        recorder.emit(
            **fields,
            phase="start",
            status="running",
            data={
                "protocol": "chat_completions",
                **context,
                "query_parameters": {
                    key: request.query_params.getlist(key) for key in request.query_params
                },
                "request": capture_arguments(payload),
            },
        )
        started = time.perf_counter()
        headers = {
            name: value
            for name, value in request.headers.items()
            if name
            in {
                "authorization",
                "content-type",
                "accept",
                "openai-organization",
                "openai-project",
                "user-agent",
                "idempotency-key",
            }
        }
        headers["accept-encoding"] = "identity"
        client: httpx.AsyncClient = request.app.state.client
        upstream_request = client.build_request(
            "POST",
            httpx.URL(upstream).copy_with(query=request.scope.get("query_string", b"")),
            content=bytes(body),
            headers=headers,
        )
        # 共享连接池不能将上一个调用方的上游 Cookie 带到下一个调用方。
        upstream_request.headers.pop("cookie", None)
        try:
            upstream_response = await client.send(
                upstream_request,
                stream=True,
            )
        except (httpx.HTTPError, asyncio.CancelledError) as exc:
            recorder.emit(
                **fields,
                phase="end",
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                duration_ms=(time.perf_counter() - started) * 1000,
                data={"error_type": type(exc).__name__},
            )
            recorder.finish()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(502, "upstream connection failed; see trace") from exc

        connection_tokens = {
            token.strip().lower()
            for token in upstream_response.headers.get("connection", "").split(",")
        }
        response_headers = {
            name: value
            for name, value in upstream_response.headers.items()
            if name not in _HOP_HEADERS | connection_tokens | {"set-cookie"}
        }
        response_headers["x-eyes-request-id"] = request_id
        response_headers["x-eyes-session-id"] = context["session_id"]
        is_stream = "text/event-stream" in upstream_response.headers.get("content-type", "")

        captured = bytearray()
        truncated = False
        status = "ok" if upstream_response.is_success else "error"
        error_type: str | None = None
        eof = False
        stream_registered = False
        processing_issues: list[str] = []

        def register_stream_end(part: bytes, tail: bytes) -> None:
            nonlocal stream_registered
            if (
                is_stream
                and not truncated
                and not stream_registered
                and upstream_response.is_success
                and b"[DONE]" in tail + part
            ):
                response, issues = capture_response(bytes(captured), streaming=True)
                if isinstance(response, dict) and response.get("stream_done"):
                    stream_registered = True
                    if not issues and not response.get("error"):
                        links.remember(scope, response, request_id, context["session_id"])

        async def relay() -> AsyncGenerator[bytes]:
            nonlocal truncated, status, error_type, eof
            tail = b""
            try:
                async for part in upstream_response.aiter_bytes():
                    remaining = max_capture_bytes - len(captured)
                    captured.extend(part[:remaining])
                    truncated = truncated or len(part) > remaining
                    # SDK 可能一读到 [DONE] 就断开并开始下一请求，必须先保存协议引用。
                    try:
                        register_stream_end(part, tail)
                    except Exception:  # noqa: BLE001 -- do not interrupt forwarding for telemetry
                        processing_issues.append("stream_link_failed")
                    tail = (tail + part)[-16:]
                    yield part
                eof = True
            except asyncio.CancelledError:
                error_type = "CancelledError"
                if not stream_registered:
                    status = "cancelled"
                raise
            except Exception as exc:
                status, error_type = "error", type(exc).__name__
                raise

        def finish_capture() -> None:
            nonlocal status
            try:
                response, issues = capture_response(bytes(captured), streaming=is_stream)
                issues.extend(sorted(set(processing_issues)))
                if truncated:
                    issues.append("capture_byte_limit")
                if isinstance(response, dict) and response.get("error"):
                    status = "error"
                if (
                    is_stream
                    and "missing_stream_done" in issues
                    and status == "ok"
                    and not truncated
                ):
                    status = "error"
                if eof and status == "ok" and not issues and not stream_registered:
                    links.remember(scope, response, request_id, context["session_id"])
                data = {
                    "http_status": upstream_response.status_code,
                    "response": capture_arguments(response),
                    "model_requested_tools": capture_arguments(tool_requests(response)),
                    "tool_execution_observed": False,
                    "capture_issues": issues,
                    "error_type": error_type,
                }
            except Exception as exc:  # noqa: BLE001 -- observation must not prevent cleanup
                data = {
                    "capture_issues": ["capture_processing_failed"],
                    "capture_error_type": type(exc).__name__,
                    "error_type": error_type,
                }
            recorder.emit(
                **fields,
                phase="end",
                status=status,
                duration_ms=(time.perf_counter() - started) * 1000,
                data=data,
            )
            recorder.finish()

        iterator = relay()

        class ProxyResponse(StreamingResponse):
            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                nonlocal status, error_type
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    if not eof and error_type is None:
                        error_type = "DownstreamDisconnected"
                        if not stream_registered:
                            status = "cancelled"
                    # BackgroundTask 在发送 headers/body 失败时不会执行。
                    # 在响应生命周期的 finally 中兜底，连生成器尚未开始的情况也覆盖。
                    with anyio.CancelScope(shield=True):
                        try:
                            await iterator.aclose()
                            finish_capture()
                        finally:
                            await upstream_response.aclose()

        return ProxyResponse(
            iterator,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    return app
