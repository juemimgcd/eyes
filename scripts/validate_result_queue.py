"""Validate final Python results through real HTTP, RabbitMQ and PostgreSQL."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from validate_storage import docker, seed, start_api, wait_api

from eyes.contracts.work import ExecutionSubmission
from eyes.runner.client import ControlClient
from eyes.runner.config import RunnerConfig
from eyes.runner.outbox import Outbox
from eyes.runner.service import RunnerService
from eyes.server.config import Settings
from eyes.server.event_queue.broker import connect, declare_queue
from eyes.server.evidence.service import LocalArtifactStore
from eyes.server.result_queue.service import consume
from eyes.server.result_queue.worker import receive_one
from eyes.server.scheduling.service import sweep
from eyes.server.storage.database import database
from eyes.server.storage.models import (
    Attempt,
    CaseRun,
    Experiment,
    Manifest,
    ResultSubmission,
    WorkItem,
    now,
)


def setup_work(sessions, *, failed=False):
    fixture = seed(sessions, 1)
    # This is a real Python subprocess. Fixture rows only supply scheduling context.
    program = (
        "raise ValueError('expected validation failure')"
        if failed
        else "import json; print(json.dumps({'sum': sum(range(1, 101))}))"
    )
    executed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    result = {
        "schema_version": "1.0",
        "status": "failed" if executed.returncode else "succeeded",
        "output": None if executed.returncode else json.loads(executed.stdout),
        "error": executed.stderr[-2000:] if executed.returncode else None,
        "cleanup_status": "succeeded",
        "evidence_status": "sealed",
        "dropped_events": 0,
    }
    with sessions.begin() as session:
        source_attempt = session.get(Attempt, UUID(fixture["attempt_id"]))
        source_run = session.get(CaseRun, source_attempt.case_run_id)
        source = session.get(Experiment, source_run.experiment_id)
        # Published snapshots are immutable; create a fresh execution context.
        experiment = Experiment(
            project_id=source.project_id,
            target_id=source.target_id,
            target_scope_id=source.target_scope_id,
            dataset_id=source.dataset_id,
            request_key=str(uuid4()),
            request_digest="0" * 64,
            snapshot={"scorers": [], "request": {"concurrency": 1}},
            status="running",
        )
        session.add(experiment)
        session.flush()
        run = CaseRun(
            experiment_id=experiment.id,
            case_version_id=source_run.case_version_id,
            repetition=0,
            status="running",
        )
        session.add(run)
        session.flush()
        attempt = Attempt(
            case_run_id=run.id,
            project_id=source.project_id,
            runner_id=source_attempt.runner_id,
            trace_id=uuid4().hex,
            root_span_id=uuid4().hex[:16],
            idempotency_key=str(uuid4()),
            deadline=now() + timedelta(minutes=10),
            status="running",
            execution_intent_at=now(),
        )
        session.add(attempt)
        session.flush()
        fixture["attempt_id"] = str(attempt.id)
        work = WorkItem(
            project_id=experiment.project_id,
            experiment_id=experiment.id,
            target_id=experiment.target_id,
            target_scope_id=experiment.target_scope_id,
            case_run_id=run.id,
            attempt_id=attempt.id,
            kind="execute",
            plugin="python",
            status="claimed",
            runner_id=attempt.runner_id,
            lease_token=uuid4(),
            lease_expires_at=now() + timedelta(minutes=10),
            reserved=True,
        )
        session.add(work)
        session.flush()
        fixture["work_id"] = str(work.id)
        fixture["submission"] = {
            "schema_version": "1.0",
            "runner_id": str(attempt.runner_id),
            "lease_token": str(work.lease_token),
            "result": result,
        }
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, artifact_root=args.output / "artifacts")
    engine, sessions = database(settings)
    store = LocalArtifactStore(settings.artifact_root)
    report = {}
    process = start_api(settings, 58082, args.output, True)
    consumers = []
    with connect(settings) as conn:
        declare_queue(
            conn.channel(), settings.result_queue, settings.event_queue_max_bytes, reliable=True
        )
    try:
        with httpx.Client(base_url="http://127.0.0.1:58082", timeout=15) as api:

            def publish(fixture):
                response = api.post(
                    f"/v1/work/{fixture['work_id']}/result-submissions",
                    json=fixture["submission"],
                    headers={"Authorization": f"Bearer {fixture['runner_token']}"},
                )
                response.raise_for_status()
                return response.json()

            normal = setup_work(sessions)
            receipt = publish(normal)
            duplicate = publish(normal)
            assert receipt["id"] == duplicate["id"]
            with sessions() as session:
                assert session.get(Attempt, UUID(normal["attempt_id"])).result is None
            # Commit without ACK, then reconnect and handle the actual re-delivery.
            with connect(settings) as conn:
                channel = conn.channel()
                method, properties, body = channel.basic_get(settings.result_queue, auto_ack=False)
                assert method
                consume(
                    sessions,
                    UUID(properties.message_id),
                    ExecutionSubmission.model_validate_json(body),
                    store,
                    settings,
                )
            with connect(settings) as conn:
                while receive_one(conn.channel(), sessions, settings, store):
                    pass
            with sessions() as session:
                work = session.get(WorkItem, UUID(normal["work_id"]))
                attempt = session.get(Attempt, UUID(normal["attempt_id"]))
                assert work.status == "completed" and not work.reserved
                assert attempt.result["output"] == {"sum": 5050}
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Manifest)
                        .where(Manifest.attempt_id == attempt.id)
                    )
                    == 1
                )
            report["python_output_committed_and_duplicate_safe"] = {
                "sum": 5050,
                "manifest_added": 1,
            }
            failed = setup_work(sessions, failed=True)
            receipt = publish(failed)
            with connect(settings) as conn:
                assert receive_one(conn.channel(), sessions, settings, store)
            with sessions() as session:
                assert session.get(ResultSubmission, UUID(receipt["id"])).status == "persisted"
                assert session.get(Attempt, UUID(failed["attempt_id"])).status == "failed"
            report["python_failure_is_a_normal_result"] = True
            # The shipping Runner producer retains its file through an unavailable broker.
            recovery = setup_work(sessions)

            async def runner_delivery():
                folder = args.output / "runner"
                folder.mkdir()
                box = Outbox(folder / "outbox", 1024 * 1024, "final-result-validation")
                runner = SimpleNamespace(config=RunnerConfig(result_delivery="queued"), outbox=box)
                RunnerService.queue_completion(
                    runner,
                    folder,
                    {"kind": "execute", "work_item_id": recovery["work_id"]},
                    recovery["submission"],
                )
                client = ControlClient(
                    "http://127.0.0.1:58082", recovery["runner_token"], 5, 8 * 1024 * 1024
                )
                try:
                    docker("stop", "eyes-mq-validation-rabbit")
                    try:
                        await box.flush(client, completions_only=True)
                        assert box.status()["pending"] == 1 and not (folder / "ack.json").exists()
                    finally:
                        docker("start", "eyes-mq-validation-rabbit")
                    deadline = time.monotonic() + 30
                    while True:
                        await box.flush(client, completions_only=True)
                        job = json.loads(next(box.pending.glob("*.json")).read_text())
                        if "submission_id" in job:
                            break
                        if time.monotonic() > deadline:
                            raise TimeoutError("result producer did not recover")
                        await asyncio.sleep(0.3)
                    assert not (folder / "ack.json").exists()
                    docker("stop", "eyes-mq-validation-pg")
                    try:
                        try:
                            with connect(settings) as conn:
                                receive_one(conn.channel(), sessions, settings, store)
                        except SQLAlchemyError:
                            pass
                        else:
                            raise AssertionError("consumer should fail while PostgreSQL is stopped")
                    finally:
                        docker("start", "eyes-mq-validation-pg")
                    wait_api("http://127.0.0.1:58082", process)
                    with connect(settings) as conn:
                        assert receive_one(conn.channel(), sessions, settings, store)
                    await box.flush(client, completions_only=True)
                    assert box.status()["pending"] == 0 and (folder / "ack.json").exists()
                finally:
                    await client.client.aclose()
                    await client.transfer_client.aclose()

            asyncio.run(runner_delivery())
            report["runner_broker_failure_and_database_failure_recovery"] = True
            # A stale completion must be retained for diagnosis rather than override
            # scheduling decisions already made after lease expiry.
            stale = setup_work(sessions)
            receipt = publish(stale)
            with sessions.begin() as session:
                session.get(WorkItem, UUID(stale["work_id"])).lease_expires_at = now() - timedelta(
                    seconds=1
                )
            with sessions.begin() as session:
                sweep(session, settings)
            with connect(settings) as conn:
                channel = conn.channel()
                assert receive_one(channel, sessions, settings, store)
                deadline = time.monotonic() + 5
                while True:
                    method, properties, wire = channel.basic_get(
                        f"{settings.result_queue}.rejected", auto_ack=False
                    )
                    if method:
                        assert json.loads(wire)["result"]["output"] == {"sum": 5050}
                        # Retain the dead-letter for inspection after this test.
                        channel.basic_nack(method.delivery_tag, requeue=True)
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("result missing from dead-letter queue")
                    conn.sleep(0.1)
            with sessions() as session:
                row = session.get(ResultSubmission, UUID(receipt["id"]))
                assert row.status == "rejected" and row.error["code"] == "stale_lease"
            report["stale_result_preserved_in_dead_letter"] = {
                "error": "stale_lease",
                "output": {"sum": 5050},
            }
            # Finally check the installed consumer as an independent process.
            live = setup_work(sessions)
            receipt = publish(live)
            log = (args.output / "consumer.log").open("w")
            consumer = subprocess.Popen(
                [sys.executable, "-m", "eyes.server.result_queue.worker"],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.close()
            consumers.append(consumer)
            deadline = time.monotonic() + 20
            while True:
                with sessions() as session:
                    state = session.get(ResultSubmission, UUID(receipt["id"])).status
                if state == "persisted":
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("standalone consumer did not persist result")
                time.sleep(0.1)
            report["independent_result_consumer_process"] = True
        report["status"] = "passed"
        print(json.dumps(report), flush=True)
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        for worker in [*consumers, process]:
            worker.terminate()
            worker.wait(timeout=15)
        engine.dispose()


if __name__ == "__main__":
    main()
