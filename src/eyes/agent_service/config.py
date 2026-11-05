import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eyes.agent_protocol.schema import input_validator
from eyes.contracts.agent_protocol import AgentCapabilities


class AgentServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    agent_version: str | None = None
    entry_point: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
    python: str = sys.executable
    import_paths: list[Path] = Field(default_factory=list)
    database_url_env: str = "EYES_AGENT_DATABASE_URL"
    # Stable principal -> environment variable containing that caller's token.
    clients: dict[str, str] = Field(min_length=1, max_length=100)
    state_dir: Path = Path("data/agent-service")
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})
    max_concurrency: int = Field(default=1, ge=1, le=100)
    session_isolation: bool = True
    environment_isolation: bool = False
    execution_scope: Literal["process", "external"] = "external"
    secret_env_allowlist: list[str] = Field(default_factory=list)
    database_pool_size: int = Field(default=4, ge=2, le=100)
    database_pool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    poll_seconds: float = Field(default=0.5, ge=0.1, le=10)
    stop_grace_seconds: float = Field(default=5, ge=0.1, le=60)
    max_request_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    max_result_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    max_event_bytes: int = Field(default=64 * 1024, ge=1024)
    max_events: int = Field(default=10000, ge=1, le=100000)
    max_evidence_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_artifact_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_artifacts: int = Field(default=20, ge=0, le=100)
    terminal_retention_seconds: int = Field(default=7 * 86400, ge=86400)
    cpu_seconds: int | None = Field(default=None, ge=1)
    memory_bytes: int | None = Field(default=None, ge=64 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_service(self):
        input_validator(self.input_schema)
        self.capabilities()
        protected = {self.database_url_env, *self.clients.values()}
        if protected.intersection(self.secret_env_allowlist):
            raise ValueError("service credentials cannot be passed to Agent functions")
        if any(not principal or len(principal) > 100 for principal in self.clients):
            raise ValueError("caller principals must contain 1..100 characters")
        if os.name != "posix":
            raise ValueError("Agent service requires POSIX process groups")
        return self

    def capabilities(self):
        return AgentCapabilities(
            protocol_version="1.0",
            agent_id=self.service_id,
            agent_version=self.agent_version,
            input_schema=self.input_schema,
            max_concurrency=self.max_concurrency,
            session_isolation=self.session_isolation,
            environment_isolation=self.environment_isolation,
            cancellation=self.execution_scope == "process",
            resources=False,
            events=True,
            artifacts=True,
            terminal_retention_seconds=self.terminal_retention_seconds,
        )


def load_config(path: Path):
    config = AgentServiceConfig.model_validate(tomllib.loads(path.read_text()))
    base = path.resolve().parent
    config.state_dir = (base / config.state_dir).resolve()
    config.import_paths = [(base / path).resolve() for path in config.import_paths]
    if "/" in config.python and not Path(config.python).is_absolute():
        config.python = str((base / config.python).resolve())
    return config
