"""Bounded async HTTP transport, shared by discovery and Runner execution.

Only protocol messages cross this boundary; Agent code never runs in the Runner
process. Every client belongs to one base URL and one resolved credential.
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eyes.agent_protocol.schema import input_validator
from eyes.contracts.agent_protocol import (
    TERMINAL_STATES,
    AgentArtifact,
    AgentCancelResult,
    AgentCapabilities,
    AgentError,
    AgentEventPage,
    AgentRun,
    AgentTask,
)
from eyes.contracts.target import http_origin


class ProtocolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    capabilities: AgentCapabilities
    poll_seconds: float = Field(default=1, ge=0.1, le=60)

    @model_validator(mode="after")
    def valid_base(self):
        http_origin(self.base_url)
        parts = urlsplit(self.base_url)
        if parts.query or parts.fragment:
            raise ValueError("Agent base URL cannot contain a query or fragment")
        self.base_url = self.base_url.rstrip("/")
        input_validator(self.capabilities.input_schema)
        return self


class AgentHttpError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: float = 1):
        self.status, self.code = status, code
        self.retry_after = retry_after
        super().__init__(f"Agent returned {status} ({code}): {message}")


class AgentResponseTooLarge(ValueError):
    """The response exceeded the transport byte budget."""


class AgentClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        allowed_origins: list[str],
        connections: int,
        timeout: float,
        pool_timeout: float,
        max_response_bytes: int,
    ):
        # This is the network entry boundary. No redirects or ambient proxies can
        # take an authorized target request to a different origin.
        origin = http_origin(base_url)
        parts = urlsplit(base_url)
        if origin not in allowed_origins or parts.query or parts.fragment:
            raise ValueError("Agent base URL is outside the Runner origin allowlist")
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
            timeout=httpx.Timeout(timeout, pool=pool_timeout),
            follow_redirects=False,
            trust_env=False,
        )
        self.snapshots: dict[UUID, AgentRun] = {}

    async def _request(self, method, path, *, json=None, params=None, max_bytes=None):
        limit = self.max_response_bytes if max_bytes is None else max_bytes
        async with (
            asyncio.timeout(self.timeout),
            self.http.stream(method, path, json=json, params=params) as response,
        ):
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise AgentResponseTooLarge("Agent response exceeds the configured byte limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            if response.status_code >= 300:
                problem = AgentError.model_validate_json(body)
                retry_after = response.headers.get("Retry-After", "1")
                delay = (
                    float(retry_after)
                    if retry_after.isdigit()
                    else (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
                )
                raise AgentHttpError(
                    response.status_code, problem.error.code, problem.error.message, max(0.1, delay)
                )
            return response.status_code, body

    async def capabilities(self) -> AgentCapabilities:
        _, body = await self._request("GET", "agent/v1/capabilities")
        return AgentCapabilities.model_validate_json(body)

    def _snapshot(self, body, task: AgentTask, run_id: UUID | None = None):
        run = AgentRun.model_validate_json(body)
        if run.task_id != task.task_id or run.attempt_id != task.attempt_id:
            raise ValueError("Agent returned a run for a different task or attempt")
        if run_id is not None and run.run_id != run_id:
            raise ValueError("Agent changed the remote run identity")
        previous = self.snapshots.get(task.attempt_id)
        if previous:
            if previous.run_id != run.run_id:
                raise ValueError("Agent idempotency mapping changed")
            if run.revision < previous.revision:
                return previous
            if run.revision == previous.revision and run.model_dump(
                exclude={"observed_at"}
            ) != previous.model_dump(exclude={"observed_at"}):
                raise ValueError("Agent returned conflicting snapshots at the same revision")
            if previous.status in TERMINAL_STATES and run.result != previous.result:
                raise ValueError("Agent changed a terminal result")
        self.snapshots[task.attempt_id] = run
        return run

    async def by_key(self, task: AgentTask):
        _, body = await self._request(
            "GET", f"agent/v1/runs/by-key/{quote(task.idempotency_key, safe='')}"
        )
        return self._snapshot(body, task)

    async def submit(self, task: AgentTask, stopping):
        try:
            while True:
                try:
                    _, body = await self._request(
                        "POST", "agent/v1/runs", json=task.model_dump(mode="json")
                    )
                    break
                except AgentHttpError as error:
                    if error.status != 429 or error.code != "capacity_exceeded":
                        raise
                    if (task.deadline - datetime.now(UTC)).total_seconds() <= error.retry_after:
                        raise
                    end = asyncio.get_running_loop().time() + error.retry_after
                    while asyncio.get_running_loop().time() < end:
                        # A cancelled task waiting for capacity must not submit
                        # new work after that capacity becomes available.
                        if stopping():
                            raise
                        await asyncio.sleep(min(0.5, end - asyncio.get_running_loop().time()))
                    if stopping() or datetime.now(UTC) >= task.deadline:
                        raise
            return self._snapshot(body, task)
        except (httpx.HTTPError, AgentHttpError, TimeoutError) as error:
            # A lost submission response is not evidence that execution failed.
            # Only transport/server errors warrant checking the original identity.
            if isinstance(error, AgentHttpError) and error.status < 500:
                raise
            try:
                return await self.by_key(task)
            except AgentHttpError as lookup:
                if lookup.status != 404:
                    raise
                if stopping():
                    raise AgentHttpError(
                        422, "submission_cancelled", "cancelled before submission replay"
                    ) from lookup
                if task.deadline <= datetime.now(UTC):
                    raise AgentHttpError(
                        422, "deadline_expired", "deadline elapsed before submission replay"
                    ) from lookup
                # The v1 protocol guarantees durable idempotency. Reuse every
                # original field; do not issue a new business attempt or deadline.
                _, body = await self._request(
                    "POST", "agent/v1/runs", json=task.model_dump(mode="json")
                )
                return self._snapshot(body, task)

    async def get(self, run_id: UUID, task: AgentTask):
        _, body = await self._request("GET", f"agent/v1/runs/{run_id}")
        return self._snapshot(body, task, run_id)

    async def cancel(self, run_id: UUID, task: AgentTask):
        _, body = await self._request("POST", f"agent/v1/runs/{run_id}/cancel")
        result = AgentCancelResult.model_validate_json(body)
        run = self._snapshot(result.run.model_dump_json(), task, run_id)
        return run

    async def events(self, run_id: UUID, cursor: str | None):
        params = {"limit": 200}
        if cursor is not None:
            params["cursor"] = cursor
        while True:
            try:
                _, body = await self._request(
                    "GET", f"agent/v1/runs/{run_id}/events", params=params
                )
                break
            except AgentResponseTooLarge:
                # A page of individually valid events can exceed the response
                # budget. Keep the cursor fixed while requesting a smaller page.
                if params["limit"] == 1:
                    raise
                params["limit"] = max(1, params["limit"] // 2)
        page = AgentEventPage.model_validate_json(body)
        if page.run_id != run_id:
            raise ValueError("Agent event page belongs to a different run")
        if page.next_cursor is None and page.evidence.status == "collecting":
            raise ValueError("Agent ended pagination while evidence is still collecting")
        return page

    async def artifact(self, run_id: UUID, artifact: AgentArtifact, max_bytes: int):
        if artifact.size_bytes > max_bytes:
            raise ValueError("Agent artifact exceeds the configured byte limit")
        _, body = await self._request(
            "GET",
            f"agent/v1/runs/{run_id}/artifacts/{artifact.artifact_id}",
            max_bytes=max_bytes,
        )
        if len(body) != artifact.size_bytes or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ValueError("Agent artifact does not match its published size and digest")
        return body

    def release(self, attempt_id: UUID):
        self.snapshots.pop(attempt_id, None)

    async def close(self):
        await self.http.aclose()
