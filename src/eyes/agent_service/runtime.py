import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text

from eyes.agent_service.storage import Event, Run, ServiceInstance, lock_key, transition
from eyes.contracts.agent_protocol import AgentEvent, AgentEvidence, AgentProblem, AgentResult
from eyes.runner.files import write_json

logger = logging.getLogger(__name__)


def unknown(run_id, reason):
    return AgentResult(
        protocol_version="1.0",
        run_id=run_id,
        status="unknown",
        output=None,
        error=AgentProblem(code="execution_unknown", message=reason),
        stopped_confirmed=False,
        completed_at=None,
        cleanup_status="unknown",
        evidence=AgentEvidence(status="partial", dropped_events=None),
    )


class Runtime:
    def __init__(self, config, engine, sessions):
        self.config, self.engine, self.sessions = config, engine, sessions
        self.stopping = asyncio.Event()
        self.active = {}
        self.ready = False
        self.owner = None
        self.supervisor = None
        self.generation = uuid4()

    def recover(self):
        self.owner = self.engine.connect()
        held = self.owner.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": lock_key(self.config.service_id, "owner")},
        )
        self.owner.commit()
        if not held:
            self.owner.close()
            raise RuntimeError("another supervisor owns this Agent service_id")
        with self.sessions.begin() as session:
            instance = session.get(ServiceInstance, self.config.service_id, with_for_update=True)
            if instance is None:
                session.add(
                    ServiceInstance(service_id=self.config.service_id, generation=self.generation)
                )
            else:
                instance.generation = self.generation
            for run in session.scalars(
                select(Run)
                .where(
                    Run.service_id == self.config.service_id,
                    Run.status.in_(["running", "cancel_requested"]),
                )
                .with_for_update()
            ):
                transition(
                    run,
                    "unknown",
                    unknown(run.id, "service restarted during execution"),
                    "startup recovery; execution is not replayed",
                )
        self.ready = True

    def fence(self, session):
        # A broken ownership connection releases its advisory lock. A replacement
        # supervisor must fence late writes from this process's surviving workers.
        instance = session.scalar(
            select(ServiceInstance)
            .where(
                ServiceInstance.service_id == self.config.service_id,
            )
            .with_for_update(read=True)
        )
        if instance.generation != self.generation:
            raise RuntimeError("Agent supervisor generation has been replaced")

    async def start(self):
        await asyncio.to_thread(self.recover)
        self.supervisor = asyncio.create_task(self.loop())

    def claim(self):
        # The owner connection is never reconnected after an error. Losing it
        # makes this supervisor unavailable until restart and recovery.
        self.owner.execute(text("SELECT 1"))
        self.owner.commit()
        with self.sessions.begin() as session:
            self.fence(session)
            runs = session.scalars(
                select(Run)
                .where(
                    Run.service_id == self.config.service_id,
                    Run.status == "accepted",
                )
                .order_by(Run.accepted_at, Run.id)
                .limit(self.config.max_concurrency - len(self.active))
                .with_for_update(skip_locked=True)
            ).all()
            for run in runs:
                transition(run, "running", reason="durable execution intent")
            return [(run.id, run.task) for run in runs]

    async def loop(self):
        try:
            while not self.stopping.is_set():
                finished = [key for key, task in self.active.items() if task.done()]
                for key in finished:
                    self.active.pop(key).result()
                for run_id, task in await asyncio.to_thread(self.claim):
                    self.active[run_id] = asyncio.create_task(self.execute(run_id, task))
                try:
                    await asyncio.wait_for(self.stopping.wait(), self.config.poll_seconds)
                except TimeoutError:
                    pass
        except Exception:
            logger.exception("Agent supervisor stopped; restart required")
            self.ready = False
            self.stopping.set()
        finally:
            await asyncio.gather(*self.active.values(), return_exceptions=True)

    def observe(self, run_id):
        directory = self.config.state_dir / "runs" / str(run_id)
        with self.sessions.begin() as session:
            self.fence(session)
            run = session.get(Run, run_id, with_for_update=True)
            # Producer writes contiguous sequence files atomically. The database
            # commit, not directory listing order, defines visible progress.
            while (path := directory / "events" / f"{run.event_count:010d}.json").exists():
                event = AgentEvent.model_validate_json(path.read_bytes())
                if event.run_id != run_id or event.sequence != run.event_count:
                    raise ValueError("Agent event journal identity or sequence mismatch")
                session.add(
                    Event(
                        id=event.event_id,
                        run_id=run_id,
                        sequence=event.sequence,
                        content=event.model_dump(mode="json"),
                    )
                )
                run.event_count += 1
            return run.cancel_requested

    async def stop_group(self, process):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                await asyncio.wait_for(process.wait(), self.config.stop_grace_seconds)
            except TimeoutError:
                pass
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def finish(self, run_id, result):
        with self.sessions.begin() as session:
            self.fence(session)
            run = session.get(Run, run_id, with_for_update=True)
            transition(run, result.status, result, "worker observation")

    async def execute(self, run_id, task):
        directory = self.config.state_dir / "runs" / str(run_id)
        process = None
        deadline = datetime.fromisoformat(task["deadline"])
        reason = "failed"
        try:
            (directory / "workspace").mkdir(parents=True, mode=0o700)
            write_json(
                directory / "job.json",
                {
                    "run_id": str(run_id),
                    "task": task,
                    "config": self.config.model_dump(mode="json"),
                },
            )
            cancelled = await asyncio.to_thread(self.observe, run_id)
            if cancelled or self.stopping.is_set() or datetime.now(UTC) >= deadline:
                reason = "timed_out" if datetime.now(UTC) >= deadline else "cancelled"
                result = AgentResult(
                    protocol_version="1.0",
                    run_id=run_id,
                    status=reason,
                    output=None,
                    error=None,
                    stopped_confirmed=True,
                    completed_at=datetime.now(UTC),
                    cleanup_status="succeeded",
                    evidence=AgentEvidence(status="sealed", dropped_events=0),
                )
            else:
                environment = {
                    name: os.environ[name]
                    for name in ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"]
                    if name in os.environ
                }
                environment.update(
                    {name: os.environ[name] for name in self.config.secret_env_allowlist}
                )
                process = await asyncio.create_subprocess_exec(
                    self.config.python,
                    "-m",
                    "eyes.agent_service.worker",
                    str(directory),
                    env=environment,
                    start_new_session=True,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                while process.returncode is None:
                    cancelled = await asyncio.to_thread(self.observe, run_id)
                    if cancelled or self.stopping.is_set() or datetime.now(UTC) >= deadline:
                        reason = "timed_out" if datetime.now(UTC) >= deadline else "cancelled"
                        break
                    try:
                        await asyncio.wait_for(process.wait(), self.config.poll_seconds)
                    except TimeoutError:
                        pass
                stopped = await self.stop_group(process)
                await asyncio.to_thread(self.observe, run_id)
                outcome_path = directory / "outcome.json"
                if outcome_path.exists() and stopped:
                    # A real returned outcome wins a concurrent cancel request.
                    with outcome_path.open("rb") as stream:
                        body = stream.read(self.config.max_result_bytes + 1)
                    if len(body) > self.config.max_result_bytes:
                        raise ValueError("worker outcome exceeds configured byte limit")
                    outcome = json.loads(body)
                    status = (
                        "succeeded"
                        if outcome["error"] is None
                        else ("failed" if self.config.execution_scope == "process" else "unknown")
                    )
                    result = AgentResult(
                        protocol_version="1.0",
                        run_id=run_id,
                        status=status,
                        output=outcome["output"],
                        error=AgentProblem(code="agent_error", message=outcome["error"])
                        if outcome["error"]
                        else None,
                        stopped_confirmed=status != "unknown",
                        completed_at=datetime.now(UTC) if status != "unknown" else None,
                        cleanup_status="succeeded" if status != "unknown" else "unknown",
                        evidence=AgentEvidence(
                            status="sealed" if outcome["dropped_events"] == 0 else "partial",
                            dropped_events=outcome["dropped_events"],
                            details={"capture_error": outcome["capture_error"]},
                        ),
                        artifacts=outcome["artifacts"],
                    )
                elif stopped and self.config.execution_scope == "process":
                    result = AgentResult(
                        protocol_version="1.0",
                        run_id=run_id,
                        status=reason,
                        output=None,
                        error=AgentProblem(
                            code="worker_exited", message="worker exited without an outcome"
                        )
                        if reason == "failed"
                        else None,
                        stopped_confirmed=True,
                        completed_at=datetime.now(UTC),
                        cleanup_status="succeeded",
                        evidence=AgentEvidence(status="partial", dropped_events=None),
                    )
                else:
                    result = unknown(run_id, "worker stopped without confirmed external outcome")
        except Exception:
            logger.exception("Agent execution boundary failed run=%s", run_id)
            result = unknown(run_id, "execution boundary failed; inspect service diagnostics")
            if process is None:
                result = AgentResult(
                    protocol_version="1.0",
                    run_id=run_id,
                    status="failed",
                    output=None,
                    error=AgentProblem(
                        code="preparation_failed",
                        message="worker could not start; inspect service diagnostics",
                    ),
                    stopped_confirmed=True,
                    completed_at=datetime.now(UTC),
                    cleanup_status="unknown",
                    evidence=AgentEvidence(status="unavailable", dropped_events=None),
                )
        finally:
            if process is not None:
                await self.stop_group(process)
        await asyncio.to_thread(self.finish, run_id, result)

    async def close(self):
        self.ready = False
        self.stopping.set()
        if self.supervisor:
            await self.supervisor
        if self.owner is not None and not self.owner.closed:
            await asyncio.to_thread(self.owner.close)
