"""Quantify local result delivery with dedicated validation services, no model calls.

Reuses the existing real Python subprocess and synthetic scheduling fixture.
Fault injection targets only the named eyes-mq-validation containers.
"""

import argparse
import asyncio
import csv
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from validate_result_queue import setup_work
from validate_storage import docker, start_api

from eyes.contracts.work import ExecutionSubmission
from eyes.runner.client import ControlClient
from eyes.runner.config import RunnerConfig
from eyes.runner.outbox import Outbox
from eyes.runner.service import RunnerService
from eyes.server.config import Settings
from eyes.server.event_queue.broker import connect, declare_queue
from eyes.server.evidence.service import LocalArtifactStore
from eyes.server.result_queue.service import consume
from eyes.server.storage.database import database
from eyes.server.storage.models import Attempt, Manifest, ResultSubmission, WorkItem


def inspect_results(sessions, fixtures):
    rows = []
    with sessions() as session:
        for fixture in fixtures:
            attempt = session.get(Attempt, UUID(fixture["attempt_id"]))
            work = session.get(WorkItem, UUID(fixture["work_id"]))
            receipts = session.scalars(
                select(ResultSubmission).where(ResultSubmission.work_id == work.id)
            ).all()
            manifests = session.scalar(
                select(func.count()).select_from(Manifest).where(Manifest.attempt_id == attempt.id)
            )
            rows.append(
                {
                    "work_id": str(work.id),
                    "attempt_id": str(attempt.id),
                    "receipt_count": len(receipts),
                    "persisted": len(receipts) == 1 and receipts[0].status == "persisted",
                    "output_matches": attempt.result
                    == ExecutionSubmission.model_validate(fixture["submission"]).result.model_dump(
                        mode="json"
                    ),
                    "manifest_count": manifests,
                    "work_completed": work.status == "completed",
                    "reserved": work.reserved,
                }
            )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = Settings(_env_file=None, artifact_root=args.output / "artifacts")
    engine, sessions = database(settings)
    store = LocalArtifactStore(settings.artifact_root)
    report = {
        "environment": {
            "host": platform.platform(),
            "python": platform.python_version(),
            "samples_per_scenario": 100,
            "producer_concurrency": 4,
            "consumer_processes": 1,
            "payload": "real Python subprocess sum(range(1, 101)); synthetic scheduling fixture",
            "recovery_timing": "docker start command to all local ACKs; includes startup",
            "retry_poll_seconds": 0.2,
            "database": make_url(settings.database_url.get_secret_value()).database,
            "queue": settings.result_queue,
            "fault_injection": "Docker CLI stop/start of dedicated PostgreSQL/RabbitMQ containers",
            "http_client": f"HTTPX {httpx.__version__}; production ControlClient and Outbox",
            "timer": "time.monotonic(); observed after each flush round",
        },
        "status": "running",
        "scenarios": {},
    }
    workers = []
    api_process = start_api(settings, 58082, args.output, True)
    with connect(settings) as connection:
        declare_queue(
            connection.channel(),
            settings.result_queue,
            settings.event_queue_max_bytes,
            reliable=True,
        )
    try:
        fixtures = [setup_work(sessions) for _ in range(100)]
        print("duplicate: created 100 Python results", flush=True)
        with httpx.Client(base_url="http://127.0.0.1:58082", timeout=15, trust_env=False) as api:
            for fixture in fixtures:
                ids = []
                for _ in range(3):
                    response = api.post(
                        f"/v1/work/{fixture['work_id']}/result-submissions",
                        json=fixture["submission"],
                        headers={"Authorization": f"Bearer {fixture['runner_token']}"},
                    )
                    response.raise_for_status()
                    ids.append(response.json()["id"])
                assert len(set(ids)) == 1
        # Lose ACKs after successful commits by closing the real AMQP channel.
        deliveries = 0
        with connect(settings) as connection:
            channel = connection.channel()
            while True:
                method, properties, body = channel.basic_get(settings.result_queue, auto_ack=False)
                if method is None:
                    break
                consume(
                    sessions,
                    UUID(properties.message_id),
                    ExecutionSubmission.model_validate_json(body),
                    store,
                    settings,
                )
                deliveries += 1
        redeliveries = 0
        with connect(settings) as connection:
            channel = connection.channel()
            while True:
                method, properties, body = channel.basic_get(settings.result_queue, auto_ack=False)
                if method is None:
                    break
                assert method.redelivered
                consume(
                    sessions,
                    UUID(properties.message_id),
                    ExecutionSubmission.model_validate_json(body),
                    store,
                    settings,
                )
                channel.basic_ack(method.delivery_tag)
                redeliveries += 1
        rows = inspect_results(sessions, fixtures)
        report["scenarios"]["duplicates_and_lost_ack"] = {
            "unique_results": 100,
            "http_submissions": 300,
            "committed_deliveries_without_ack": deliveries,
            "observed_redeliveries": redeliveries,
            "rows": rows,
        }
        assert deliveries == redeliveries == 300
        assert all(
            r["persisted"]
            and r["output_matches"]
            and r["manifest_count"] == 1
            and r["work_completed"]
            and not r["reserved"]
            for r in rows
        )
        print(
            "duplicate: 300 submissions + 300 actual redeliveries, 100 unique results", flush=True
        )

        async def recovery(scenario, container):
            fixtures = [setup_work(sessions) for _ in range(100)]
            base = args.output / scenario
            base.mkdir()
            entries = []
            for index, fixture in enumerate(fixtures):
                folder = base / str(index)
                folder.mkdir()
                box = Outbox(folder / "outbox", 1024 * 1024, fixture["work_id"])
                runner = SimpleNamespace(config=RunnerConfig(result_delivery="queued"), outbox=box)
                RunnerService.queue_completion(
                    runner,
                    folder,
                    {"kind": "execute", "work_item_id": fixture["work_id"]},
                    fixture["submission"],
                )
                client = ControlClient("http://127.0.0.1:58082", fixture["runner_token"], 5)
                entries.append((box, client, folder))
            semaphore = asyncio.Semaphore(4)

            async def flush(entry):
                async with semaphore:
                    await entry[0].flush(entry[1], completions_only=True)

            log_path = base / "consumer.log"
            consumer = None
            try:
                # PostgreSQL fault starts only after all results have broker receipts.
                if scenario == "postgres_outage":
                    await asyncio.gather(*(flush(entry) for entry in entries))
                    assert all(
                        "submission_id" in json.loads(next(box.pending.glob("*.json")).read_text())
                        for box, _, _ in entries
                    )
                docker("stop", container)
                stopped_at = time.monotonic()
                if scenario == "rabbitmq_outage":
                    await asyncio.gather(*(flush(entry) for entry in entries))
                pending_during_outage = sum(box.status()["pending"] for box, _, _ in entries)
                assert pending_during_outage == 100
                assert all(not (folder / "ack.json").exists() for _, _, folder in entries)
                with log_path.open("w") as log:
                    consumer = subprocess.Popen(
                        [sys.executable, "-m", "eyes.server.result_queue.worker"],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env={**os.environ, "EYES_ARTIFACT_ROOT": str(settings.artifact_root)},
                    )
                workers.append(consumer)
                # Bound the wait so a consumer that never reaches the fault fails visibly.
                deadline = time.monotonic() + 20
                while "result_consumer_failed" not in log_path.read_text():
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{scenario}: consumer did not observe service outage")
                    await asyncio.sleep(0.1)
                await asyncio.sleep(5)
                restart_at = time.monotonic()
                docker("start", container)
                observations = []
                completed = {}
                while len(completed) < 100:
                    await asyncio.gather(
                        *(flush(entry) for entry in entries if entry[0].status()["pending"])
                    )
                    elapsed = time.monotonic() - restart_at
                    for index, (_box, _, folder) in enumerate(entries):
                        if index not in completed and (folder / "ack.json").exists():
                            completed[index] = elapsed
                    observations.append({"seconds": elapsed, "acknowledged": len(completed)})
                    if elapsed > 90:
                        raise TimeoutError(f"{scenario}: only {len(completed)}/100 acknowledged")
                    await asyncio.sleep(0.2)
                rows = inspect_results(sessions, fixtures)
                for index, row in enumerate(rows):
                    row["ack_after_restart_seconds"] = completed[index]
                result = {
                    "unique_results": 100,
                    "pending_during_outage": pending_during_outage,
                    "outage_hold_seconds": restart_at - stopped_at,
                    "all_acknowledged_seconds": max(completed.values()),
                    "rows": rows,
                    "progress": observations,
                }
                report["scenarios"][scenario] = result
                assert all(
                    r["persisted"]
                    and r["output_matches"]
                    and r["manifest_count"] == 1
                    and r["work_completed"]
                    and not r["reserved"]
                    for r in rows
                )
                assert (
                    sum(box.status()["pending"] + box.status()["rejected"] for box, _, _ in entries)
                    == 0
                )
                print(
                    f"{scenario}: 100/100 acknowledged in {max(completed.values()):.3f}s",
                    flush=True,
                )
            finally:
                docker("start", container)
                if consumer is not None:
                    consumer.terminate()
                    consumer.wait(timeout=15)
                    workers.remove(consumer)
                await asyncio.gather(*(client.close() for _, client, _ in entries))

        asyncio.run(recovery("rabbitmq_outage", "eyes-mq-validation-rabbit"))
        asyncio.run(recovery("postgres_outage", "eyes-mq-validation-pg"))
        report["status"] = "passed"
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        all_rows = [
            {"scenario": name, **row}
            for name, value in report["scenarios"].items()
            for row in value["rows"]
        ]
        with (args.output / "results.csv").open("w") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "scenario",
                    "work_id",
                    "attempt_id",
                    "receipt_count",
                    "persisted",
                    "output_matches",
                    "manifest_count",
                    "work_completed",
                    "reserved",
                    "ack_after_restart_seconds",
                ],
            )
            writer.writeheader()
            writer.writerows(all_rows)
        for worker in [*workers, api_process]:
            worker.terminate()
            worker.wait(timeout=15)
        engine.dispose()


if __name__ == "__main__":
    main()
