"""Context passed to an installed Agent function: execute(input, context)."""

import hashlib
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from eyes.contracts.agent_protocol import AgentArtifact, AgentEvent, AgentTask
from eyes.runner.files import atomic_write, json_bytes, redact, write_json


class AgentContext:
    def __init__(self, task: AgentTask, run_id, directory: Path, config, secrets):
        self.task, self.run_id = task, run_id
        self.workspace = directory / "workspace"
        self.directory, self.config, self.secrets = directory, config, secrets
        self.lock = threading.Lock()
        self.sequence = 0
        self.event_bytes = 0
        self.dropped_events = 0
        self.artifacts = []
        self.capture_error = None

    def emit(self, event_type: str, data: dict):
        try:
            return self._emit(event_type, data)
        except ValueError, OSError:
            # Telemetry validation and disk failures must not change the Agent's
            # business return value. Preserve the latest diagnostic in its outcome.
            with self.lock:
                self.dropped_events += 1
                self.capture_error = redact(traceback.format_exc(), self.secrets)[-4000:]
            return False

    def _emit(self, event_type, data):
        with self.lock:
            event = AgentEvent(
                event_id=uuid4(),
                run_id=self.run_id,
                sequence=self.sequence,
                occurred_at=datetime.now(UTC),
                type=event_type,
                data=redact(data, self.secrets),
            )
            body = json_bytes(event.model_dump(mode="json"))
            if (
                self.sequence >= self.config.max_events
                or len(body) > self.config.max_event_bytes
                or self.event_bytes + len(body) > self.config.max_evidence_bytes
            ):
                self.dropped_events += 1
                return False
            atomic_write(self.directory / "events" / f"{self.sequence:010d}.json", body)
            self.sequence += 1
            self.event_bytes += len(body)
            return True

    def artifact(self, name: str, content: bytes, media_type="application/octet-stream"):
        with self.lock:
            if len(self.artifacts) >= self.config.max_artifacts:
                raise ValueError("Agent artifact count limit reached")
            if len(content) > self.config.max_artifact_bytes:
                raise ValueError("Agent artifact byte limit reached")
            if any(item.name == name for item in self.artifacts):
                raise ValueError("Agent artifact names must be unique")
            item = AgentArtifact(
                artifact_id=uuid4(),
                name=name,
                media_type=media_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            atomic_write(self.directory / "artifacts" / str(item.artifact_id), content)
            self.artifacts.append(item)
            write_json(
                self.directory / "artifacts.json",
                [artifact.model_dump(mode="json") for artifact in self.artifacts],
            )
            return item
