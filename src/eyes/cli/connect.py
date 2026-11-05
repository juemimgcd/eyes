"""Pair a locally installed Agent with Eyes, then run the existing Runner."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from eyes.contracts.pairing import PairingRedeem
from eyes.contracts.target import http_origin
from eyes.runner.client import ControlClient
from eyes.runner.config import Binding, RunnerConfig
from eyes.runner.files import write_json
from eyes.runner.main import run


def main(arguments):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server")
    parser.add_argument("--pair-code")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--entry", help="Python execute(input: dict, context) entry point")
    entry.add_argument("--url", help="local HTTP endpoint accepting input JSON")
    parser.add_argument("--python", help="Agent Python interpreter path")
    parser.add_argument("--output-pointer")
    parser.add_argument("--secret", action="append", default=[], metavar="NAME=ENV_VAR")
    parser.add_argument("--state-dir", type=Path, default=Path(".eyes/connector"))
    args = parser.parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "runner.lock").open("a") as lock:
        # Two processes sharing a credential/journal could execute the same local work.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another connector is using this state directory") from error
        path = directory / "connection.json"
        if path.exists():
            if (
                args.server
                or args.pair_code
                or args.entry
                or args.url
                or args.secret
                or args.python is not None
                or args.output_pointer is not None
            ):
                parser.error("connection already saved; resume with --state-dir only")
            saved = json.loads(path.read_bytes())
            config = RunnerConfig.model_validate(saved["config"])
        else:
            if not args.server or not args.pair_code or not (args.entry or args.url):
                parser.error("first connection requires --server, --pair-code and --entry or --url")
            http_origin(args.server)
            server = urlsplit(args.server)
            # Public-network pairing must not send its credential over plaintext HTTP.
            if server.scheme != "https" and server.hostname not in {
                "127.0.0.1",
                "::1",
                "localhost",
            }:
                parser.error("use HTTPS for a remote Eyes server")
            if server.query or server.fragment:
                parser.error("server must be an API base URL without query or fragment")
            refs = {}
            for value in args.secret:
                name, separator, variable = value.partition("=")
                if not separator or not name or not variable or name in refs:
                    parser.error("--secret must be a unique NAME=ENV_VAR mapping")
                if not os.environ.get(variable):
                    parser.error(f"missing Agent environment variable: {variable}")
                refs[name] = "env:" + variable
            token = "eyes_" + secrets.token_urlsafe(32)
            request = PairingRedeem(
                code=args.pair_code,
                token=token,
                adapter="python" if args.entry else "http",
                url=args.url,
                output_pointer=args.output_pointer or "",
                secret_refs=refs,
            )
            bindings = {}
            if args.entry:
                binding = Binding(
                    entry_point=args.entry,
                    # Keep the venv executable path: resolving its symlink loses site-packages.
                    python=str(Path(args.python or sys.executable).absolute()),
                    import_paths=[Path.cwd()],
                    input_mode="input",
                    session_isolation=False,
                )
                # Resolve without executing a task; catch missing packages before pairing.
                subprocess.run(
                    [
                        binding.python,
                        "-c",
                        "import sys; from eyes.adapters.python import resolve; "
                        "resolve(sys.argv[1])",
                        binding.entry_point,
                    ],
                    check=True,
                    timeout=30,
                    stdout=sys.stderr,
                )
                bindings["connected"] = binding
            config = RunnerConfig(
                server_url=args.server,
                state_dir=directory,
                name="connector-" + directory.parent.name,
                score_slots=0,
                result_delivery="queued",
                python_agents=bindings,
                allowed_http_origins=[http_origin(args.url)] if args.url else [],
                secret_env_allowlist=[v.removeprefix("env:") for v in refs.values()],
            )
            saved = {
                "config": config.model_dump(mode="json"),
                "token": token,
                "pending": request.model_dump(mode="json"),
            }
            # Persist before redemption so a lost response can replay with the same credential.
            write_json(path, saved)
        try:
            asyncio.run(connect(config, saved, path))
        except Exception:
            print(
                f"连接中断；原始错误如下。可用 eyes connect --state-dir {str(directory)!r} 重试。",
                file=sys.stderr,
            )
            raise


async def connect(config, saved, path):
    if "pending" in saved:
        client = ControlClient(config.server_url, saved["token"], config.api_timeout_seconds)
        try:
            result = await client.request("POST", "v1/agent-pairings/redeem", json=saved["pending"])
        finally:
            await client.close()
        saved["target_version_id"] = result["target_version_id"]
        saved.pop("pending")
        write_json(path, saved)
    print(f"已配对目标 {saved['target_version_id']}；正在连接 Eyes。", flush=True)
    print(f"重启：eyes connect --state-dir {str(path.parent)!r}", flush=True)
    await run(config, saved["token"], False)
