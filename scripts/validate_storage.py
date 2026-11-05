"""Real-service event delivery checks and paired HTTP summary-cache benchmark.

Only use a disposable database/broker. No model calls or real Agent execution.
Run with EYES_DATABASE_URL, EYES_REDIS_URL and EYES_RABBITMQ_URL configured.
Fault checks operate only on the explicitly supplied validation container names.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import platform
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from redis import Redis
from sqlalchemy import func, select

from eyes.contracts.evidence import EventBatch
from eyes.runner.client import ControlClient
from eyes.runner.outbox import Outbox
from eyes.server.config import Settings
from eyes.server.event_queue.broker import connect, declare
from eyes.server.event_queue.worker import consume, receive_one
from eyes.server.storage.database import database
from eyes.server.storage.models import (
    Attempt,
    CaseRun,
    CaseVersion,
    Credential,
    DatasetVersion,
    Event,
    Experiment,
    Manifest,
    Project,
    Runner,
    ScoreRun,
    ScorerVersion,
    Target,
    TargetVersion,
    now,
)


def seed(sessions, count):
    """Synthetic persisted evaluation results, not claimed real Agent outcomes."""
    manager_token, runner_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with sessions.begin() as session:
        project = Project(name=f"storage-validation-{uuid4()}")
        session.add(project)
        session.flush()
        manager = Credential(
            project_id=project.id,
            role="manage",
            token_digest=hashlib.sha256(manager_token.encode()).hexdigest(),
        )
        credential = Credential(
            project_id=project.id,
            role="runner",
            work_kinds=["execute"],
            token_digest=hashlib.sha256(runner_token.encode()).hexdigest(),
        )
        target = Target(
            project_id=project.id, name="synthetic-storage-fixture", concurrency_limit=4
        )
        session.add_all([manager, credential, target])
        session.flush()
        version = TargetVersion(
            project_id=project.id,
            target_id=target.id,
            name=target.name,
            digest="0" * 64,
            content={},
        )
        dataset = DatasetVersion(
            project_id=project.id, name="synthetic-results", digest="1" * 64, content={}
        )
        scorer = ScorerVersion(
            project_id=project.id, name="synthetic-verdicts", digest="2" * 64, content={}
        )
        runner = Runner(credential_id=credential.id, name="fixture-only", capabilities={})
        session.add_all([version, dataset, scorer, runner])
        session.flush()
        experiment = Experiment(
            project_id=project.id,
            target_id=version.id,
            target_scope_id=target.id,
            dataset_id=dataset.id,
            request_key=str(uuid4()),
            request_digest="3" * 64,
            status="completed",
            snapshot={"scorers": [{"id": str(scorer.id)}], "request": {"concurrency": 4}},
        )
        cases = [
            CaseVersion(
                dataset_id=dataset.id,
                case_id=f"case-{i}",
                digest="4" * 64,
                content={"input": {"prompt": f"synthetic case {i}"}, "expectations": {}},
            )
            for i in range(count)
        ]
        session.add_all([experiment, *cases])
        session.flush()
        runs = [
            CaseRun(
                experiment_id=experiment.id,
                case_version_id=case.id,
                repetition=0,
                status="succeeded",
            )
            for case in cases
        ]
        session.add_all(runs)
        session.flush()
        attempts = [
            Attempt(
                case_run_id=run.id,
                project_id=project.id,
                runner_id=runner.id,
                trace_id=uuid4().hex,
                root_span_id=uuid4().hex[:16],
                idempotency_key=str(uuid4()),
                deadline=now() + timedelta(hours=1),
                status="succeeded",
                result={"output": "synthetic result", "dropped_events": 0},
            )
            for run in runs
        ]
        session.add_all(attempts)
        session.flush()
        manifests = [
            Manifest(
                attempt_id=a.id,
                version=1,
                content={"status": "partial", "references": []},
                digest="5" * 64,
            )
            for a in attempts
        ]
        session.add_all(manifests)
        session.flush()
        session.add_all(
            [
                ScoreRun(
                    attempt_id=a.id,
                    scorer_id=scorer.id,
                    manifest_id=m.id,
                    trace_id=uuid4().hex,
                    root_span_id=uuid4().hex[:16],
                    status="completed",
                    result={"verdict": "fail" if i % 5 == 0 else "pass"},
                )
                for i, (a, m) in enumerate(zip(attempts, manifests, strict=True))
            ]
        )
        result = {
            "project_id": str(project.id),
            "experiment_id": str(experiment.id),
            "attempt_id": str(attempts[0].id),
            "trace_id": attempts[0].trace_id,
            "credential_id": str(credential.id),
            "manager_token": manager_token,
            "runner_token": runner_token,
        }
    return result


def event_body(fixture, sequence=0):
    return {
        "schema_version": "1.0",
        "events": [
            {
                "schema_version": "1.0",
                "event_id": str(uuid4()),
                "producer_id": "storage-validation",
                "attempt_id": fixture["attempt_id"],
                "trace_id": fixture["trace_id"],
                "span_id": uuid4().hex[:16],
                "sequence": sequence,
                "occurred_at": now().isoformat(),
                "type": "validation.event",
                "source": "runner",
                "data": {"sequence": sequence},
            }
        ],
    }


def wait_api(url, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("validation API exited; inspect API log")
        try:
            if httpx.get(f"{url}/health/ready", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise TimeoutError("validation API did not become ready")


def start_api(settings, port, output, cached):
    env = {
        **os.environ,
        "EYES_DATABASE_URL": settings.database_url.get_secret_value(),
        "EYES_RABBITMQ_URL": settings.rabbitmq_url.get_secret_value(),
        "EYES_EVENT_QUEUE": settings.event_queue,
        "EYES_DATABASE_POOL_SIZE": "8",
        "EYES_DATABASE_MAX_OVERFLOW": "0",
        "EYES_CACHE_QUERY_CONCURRENCY": "4",
        "EYES_CACHE_TTL_SECONDS": "3600",
    }
    # Explicit constructor override avoids inheriting a Redis URL from .env for A.
    code = (
        "import uvicorn; from eyes.server.api.app import create_app; "
        "from eyes.server.config import Settings; "
        f"s=Settings(_env_file=None, artifact_root={str(output / 'artifacts')!r}"
        + (", redis_url=None" if not cached else "")
        + f"); uvicorn.run(create_app(s),host='127.0.0.1',port={port},access_log=False)"
    )
    log = (output / f"api-{port}.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    log.close()
    wait_api(f"http://127.0.0.1:{port}", process)
    return process


def docker(action, name):
    subprocess.run(["docker", action, name], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=1000)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--skip-benchmark", action="store_true")
    parser.add_argument("--redis-container", required=True)
    parser.add_argument("--rabbit-container", required=True)
    parser.add_argument("--postgres-container", required=True)
    args = parser.parse_args()
    # Fault injection must never target an unrelated service by accidental name.
    for name in (args.redis_container, args.rabbit_container, args.postgres_container):
        if not name.startswith("eyes-mq-validation-"):
            parser.error("fault checks require isolated eyes-mq-validation-* containers")
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None)
    engine, sessions = database(settings)
    fixture = seed(sessions, args.cases)
    processes = []
    report = {
        "environment": {
            "host": platform.platform(),
            "python": sys.version,
            "cases": args.cases,
            "attempts": args.cases,
            "scores": args.cases,
            "concurrency": args.concurrency,
            "requests_per_phase": args.requests,
            "db_pool": 8,
            "summary_slots": 4,
            "warmup_requests": 10,
            "fixture": "synthetic stored results; no Agent/model calls",
            "percentile": "nearest rank",
            "scope": "loopback HTTP including authentication, database, Redis and JSON; no browser",
        },
        "fixture": {k: v for k, v in fixture.items() if not k.endswith("token")},
        "checks": {},
        "phases": [],
    }
    runner_headers = {"Authorization": f"Bearer {fixture['runner_token']}"}
    manage_headers = {"Authorization": f"Bearer {fixture['manager_token']}"}
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    # Unique queue configured by caller; never purge a shared application's queue.
    with connect(settings) as conn:
        channel = conn.channel()
        declare(channel, settings)
        if channel.queue_declare(queue=settings.event_queue, passive=True).method.message_count:
            raise RuntimeError("use an empty, dedicated validation queue")
    try:
        processes.append(start_api(settings, 58081, args.output, False))
        processes.append(start_api(settings, 58082, args.output, True))
        with httpx.Client(
            base_url="http://127.0.0.1:58082", headers=runner_headers, timeout=15
        ) as client:
            body = event_body(fixture)
            response = client.post("/v1/event-submissions", json=body)
            response.raise_for_status()
            receipt = response.json()
            sid = receipt["id"]
            assert receipt["status"] == "pending", receipt
            with sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.attempt_id == UUID(fixture["attempt_id"]))
                    )
                    == 0
                )
            report["checks"]["accepted_not_persisted"] = True
            repeated = client.post("/v1/event-submissions", json=body).json()
            assert repeated["id"] == sid
            # Exercise the crash window after database COMMIT but before broker ACK.
            with connect(settings) as conn:
                channel = conn.channel()
                method, properties, wire = channel.basic_get(settings.event_queue, auto_ack=False)
                assert method is not None
                consume(sessions, UUID(properties.message_id), EventBatch.model_validate_json(wire))
                # Closing without ACK emulates a consumer process lost after COMMIT.
            with connect(settings) as conn:
                channel = conn.channel()
                while receive_one(channel, sessions, settings):
                    pass
            assert client.get(f"/v1/event-submissions/{sid}").json()["status"] == "persisted"
            with sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.attempt_id == UUID(fixture["attempt_id"]))
                    )
                    == 1
                )
            report["checks"]["duplicate_and_commit_before_ack"] = True
            # Same event ID with a different body must remain diagnosable, not overwrite.
            body["events"][0]["data"] = {"changed": True}
            conflict = client.post("/v1/event-submissions", json=body).json()
            with connect(settings) as conn:
                receive_one(conn.channel(), sessions, settings)
            rejected = client.get(f"/v1/event-submissions/{conflict['id']}").json()
            assert (
                rejected["status"] == "rejected" and rejected["error"]["code"] == "event_conflict"
            ), rejected
            report["checks"]["conflict_rejected_with_cause"] = True

            # Real Runner outbox must retain events across broker acceptance/restart.
            async def local_delivery():
                box = Outbox(args.output / "runner-outbox", 1024 * 1024, "validation")
                transport = ControlClient(
                    "http://127.0.0.1:58082", fixture["runner_token"], 5, 8 * 1024 * 1024
                )
                try:
                    box.enqueue(
                        "queued-event",
                        {
                            "kind": "events",
                            "work_id": "validation-work",
                            "path": "v1/event-submissions",
                            "body": event_body(fixture, 1),
                        },
                    )
                    await box.flush(transport)
                    assert box.evidence("validation-work") == (1, 0)
                    docker("restart", args.rabbit_container)
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            with connect(settings) as conn:
                                assert receive_one(conn.channel(), sessions, settings)
                            break
                        except Exception:
                            if time.monotonic() >= deadline:
                                raise
                            await asyncio.sleep(0.3)
                    # Reconstruct from disk, as after a Runner restart.
                    restored = Outbox(args.output / "runner-outbox", 1024 * 1024, "validation")
                    await restored.flush(transport)
                    assert restored.evidence("validation-work") == (0, 0)
                finally:
                    await transport.client.aclose()
                    await transport.transfer_client.aclose()

            asyncio.run(local_delivery())
            report["checks"]["broker_restart_and_local_receipt_gate"] = True
            docker("stop", args.rabbit_container)
            outage_body = event_body(fixture, 2)
            try:
                unavailable = client.post("/v1/event-submissions", json=outage_body)
                assert unavailable.status_code == 503, unavailable.text
            finally:
                docker("start", args.rabbit_container)
            deadline = time.monotonic() + 30
            while True:
                response = client.post("/v1/event-submissions", json=outage_body)
                if response.status_code == 200:
                    break
                if time.monotonic() > deadline:
                    response.raise_for_status()
                time.sleep(0.3)
            # A DB failure during consumption leaves the broker delivery unacked.
            docker("stop", args.postgres_container)
            try:
                from sqlalchemy.exc import SQLAlchemyError

                try:
                    with connect(settings) as conn:
                        receive_one(conn.channel(), sessions, settings)
                except SQLAlchemyError:
                    pass
                else:
                    raise AssertionError("expected database failure during consumption")
            finally:
                docker("start", args.postgres_container)
            wait_api("http://127.0.0.1:58082", processes[1])
            with connect(settings) as conn:
                assert receive_one(conn.channel(), sessions, settings)
            assert (
                client.get(f"/v1/event-submissions/{response.json()['id']}").json()["status"]
                == "persisted"
            )
            report["checks"]["broker_and_database_outage_retry"] = True
            # Run the shipped consumer entry point, not only its transaction helper.
            worker_log = (args.output / "consumer.log").open("w")
            consumer = subprocess.Popen(
                [sys.executable, "-m", "eyes.server.event_queue.worker"],
                stdout=worker_log,
                stderr=subprocess.STDOUT,
            )
            worker_log.close()
            processes.append(consumer)
            batch = {
                "schema_version": "1.0",
                "events": [event_body(fixture, index + 10)["events"][0] for index in range(100)],
            }
            response = client.post("/v1/event-submissions", json=batch)
            response.raise_for_status()
            submission_id = response.json()["id"]
            deadline = time.monotonic() + 30
            while True:
                state = client.get(f"/v1/event-submissions/{submission_id}").json()
                if state["status"] == "persisted":
                    break
                assert state["status"] == "pending", state
                if time.monotonic() > deadline:
                    raise TimeoutError("consumer did not persist the batch")
                time.sleep(0.1)
            with sessions() as session:
                count = session.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.attempt_id == UUID(fixture["attempt_id"]))
                )
                assert count == 103, count
            report["checks"]["consumer_process_100_events"] = {
                "new_events": 100,
                "stored_total": count,
            }
        print("event delivery checks passed", flush=True)
        if args.skip_benchmark:
            report["status"] = "event_checks_passed"
            return
        path = f"/v1/experiments/{fixture['experiment_id']}/results"
        clients = {
            name: httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers=manage_headers,
                timeout=30,
                limits=httpx.Limits(
                    max_connections=args.concurrency, max_keepalive_connections=args.concurrency
                ),
            )
            for name, port in (("A", 58081), ("B", 58082))
        }

        def validate_result(value):
            assert (
                value["planned_runs"] == args.cases and value["execution_successes"] == args.cases
            )
            assert value["scorers"][0]["passed"] == args.cases - math.ceil(args.cases / 5)

        with (args.output / "requests.csv").open("w", newline="") as raw:
            writer = csv.DictWriter(
                raw, fieldnames=["phase", "index", "latency_ms", "status", "error"]
            )
            writer.writeheader()
            for label, mode in (("A1", "A"), ("B1", "B"), ("B2", "B"), ("A2", "A")):
                client = clients[mode]
                for _ in range(10):
                    r = client.get(path)
                    r.raise_for_status()
                    validate_result(r.json())
                before = client.get("/v1/operations").json()["summary_cache"]

                def sample(index, client=client, label=label):
                    start = time.perf_counter()
                    try:
                        response = client.get(path)
                        elapsed = (time.perf_counter() - start) * 1000
                        response.raise_for_status()
                        validate_result(response.json())
                        return {
                            "phase": label,
                            "index": index,
                            "latency_ms": elapsed,
                            "status": response.status_code,
                            "error": "",
                        }
                    except Exception as exc:
                        return {
                            "phase": label,
                            "index": index,
                            "latency_ms": (time.perf_counter() - start) * 1000,
                            "status": 0,
                            "error": type(exc).__name__,
                        }

                start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    rows = list(pool.map(sample, range(args.requests)))
                elapsed = time.perf_counter() - start
                writer.writerows(rows)
                raw.flush()
                after = client.get("/v1/operations").json()["summary_cache"]
                values = sorted(row["latency_ms"] for row in rows if not row["error"])
                phase = {
                    "phase": label,
                    "mode": "warm Redis" if mode == "B" else "no Redis",
                    "requests": len(rows),
                    "errors": len(rows) - len(values),
                    "elapsed_seconds": elapsed,
                    "p50_ms": values[math.ceil(len(values) * 0.5) - 1],
                    "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
                    "requests_per_second": len(rows) / elapsed,
                    "counter_delta": {
                        key: after[key] - before[key]
                        for key in (
                            "hits",
                            "misses",
                            "database_reads",
                            "busy_rejections",
                            "cache_errors",
                            "bypasses",
                        )
                    },
                }
                report["phases"].append(phase)
                print(json.dumps(phase), flush=True)
        # Invalidate through a persisted status change and verify no old summary is reused.
        b = clients["B"]
        original = b.get(path).json()
        with sessions.begin() as session:
            experiment = session.get(Experiment, UUID(fixture["experiment_id"]))
            experiment.status = "completed_with_unresolved"
        changed = b.get(path).json()
        assert (
            changed["status"] == "completed_with_unresolved"
            and changed["computed_at"] != original["computed_at"]
        )
        report["checks"]["versioned_invalidation"] = True
        docker("stop", args.redis_container)
        try:
            failures = []
            for _ in range(2):
                response = b.get(path)
                assert response.status_code == 200, response.text
                assert response.json()["status"] == changed["status"]

            # Simultaneous cold reads expose the bounded fallback under real work.
            # Separate client pool avoids artificially limiting the burst to four.
            with httpx.Client(
                base_url="http://127.0.0.1:58082", headers=manage_headers, timeout=30
            ) as burst:

                def one(_):
                    r = burst.get(path)
                    return {"status": r.status_code, "code": r.json().get("error", {}).get("code")}

                with ThreadPoolExecutor(max_workers=16) as pool:
                    failures = list(pool.map(one, range(32)))
            assert all(
                r["status"] == 200 or (r["status"] == 503 and r["code"] == "summary_busy")
                for r in failures
            ), failures
            report["checks"]["redis_down"] = {"sequential_successes": 2, "burst": failures}
        finally:
            docker("start", args.redis_container)
        time.sleep(5.2)
        first = b.get(path)
        first.raise_for_status()
        before = b.get("/v1/operations").json()["summary_cache"]
        second = b.get(path)
        second.raise_for_status()
        after = b.get("/v1/operations").json()["summary_cache"]
        assert second.json()["status"] == changed["status"] and after["hits"] == before["hits"] + 1
        report["checks"]["redis_recovery_cache_hit"] = True
        for client in clients.values():
            client.close()
        assert all(p["errors"] == 0 for p in report["phases"]), report["phases"]
        report["status"] = "passed"
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        for process in processes:
            process.terminate()
            process.wait(timeout=15)
        redis.close()
        engine.dispose()
    print(f"Report: {args.output / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
