"""Standard Agent execution in the Runner's async network path."""

import asyncio
import hashlib
import json
import logging
import traceback
from datetime import UTC, datetime

import httpx

from eyes.agent_protocol.client import AgentClient, AgentHttpError
from eyes.agent_protocol.target import validate_target
from eyes.contracts.agent_protocol import AgentTask
from eyes.contracts.evidence import ExecutionEvent
from eyes.contracts.target import ExecutionInput, ExecutionResult
from eyes.runner.client import ApiError
from eyes.runner.files import error_text, redact, resolve_secrets, write_json

logger = logging.getLogger(__name__)


async def recover_evidence(service, work, request, config, client, run, directory, secrets):
    cursor_file = directory / "protocol-events.json"
    progress = (
        json.loads(cursor_file.read_bytes())
        if cursor_file.exists()
        else {
            "cursor": None,
            "finished": not config.capabilities.events,
        }
    )
    end = asyncio.get_running_loop().time() + service.config.evidence_wait_seconds
    while not progress["finished"] and asyncio.get_running_loop().time() < end:
        page = await client.events(run.run_id, progress["cursor"])
        queue_events(service, work, request, run, page, secrets)
        progress = {
            "cursor": page.next_cursor,
            "finished": page.next_cursor is None,
            "partial": progress.get("partial", False)
            or page.evidence.status in {"partial", "unavailable"}
            or (page.evidence.dropped_events is not None and page.evidence.dropped_events > 0),
        }
        write_json(cursor_file, progress)
        if not progress["finished"]:
            await service.outbox.flush(service.client, evidence_only=True)
    artifact_file = directory / "protocol-recovered-artifacts.json"
    downloaded = json.loads(artifact_file.read_bytes()) if artifact_file.exists() else []
    for artifact in run.result.artifacts[: service.config.max_artifacts]:
        if str(artifact.artifact_id) in downloaded:
            continue
        body = await client.artifact(run.run_id, artifact, service.config.max_artifact_bytes)
        service.outbox.enqueue(
            f"artifact-{request.attempt_id}-{artifact.artifact_id}",
            {
                "kind": "artifact",
                "work_id": work["work_item_id"],
                "attempt_id": str(request.attempt_id),
                "metadata": {
                    "schema_version": "1.0",
                    "name": artifact.name,
                    "media_type": artifact.media_type,
                    "size": artifact.size_bytes,
                    "sha256": artifact.sha256,
                },
            },
            body,
        )
        downloaded.append(str(artifact.artifact_id))
        write_json(artifact_file, downloaded)
    await service.outbox.flush(service.client, evidence_only=True)
    pending, rejected = service.outbox.evidence(work["work_item_id"])
    if not progress["finished"] or pending:
        return False
    if run.status != "unknown":
        state = run.result.evidence.status
        if (
            state == "collecting"
            or progress.get("partial")
            or rejected
            or len(downloaded) != len(run.result.artifacts)
        ):
            state = "partial"
        service.outbox.enqueue(
            f"recovery-evidence-close-{request.attempt_id}-{run.revision}",
            {
                "kind": "evidence_close",
                "work_id": work["work_item_id"],
                "path": f"v1/attempts/{request.attempt_id}/evidence-close",
                "journal_dir": str(directory),
                "body": {
                    "schema_version": "1.0",
                    "status": state,
                    "dropped_events": run.result.evidence.dropped_events,
                    "details": {
                        "source": "agent_protocol_recovery",
                        "rejected_uploads": rejected,
                        "artifact_count_limited": len(downloaded) != len(run.result.artifacts),
                    },
                },
            },
        )
        await service.outbox.flush(service.client, evidence_only=True)
    return True


async def recovery_candidates(service):
    cursor = None
    while True:
        path = f"v1/runners/{service.runner_id}/reservations"
        if cursor is not None:
            path += f"?after={cursor}"
        page = await service.client.request("GET", path)
        for item in page["items"]:
            yield item
        cursor = page["next_cursor"]
        if cursor is None:
            break
    for marker in service.work_dir.glob("*/reconciled.json"):
        saved = json.loads(marker.read_bytes())
        if not saved["evidence_complete"]:
            yield {
                "work_item_id": marker.parent.name,
                "plugin": "agent_protocol",
                "status": "unknown",
                "cancel_requested": False,
            }


async def reconcile_once(service):
    try:
        async for item in recovery_candidates(service):
            if item["plugin"] != "agent_protocol" or item["status"] != "unknown":
                continue
            directory = service.work_dir / item["work_item_id"]
            # Only a journaled assignment supplies the original request and
            # secret references. Missing local state remains visibly unresolved.
            if (
                not (directory / "assignment.json").exists()
                or not (directory / "intent.json").exists()
            ):
                continue
            work = json.loads((directory / "assignment.json").read_bytes())
            request = ExecutionInput.model_validate(work["payload"])
            config = validate_target(request.target)
            secrets = resolve_secrets(
                request.target.secret_refs,
                service.config.secret_env_allowlist,
                service.config.token_env,
            )
            key, client = await service.recovery_clients.acquire(
                config.base_url, secrets.get("token")
            )
            task = AgentTask(
                protocol_version="1.0",
                task_id=request.case_run_id,
                attempt_id=request.attempt_id,
                idempotency_key=request.idempotency_key,
                deadline=request.deadline,
                input=request.input,
                traceparent=request.traceparent,
            )
            try:
                # Recovery never submits work, even when lookup returns 404.
                run = await client.by_key(task)
                if (
                    run.result is None
                    and config.capabilities.cancellation
                    and (item["cancel_requested"] or datetime.now(UTC) >= task.deadline)
                ):
                    run = await client.cancel(run.run_id, task)
                write_json(
                    directory / "remote-observation.json",
                    redact(run.model_dump(mode="json"), secrets.values()),
                )
                if run.result is not None and run.result.stopped_confirmed:
                    response = await service.client.request(
                        "POST",
                        f"v1/work/{item['work_item_id']}/reconcile-agent",
                        json={
                            "schema_version": "1.0",
                            "runner_id": service.runner_id,
                            "run": redact(run.model_dump(mode="json"), secrets.values()),
                        },
                    )
                    write_json(
                        directory / "reconciled.json",
                        {
                            "status": response["status"],
                            "revision": run.revision,
                            "evidence_complete": False,
                        },
                    )
                    complete = await recover_evidence(
                        service, work, request, config, client, run, directory, secrets
                    )
                    write_json(
                        directory / "reconciled.json",
                        {
                            "status": response["status"],
                            "revision": run.revision,
                            "evidence_complete": complete,
                        },
                    )
            except (AgentHttpError, ApiError, httpx.HTTPError, ValueError, OSError) as error:
                logger.warning(
                    "protocol_reconciliation_pending work=%s\n%s",
                    item["work_item_id"],
                    redact(traceback.format_exc(), secrets.values()),
                )
                write_json(
                    directory / "reconciliation-error.json",
                    {"error": error_text(error, secrets.values())},
                )
            finally:
                service.recovery_clients.release(key, request.attempt_id)
    except ApiError, httpx.HTTPError, ValueError, OSError:
        logger.exception("protocol_recovery_inventory_unavailable")
        return False
    return True


async def reconcile_forever(service):
    while not service.stopping.is_set():
        await reconcile_once(service)
        try:
            await asyncio.wait_for(
                service.stopping.wait(), service.config.reconcile_interval_seconds
            )
        except TimeoutError:
            pass


def queue_events(service, work, request, run, page, secrets):
    for item in page.items:
        event = ExecutionEvent(
            event_id=item.event_id,
            producer_id=f"agent-protocol:{run.run_id}",
            attempt_id=request.attempt_id,
            trace_id=request.traceparent.split("-")[1],
            span_id=request.traceparent.split("-")[2],
            sequence=item.sequence,
            occurred_at=item.occurred_at,
            type="custom.agent.event",
            source="target_trace",
            data=redact({"protocol_type": item.type, "payload": item.data}, secrets.values()),
        )
        if len(event.model_dump_json().encode()) > service.config.max_event_bytes:
            raise ValueError("Agent event exceeds the configured capture byte limit")
        service.outbox.enqueue(
            f"event-{item.event_id}",
            {
                "kind": "events",
                "work_id": work["work_item_id"],
                "path": "v1/events",
                "body": {"schema_version": "1.0", "events": [event.model_dump(mode="json")]},
            },
        )


class AgentClients:
    def __init__(self, config):
        self.config = config
        self.clients = {}
        self.users = {}
        self.lock = asyncio.Lock()

    async def acquire(self, base_url, token):
        async with self.lock:
            return await self._acquire(base_url, token)

    async def _acquire(self, base_url, token):
        key = (base_url, hashlib.sha256((token or "").encode()).hexdigest())
        if key not in self.clients:
            # At most one cached pool per configured execution slot. Idle pools
            # are replaced as target versions change; active clients are shared.
            if len(self.clients) >= max(1, self.config.execution_slots):
                idle = next(k for k in self.clients if not self.users[k])
                await self.clients.pop(idle).close()
                del self.users[idle]
            self.clients[key] = AgentClient(
                base_url,
                token,
                allowed_origins=self.config.allowed_http_origins,
                connections=self.config.agent_connections_per_target,
                timeout=self.config.agent_timeout_seconds,
                pool_timeout=self.config.http_pool_timeout_seconds,
                max_response_bytes=self.config.max_message_bytes,
            )
            self.users[key] = 0
        self.users[key] += 1
        return key, self.clients[key]

    def release(self, key, attempt_id):
        self.clients[key].release(attempt_id)
        self.users[key] -= 1

    async def close(self):
        await asyncio.gather(*(client.close() for client in self.clients.values()))


async def execute(service, work, lease, directory):
    request = ExecutionInput.model_validate(work["payload"])
    config = validate_target(request.target)
    secrets = resolve_secrets(
        request.target.secret_refs, service.config.secret_env_allowlist, service.config.token_env
    )
    key, client = await service.agent_clients.acquire(config.base_url, secrets.get("token"))
    task = AgentTask(
        protocol_version="1.0",
        task_id=request.case_run_id,
        attempt_id=request.attempt_id,
        idempotency_key=request.idempotency_key,
        deadline=request.deadline,
        input=request.input,
        traceparent=request.traceparent,
    )
    cursor = None
    events_finished = not config.capabilities.events
    capture_errors = set()
    run = None
    intended = False

    async def collect_events():
        nonlocal cursor, events_finished
        page = await client.events(run.run_id, cursor)
        if page.evidence.status in {"partial", "unavailable"} or (
            page.evidence.dropped_events is not None and page.evidence.dropped_events > 0
        ):
            capture_errors.add("Agent event stream reports incomplete or unknown capture")
        queue_events(service, work, request, run, page, secrets)
        # Advance only after the complete page is durably queued. A failed page
        # may be replayed with identical event identifiers.
        cursor = page.next_cursor
        events_finished = cursor is None
        write_json(
            directory / "protocol-events.json",
            {"cursor": cursor, "finished": events_finished, "partial": bool(capture_errors)},
        )

    try:
        await service.progress(lease, "preparing")
        actual = await client.capabilities()
        if actual != config.capabilities:
            raise ValueError(
                "Agent capabilities changed; discover and publish a new target version"
            )
        write_json(directory / "protocol-task.json", task.model_dump(mode="json"))
        if lease.stop.is_set() or service.stopping.is_set() or datetime.now(UTC) >= task.deadline:
            result = ExecutionResult(
                status="timed_out" if datetime.now(UTC) >= task.deadline else "cancelled",
                stopped_confirmed=True,
                cleanup_status="succeeded",
                evidence_status="unavailable",
                dropped_events=0,
            )
        else:
            write_json(directory / "intent.json", {"intent": True})
            await service.progress(lease, "running")
            intended = True
            run = await client.submit(
                task, lambda: lease.stop.is_set() or service.stopping.is_set()
            )
            await service._operation(lease, directory, str(run.run_id))
            while run.result is None:
                if (
                    lease.stop.is_set()
                    or service.stopping.is_set()
                    or datetime.now(UTC) >= task.deadline
                ):
                    if config.capabilities.cancellation:
                        run = await client.cancel(run.run_id, task)
                    # Cancellation acceptance does not release capacity. Bound
                    # the wait; unresolved work becomes unknown below.
                    end = asyncio.get_running_loop().time() + service.config.stop_grace_seconds
                    while run.result is None and asyncio.get_running_loop().time() < end:
                        await asyncio.sleep(min(config.poll_seconds, 0.5))
                        run = await client.get(run.run_id, task)
                    break
                if not events_finished:
                    try:
                        await collect_events()
                    except (AgentHttpError, httpx.HTTPError, ValueError, OSError) as error:
                        capture_errors.add(error_text(error, secrets.values()))
                        # Keep collecting task status even if telemetry is broken.
                        events_finished = True
                await asyncio.sleep(config.poll_seconds)
                run = await client.get(run.run_id, task)
            if run.result is None:
                result = ExecutionResult(
                    status="unknown",
                    error="Agent stop has not been confirmed",
                    remote_operation_id=str(run.run_id),
                    cleanup_status="unknown",
                    evidence_status="partial",
                    dropped_events=None,
                )
            else:
                remote = run.result
                result = ExecutionResult(
                    status=remote.status,
                    output=redact(remote.output, secrets.values()),
                    error=redact(f"{remote.error.code}: {remote.error.message}", secrets.values())[
                        :2000
                    ]
                    if remote.error
                    else None,
                    remote_operation_id=str(remote.run_id),
                    stopped_confirmed=remote.stopped_confirmed,
                    cleanup_status=remote.cleanup_status,
                    cleanup_error="Agent reported cleanup failure"
                    if remote.cleanup_status == "failed"
                    else None,
                    evidence_status=remote.evidence.status
                    if remote.evidence.status != "collecting"
                    else "partial",
                    dropped_events=remote.evidence.dropped_events,
                    evidence_details={
                        "agent_evidence": redact(
                            remote.evidence.model_dump(mode="json"), secrets.values()
                        ),
                        "dropped_events_known": remote.evidence.dropped_events is not None,
                    },
                )
                if remote.evidence.dropped_events is None:
                    result.evidence_status = "partial"
                end = asyncio.get_running_loop().time() + service.config.evidence_wait_seconds
                while not events_finished and asyncio.get_running_loop().time() < end:
                    try:
                        await collect_events()
                    except (AgentHttpError, httpx.HTTPError, ValueError, OSError) as error:
                        capture_errors.add(error_text(error, secrets.values()))
                        break
                    if not events_finished:
                        await asyncio.sleep(config.poll_seconds)
                if len(remote.artifacts) > service.config.max_artifacts:
                    capture_errors.add("Agent artifact count exceeds the configured capture limit")
                for artifact in remote.artifacts[: service.config.max_artifacts]:
                    try:
                        body = await client.artifact(
                            run.run_id, artifact, service.config.max_artifact_bytes
                        )
                        service.outbox.enqueue(
                            f"artifact-{request.attempt_id}-{artifact.artifact_id}",
                            {
                                "kind": "artifact",
                                "work_id": work["work_item_id"],
                                "attempt_id": str(request.attempt_id),
                                "metadata": {
                                    "schema_version": "1.0",
                                    "name": artifact.name,
                                    "media_type": artifact.media_type,
                                    "size": artifact.size_bytes,
                                    "sha256": artifact.sha256,
                                },
                            },
                            body,
                        )
                    except (AgentHttpError, httpx.HTTPError, ValueError, OSError) as error:
                        capture_errors.add(error_text(error, secrets.values()))
        if capture_errors or not events_finished:
            result.evidence_status = "partial"
        result.evidence_details.update(
            {"capture_errors": sorted(capture_errors), "events_finished": events_finished}
        )
    except (AgentHttpError, httpx.HTTPError, ValueError, OSError) as error:
        logger.warning(
            "protocol_execution_failed attempt=%s\n%s",
            request.attempt_id,
            redact(traceback.format_exc(), secrets.values()),
        )
        rejected = (
            run is None
            and isinstance(error, AgentHttpError)
            and (
                (error.status == 429 and error.code == "capacity_exceeded")
                or (
                    error.status == 422
                    and error.code
                    in {
                        "deadline_expired",
                        "invalid_input",
                        "unsupported_capability",
                        "submission_cancelled",
                    }
                )
            )
        )
        status = "unknown" if intended and not rejected else "failed"
        if rejected:
            status = {
                "submission_cancelled": "cancelled",
                "deadline_expired": "timed_out",
            }.get(error.code, "failed")
        result = ExecutionResult(
            status=status,
            error=error_text(error, secrets.values()),
            remote_operation_id=str(run.run_id) if run else None,
            stopped_confirmed=rejected,
            cleanup_status="succeeded" if rejected else "unknown",
            evidence_status="partial",
            dropped_events=None if intended and not rejected else 0,
        )
    finally:
        service.agent_clients.release(key, request.attempt_id)
    result.evidence_details["producer_evidence_status"] = result.evidence_status
    pending, rejected = service.outbox.evidence(work["work_item_id"])
    result.evidence_details.update({"pending_uploads": pending, "rejected_uploads": rejected})
    if pending or rejected:
        result.evidence_status = "partial"
    if result.status == "unknown" or result.cleanup_status != "succeeded":
        write_json(
            directory / "quarantine.json",
            {"reason": "Agent execution or cleanup requires reconciliation"},
        )
    await service.complete(directory, work, lease, result)
