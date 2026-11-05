import argparse
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import uvicorn
from sqlalchemy import delete, select, text

from eyes.agent_service.app import create_app
from eyes.agent_service.config import load_config
from eyes.agent_service.storage import Base, Event, Run, ServiceSchema, database, transition
from eyes.contracts.agent_protocol import TERMINAL_STATES, AgentEvidence, AgentProblem, AgentResult


def main():
    parser = argparse.ArgumentParser(
        description="Durable Eyes protocol wrapper for an Agent function"
    )
    parser.add_argument("--config", type=Path, default=Path("agent-service.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="initialize the separate Agent service tables")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9000)
    maintain = commands.add_parser(
        "maintain", help="expire terminal content after the retention window"
    )
    maintain.add_argument("--apply", action="store_true")
    resolve = commands.add_parser("resolve", help="record an operator-confirmed unknown outcome")
    resolve.add_argument("run_id", type=UUID)
    resolve.add_argument("--status", required=True, choices=[*sorted(TERMINAL_STATES), "unknown"])
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--stopped-confirmed", action="store_true", required=True)
    resolve.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    if args.command == "serve":
        uvicorn.run(create_app(config), host=args.host, port=args.port)
        return
    engine, sessions = database(config)
    try:
        if args.command == "init-db":
            with engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(74010)"))
                Base.metadata.create_all(connection)
                connection.execute(
                    text(
                        "INSERT INTO eyes_agent_schema (id, revision) VALUES (1, 1) "
                        "ON CONFLICT (id) DO NOTHING"
                    )
                )
            with sessions() as session:
                revision = session.get(ServiceSchema, 1).revision
                if revision != 1:
                    raise ValueError("Agent schema revision is not supported by this version")
            print(json.dumps({"schema_revision": revision}))
        elif args.command == "resolve":
            with sessions.begin() as session:
                run = session.scalar(
                    select(Run)
                    .where(
                        Run.id == args.run_id,
                        Run.service_id == config.service_id,
                    )
                    .with_for_update()
                )
                if run is None or run.status != "unknown":
                    parser.error("resolution requires an unknown run owned by this service")
                result = AgentResult(
                    protocol_version="1.0",
                    run_id=run.id,
                    status=args.status,
                    output=json.loads(args.output.read_bytes()) if args.output else None,
                    error=AgentProblem(code="operator_resolution", message=args.reason)
                    if args.status in {"failed", "unknown"}
                    else None,
                    stopped_confirmed=True,
                    completed_at=None if args.status == "unknown" else datetime.now(UTC),
                    cleanup_status="unknown",
                    evidence=AgentEvidence(status="partial", dropped_events=None),
                    artifacts=(run.result or {}).get("artifacts", []),
                )
                transition(run, args.status, result, f"operator: {args.reason}")
            print(result.model_dump_json())
        else:
            current = datetime.now(UTC)
            expired = []
            with sessions.begin() as session:
                for run in session.scalars(
                    select(Run)
                    .where(
                        Run.service_id == config.service_id,
                        Run.status.in_(TERMINAL_STATES),
                        Run.expired.is_(False),
                    )
                    .with_for_update()
                ):
                    completed = datetime.fromisoformat(run.result["completed_at"])
                    until = max(
                        run.deadline,
                        completed + timedelta(seconds=config.terminal_retention_seconds),
                    )
                    if current < until:
                        continue
                    expired.append(str(run.id))
                    if args.apply:
                        run.expired = True
                        run.task = {}
                        run.result = {**run.result, "output": None}
                        run.history = []
                        session.execute(delete(Event).where(Event.run_id == run.id))
            # Commit the tombstone first. A filesystem failure can be retried;
            # it cannot make deleted evidence look readable or permit re-execution.
            if args.apply:
                with sessions() as session:
                    ids = list(
                        session.scalars(
                            select(Run.id).where(
                                Run.service_id == config.service_id, Run.expired.is_(True)
                            )
                        )
                    )
                for run_id in ids:
                    path = config.state_dir / "runs" / str(run_id)
                    if path.exists():
                        shutil.rmtree(path)
            print(json.dumps({"apply": args.apply, "expired_run_ids": expired}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
