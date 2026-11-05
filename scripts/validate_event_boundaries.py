"""Authorization, atomic rejection and backup checks using real local services."""

import argparse
import hashlib
import json
import secrets
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from validate_storage import event_body, seed, start_api

from eyes.server.config import Settings
from eyes.server.event_queue.broker import EventPublisher, connect, declare
from eyes.server.event_queue.worker import receive_one
from eyes.server.operations.backup import backup
from eyes.server.storage.database import database
from eyes.server.storage.models import Credential, Event, EventSubmission


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, artifact_root=args.output / "artifacts")
    engine, sessions = database(settings)
    fixture = seed(sessions, 2)
    checks = {}
    process = start_api(settings, 58082, args.output, True)
    try:
        with httpx.Client(base_url="http://127.0.0.1:58082", timeout=10) as client:
            body = event_body(fixture)
            assert client.post("/v1/event-submissions", json=body).status_code == 401
            headers = {"Authorization": f"Bearer {fixture['runner_token']}"}
            assert (
                client.post(
                    "/v1/event-submissions", json={"events": []}, headers=headers
                ).status_code
                == 422
            )
            response = client.post("/v1/event-submissions", json=body, headers=headers)
            response.raise_for_status()
            sid = response.json()["id"]
            token = secrets.token_urlsafe(32)
            with sessions.begin() as session:
                session.add(
                    Credential(
                        project_id=UUID(fixture["project_id"]),
                        role="runner",
                        work_kinds=["execute"],
                        token_digest=hashlib.sha256(token.encode()).hexdigest(),
                    )
                )
            foreign = {"Authorization": f"Bearer {token}"}
            assert (
                client.post("/v1/event-submissions", json=body, headers=foreign).status_code == 403
            )
            assert client.get(f"/v1/event-submissions/{sid}", headers=foreign).status_code == 403
            checks["authentication_schema_and_runner_ownership"] = True
            try:
                backup(sessions, settings, args.output / "blocked-backup")
            except ValueError as exc:
                assert "drain pending event submissions" in str(exc), str(exc)
            else:
                raise AssertionError("pending broker payload must block DB-only backup")
            checks["pending_submission_blocks_backup"] = True
            with connect(settings) as conn:
                receive_one(conn.channel(), sessions, settings)
            # A batch may insert one event before discovering a conflicting second
            # event. Verify that the entire batch rolls back before recording rejection.
            fresh = event_body(fixture, 1)["events"][0]
            conflicting = {**body["events"][0], "data": {"changed": True}}
            mixed = {"schema_version": "1.0", "events": [fresh, conflicting]}
            response = client.post("/v1/event-submissions", json=mixed, headers=headers)
            response.raise_for_status()
            with connect(settings) as conn:
                receive_one(conn.channel(), sessions, settings)
            with sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.event_id == UUID(fresh["event_id"]))
                    )
                    == 0
                )
                assert (
                    session.get(EventSubmission, UUID(response.json()["id"])).status == "rejected"
                )
            checks["batch_conflict_rolls_back_all_events"] = True
            response = client.post(
                "/v1/event-submissions", json=event_body(fixture, 2), headers=headers
            )
            response.raise_for_status()
            with sessions.begin() as session:
                session.get(Credential, UUID(fixture["credential_id"])).revoked = True
            with connect(settings) as conn:
                receive_one(conn.channel(), sessions, settings)
            with sessions() as session:
                receipt = session.get(EventSubmission, UUID(response.json()["id"]))
                assert receipt.status == "rejected" and receipt.error["code"] == "invalid_runner"
            checks["revocation_before_consumption"] = True
            EventPublisher(settings).publish(uuid4(), b"not-json")
            with connect(settings) as conn:
                channel = conn.channel()
                declare(channel, settings)
                receive_one(channel, sessions, settings)
                # Dead-letter routing is asynchronous; query the broker afresh
                # until delivery appears rather than asserting an immediate count.
                deadline = time.monotonic() + 5
                while (
                    channel.queue_declare(
                        queue=f"{settings.event_queue}.rejected", passive=True
                    ).method.message_count
                    != 1
                ):
                    if time.monotonic() > deadline:
                        raise TimeoutError("rejected message did not reach the quarantine queue")
                    conn.sleep(0.1)
            checks["malformed_broker_payload_quarantined"] = True
        print(json.dumps(checks), flush=True)
    finally:
        (args.output / "report.json").write_text(json.dumps(checks, indent=2))
        process.terminate()
        process.wait(timeout=15)
        engine.dispose()


if __name__ == "__main__":
    main()
