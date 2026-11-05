import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from eyes.agent_protocol.client import AgentClient
from eyes.agent_protocol.target import discovered_target, validate_target
from eyes.runner.config import load_config
from eyes.runner.files import resolve_secrets
from eyes.runner.service import RunnerService


def describe(config):
    entries = {"rules": None, **config.python_scorers}
    descriptions = {}
    environment = {
        key: os.environ[key]
        for key in ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"]
        if key in os.environ
    }
    for name, binding in entries.items():
        payload = binding.model_dump(mode="json") if binding else None
        source = (
            "import json,sys; from eyes.runner.config import Binding; "
            "from eyes.runner.worker import implementation_digest; "
            "v=json.load(sys.stdin); b=Binding.model_validate(v) if v else None; "
            "sys.path[:0]=[str(p) for p in b.import_paths] if b else []; "
            "print(json.dumps({'implementation_digest':implementation_digest(b)}))"
        )
        response = subprocess.run(
            [binding.python if binding else sys.executable, "-c", source],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            timeout=config.cleanup_timeout_seconds,
            check=True,
        )
        descriptions[name] = json.loads(response.stdout.splitlines()[-1])
    return {
        "schema_version": "1.0",
        "adapters": {
            "http": {"allowed_origins": config.allowed_http_origins},
            "python": {
                name: {
                    "entry_point": binding.entry_point,
                    "session_isolation": binding.session_isolation,
                    "environment_isolation": binding.environment_isolation,
                    "execution_scope": binding.execution_scope,
                    "cancellation": bool(binding.cancel) or binding.execution_scope == "process",
                    "reconciliation": bool(binding.reconcile),
                    "idempotency": binding.idempotency,
                }
                for name, binding in config.python_agents.items()
            },
        },
        "scorers": descriptions,
    }


async def run(config, token, once):
    service = RunnerService(config, token)
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, service.stopping.set)
    try:
        await service.run(once)
    finally:
        await service.client.close()
        await service.agent_clients.close()
        await service.recovery_clients.close()


async def discover(config, arguments):
    refs = {"token": f"env:{arguments.credential_env}"} if arguments.credential_env else {}
    secrets = resolve_secrets(refs, config.secret_env_allowlist, config.token_env)
    client = AgentClient(
        arguments.base_url,
        secrets.get("token"),
        allowed_origins=config.allowed_http_origins,
        connections=config.agent_connections_per_target,
        timeout=config.agent_timeout_seconds,
        pool_timeout=config.http_pool_timeout_seconds,
        max_response_bytes=config.max_message_bytes,
    )
    try:
        capabilities = await client.capabilities()
        target = discovered_target(
            arguments.name,
            arguments.base_url,
            capabilities,
            arguments.credential_env,
            arguments.concurrency,
        )
        validate_target(target)
        print(target.model_dump_json(indent=2))
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Eyes host Runner: HTTP and trusted Python Agent execution"
    )
    parser.add_argument("--config", type=Path, default=Path("runner.toml"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("run", help="register, claim, execute, score and report")
    start.add_argument(
        "--once",
        action="store_true",
        help="claim at most one execution and one score, then drain and exit",
    )
    subcommands.add_parser(
        "plugins", help="inspect local capabilities and scorer implementation digests"
    )
    subcommands.add_parser(
        "status", help="inspect local outbox and retained work without contacting the server"
    )
    discovery = subcommands.add_parser("discover", help="inspect an Agent and emit target JSON")
    discovery.add_argument("--base-url", required=True)
    discovery.add_argument("--name", required=True)
    discovery.add_argument("--credential-env")
    discovery.add_argument("--concurrency", type=int)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(arguments.config)
        if arguments.command == "discover":
            asyncio.run(discover(config, arguments))
            return
        if arguments.command == "plugins":
            print(json.dumps(describe(config), ensure_ascii=False, indent=2))
            return
        if arguments.command == "status":
            outbox = config.state_dir / "outbox"
            work = config.state_dir / "work"
            print(
                json.dumps(
                    {
                        "pending_uploads": len(list((outbox / "pending").glob("*.json"))),
                        "rejected_uploads": len(list((outbox / "rejected").glob("*.json"))),
                        "retained_work": len(list(work.glob("*/assignment.json"))),
                        "quarantined_work": len(list(work.glob("*/quarantine.json"))),
                    },
                    indent=2,
                )
            )
            return
        token = os.environ.get(config.token_env)
        if not token:
            raise ValueError(f"set Runner credential in environment variable {config.token_env}")
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (config.state_dir / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another Runner is using this state_dir") from None
            asyncio.run(run(config, token, arguments.once))
    except Exception as error:
        # Exception strings may contain URLs or credentials from dependencies.
        logging.error("runner_stopped type=%s", type(error).__name__)
        if isinstance(error, ValueError):
            logging.error("configuration: %s", error)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
