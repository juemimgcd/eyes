import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from eyes.agent_protocol.schema import input_validator
from eyes.agent_service.runtime import Runtime
from eyes.agent_service.storage import (
    Event,
    Run,
    ServiceSchema,
    database,
    lock_key,
    snapshot,
    transition,
)
from eyes.contracts.agent_protocol import (
    TERMINAL_STATES,
    AgentCancelResult,
    AgentEventPage,
    AgentEvidence,
    AgentResult,
    AgentTask,
)
from eyes.runner.files import json_bytes


class Problem(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def create_app(config):
    engine, sessions = database(config)
    runtime = Runtime(config, engine, sessions)
    validator = input_validator(config.input_schema)
    tokens = {}
    for principal, variable in config.clients.items():
        token = os.environ[variable]
        if not token or hashlib.sha256(token.encode()).hexdigest() in tokens:
            raise ValueError("Agent caller tokens must be nonempty and distinct")
        tokens[hashlib.sha256(token.encode()).hexdigest()] = principal
    for variable in config.secret_env_allowlist:
        if variable not in os.environ:
            raise ValueError(f"missing Agent environment variable: {variable}")

    @asynccontextmanager
    async def lifespan(app):
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sessions() as session:
            revision = session.get(ServiceSchema, 1)
            if revision is None or revision.revision != 1:
                raise RuntimeError("initialize the Agent service database with eyes-agent init-db")
        try:
            await runtime.start()
            yield
        finally:
            await runtime.close()
            engine.dispose()

    app = FastAPI(title="Eyes Agent protocol service", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def caller(auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if auth is None:
            raise Problem(401, "unauthenticated", "Bearer credential required")
        principal = tokens.get(hashlib.sha256(auth.credentials.encode()).hexdigest())
        if principal is None:
            raise Problem(401, "unauthenticated", "invalid caller credential")
        return principal

    Caller = Annotated[str, Depends(caller)]

    def own_run(session, principal, run_id, *, lock=False):
        query = select(Run).where(
            Run.service_id == config.service_id, Run.principal == principal, Run.id == run_id
        )
        if lock:
            query = query.with_for_update()
        run = session.scalar(query)
        if run is None:
            raise Problem(404, "not_found", "run not found")
        if run.expired:
            raise Problem(410, "record_expired", "run content has expired")
        return run

    @app.exception_handler(Problem)
    async def problem(request, error):
        return JSONResponse(
            status_code=error.status,
            headers={"WWW-Authenticate": "Bearer"}
            if error.status == 401
            else ({"Retry-After": "1"} if error.status == 429 else None),
            content={
                "protocol_version": "1.0",
                "error": {"code": error.code, "message": error.message},
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        # Keep the original traceback server-side; the wire response cannot leak
        # SQL parameters, credentials or execution input.
        import logging

        logging.getLogger(__name__).error("Agent database operation failed", exc_info=error)
        return JSONResponse(
            status_code=503,
            content={
                "protocol_version": "1.0",
                "error": {
                    "code": "temporarily_unavailable",
                    "message": "Agent state store is unavailable",
                },
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request, error):
        fields = [".".join(str(part) for part in item["loc"]) for item in error.errors()[:5]]
        return await problem(
            request,
            Problem(400, "invalid_request", "invalid request parameters: " + ", ".join(fields)),
        )

    @app.get("/health/ready")
    def ready():
        if not runtime.ready:
            raise Problem(503, "temporarily_unavailable", "Agent supervisor is unavailable")
        return {"status": "ready"}

    @app.get("/agent/v1/capabilities")
    def capabilities(principal: Caller):
        return config.capabilities()

    @app.put("/agent/v1/resources/{resource_id}")
    def unsupported_resources(resource_id: UUID, principal: Caller):
        raise Problem(422, "unsupported_capability", "this service does not accept input resources")

    @app.post("/agent/v1/runs")
    async def submit(request: Request, principal: Caller):
        if not runtime.ready:
            raise Problem(503, "temporarily_unavailable", "Agent supervisor is unavailable")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > config.max_request_bytes:
                raise Problem(413, "payload_too_large", "request exceeds configured byte limit")
        try:
            task = AgentTask.model_validate_json(data)
        except ValidationError as exc:
            raise Problem(400, "invalid_request", "request does not match AgentTask v1") from exc
        task.deadline = task.deadline.astimezone(UTC)
        value = task.model_dump(mode="json")
        digest = hashlib.sha256(json_bytes(value)).hexdigest()

        def accept():
            with sessions.begin() as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": lock_key(config.service_id, "admission")},
                )
                prior = session.scalar(
                    select(Run).where(
                        Run.service_id == config.service_id,
                        Run.principal == principal,
                        Run.idempotency_key == task.idempotency_key,
                    )
                )
                if prior is not None:
                    if prior.expired:
                        raise Problem(410, "record_expired", "run content has expired")
                    if prior.digest != digest:
                        raise Problem(
                            409, "idempotency_conflict", "key already identifies another request"
                        )
                    return 200, snapshot(prior)
                if (
                    session.scalar(
                        select(Run.id).where(
                            Run.service_id == config.service_id,
                            Run.principal == principal,
                            Run.attempt_id == task.attempt_id,
                        )
                    )
                    is not None
                ):
                    raise Problem(
                        409, "attempt_conflict", "attempt already has an execution identity"
                    )
                if task.deadline <= datetime.now(UTC):
                    raise Problem(422, "deadline_expired", "deadline has elapsed")
                if task.resources:
                    raise Problem(
                        422,
                        "unsupported_capability",
                        "this service does not accept input resources",
                    )
                if not validator.is_valid(task.input):
                    raise Problem(422, "invalid_input", "input does not match the Agent schema")
                active = session.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.service_id == config.service_id,
                        Run.status.not_in(TERMINAL_STATES),
                        (Run.result.is_(None))
                        | (Run.result["stopped_confirmed"].as_boolean().is_not(True)),
                    )
                )
                if active >= config.max_concurrency:
                    raise Problem(429, "capacity_exceeded", "Agent execution capacity is occupied")
                run = Run(
                    id=uuid4(),
                    service_id=config.service_id,
                    principal=principal,
                    attempt_id=task.attempt_id,
                    task_id=task.task_id,
                    idempotency_key=task.idempotency_key,
                    digest=digest,
                    task=value,
                    status="accepted",
                    accepted_at=datetime.now(UTC),
                    deadline=task.deadline,
                )
                session.add(run)
                session.flush()
                return 202, snapshot(run)

        import asyncio

        status, run = await asyncio.to_thread(accept)
        return JSONResponse(status_code=status, content=run.model_dump(mode="json"))

    @app.get("/agent/v1/runs/by-key/{key}")
    def by_key(key: str, principal: Caller):
        with sessions() as session:
            run_id = session.scalar(
                select(Run.id).where(
                    Run.service_id == config.service_id,
                    Run.principal == principal,
                    Run.idempotency_key == key,
                )
            )
            return snapshot(own_run(session, principal, run_id))

    @app.get("/agent/v1/runs/{run_id}")
    def get_run(run_id: UUID, principal: Caller):
        with sessions() as session:
            return snapshot(own_run(session, principal, run_id))

    @app.get("/agent/v1/runs/{run_id}/result")
    def get_result(run_id: UUID, principal: Caller):
        with sessions() as session:
            run = snapshot(own_run(session, principal, run_id))
            return JSONResponse(
                status_code=200 if run.result else 202,
                content=(run.result or run).model_dump(mode="json"),
            )

    @app.post("/agent/v1/runs/{run_id}/cancel")
    def cancel(run_id: UUID, principal: Caller):
        with sessions.begin() as session:
            run = own_run(session, principal, run_id, lock=True)
            if not config.capabilities().cancellation:
                raise Problem(
                    422, "unsupported_capability", "confirmed cancellation is not supported"
                )
            accepted = run.status in {"accepted", "running", "cancel_requested"}
            if run.status == "accepted":
                result = AgentResult(
                    protocol_version="1.0",
                    run_id=run.id,
                    status="cancelled",
                    output=None,
                    error=None,
                    stopped_confirmed=True,
                    completed_at=datetime.now(UTC),
                    cleanup_status="succeeded",
                    evidence=AgentEvidence(status="sealed", dropped_events=0),
                )
                transition(run, "cancelled", result, "cancelled before execution intent")
            elif run.status == "running":
                run.cancel_requested = True
                transition(run, "cancel_requested", reason="caller requested cancellation")
            value = snapshot(run)
            result = AgentCancelResult(
                protocol_version="1.0",
                run=value,
                accepted=accepted,
                stopped_confirmed=bool(value.result and value.result.stopped_confirmed),
            )
        return JSONResponse(
            status_code=200 if value.result else 202, content=result.model_dump(mode="json")
        )

    @app.get("/agent/v1/runs/{run_id}/events")
    def events(
        run_id: UUID,
        principal: Caller,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
    ):
        with sessions() as session:
            run = own_run(session, principal, run_id)
            items = list(
                session.scalars(
                    select(Event)
                    .where(Event.run_id == run_id, Event.sequence >= cursor)
                    .order_by(Event.sequence)
                    .limit(limit)
                )
            )
            next_index = items[-1].sequence + 1 if items else cursor
            done = run.result is not None and next_index >= run.event_count
            evidence = (
                AgentEvidence.model_validate(run.result["evidence"])
                if run.result
                else AgentEvidence(status="collecting", dropped_events=None)
            )
            return AgentEventPage(
                protocol_version="1.0",
                run_id=run_id,
                items=[item.content for item in items],
                next_cursor=None if done else str(next_index),
                evidence=evidence,
            )

    @app.get("/agent/v1/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: UUID, artifact_id: UUID, principal: Caller):
        with sessions() as session:
            run = own_run(session, principal, run_id)
            artifacts = (run.result or {}).get("artifacts", [])
            item = next(
                (item for item in artifacts if item["artifact_id"] == str(artifact_id)), None
            )
            if item is None:
                raise Problem(404, "not_found", "artifact not found in this run")
            try:
                stream = (
                    config.state_dir / "runs" / str(run_id) / "artifacts" / str(artifact_id)
                ).open("rb")
            except OSError as exc:
                raise Problem(404, "not_found", "artifact content is unavailable") from exc
        return StreamingResponse(
            iter(lambda: stream.read(64 * 1024), b""),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(stream.close),
        )

    return app
