"""One trusted function in one process group, with a durable result handoff."""

import asyncio
import importlib
import inspect
import json
import os
import resource
import sys
import traceback
from pathlib import Path
from uuid import UUID

from eyes.agent_service.config import AgentServiceConfig
from eyes.agent_service.context import AgentContext
from eyes.contracts.agent_protocol import AgentTask
from eyes.runner.files import atomic_write, json_bytes, redact


def main():
    directory = Path(sys.argv[1]).resolve()
    job = json.loads((directory / "job.json").read_bytes())
    config = AgentServiceConfig.model_validate(job["config"])
    task = AgentTask.model_validate(job["task"])
    sys.path[:0] = [str(path) for path in config.import_paths]
    for kind, value in (
        (resource.RLIMIT_CPU, config.cpu_seconds),
        (resource.RLIMIT_AS, config.memory_bytes),
        (resource.RLIMIT_FSIZE, max(config.max_artifact_bytes, config.max_result_bytes)),
    ):
        if value is not None:
            _, hard = resource.getrlimit(kind)
            resource.setrlimit(
                kind, (min(value, hard) if hard != resource.RLIM_INFINITY else value, hard)
            )
    secret_values = [os.environ[name] for name in config.secret_env_allowlist]
    context = AgentContext(task, UUID(job["run_id"]), directory, config, secret_values)
    os.chdir(context.workspace)
    try:
        module, attribute = config.entry_point.split(":")
        function = getattr(importlib.import_module(module), attribute)
        output = function(task.input, context)
        if inspect.isawaitable(output):
            output = asyncio.run(output)
        if not isinstance(output, dict):
            raise ValueError("Agent function must return a JSON object")
        body = json_bytes(
            {
                "output": redact(output, secret_values),
                "error": None,
                "dropped_events": context.dropped_events,
                "capture_error": context.capture_error,
                "artifacts": [item.model_dump(mode="json") for item in context.artifacts],
            }
        )
        if len(body) > config.max_result_bytes:
            raise ValueError("Agent result exceeds the configured byte limit")
    except Exception as error:
        # Preserve diagnostic traceback locally; only a bounded, redacted message
        # crosses the authenticated protocol boundary.
        atomic_write(
            directory / "error.txt", redact(traceback.format_exc(), secret_values).encode()
        )
        body = json_bytes(
            {
                "output": None,
                "error": redact(f"{type(error).__name__}: {error}", secret_values)[:2000],
                "dropped_events": context.dropped_events,
                "capture_error": context.capture_error,
                "artifacts": [item.model_dump(mode="json") for item in context.artifacts],
            }
        )
    atomic_write(directory / "outcome.json", body)


if __name__ == "__main__":
    main()
