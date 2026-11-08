"""Project-scoped, single-use connector pairing."""

from typing import Literal

from pydantic import Field, model_validator

from eyes.contracts.base import Contract
from eyes.contracts.target import http_origin


class PairingCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    purpose: Literal["observe", "evaluate"] = "evaluate"


class PairingRedeem(Contract):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    token: str = Field(pattern=r"^eyes_[A-Za-z0-9_-]{43}$")
    adapter: Literal["python", "http", "tunnel"]
    url: str | None = Field(default=None, max_length=2000)
    output_pointer: str = Field(default="", max_length=1000)
    secret_refs: dict[str, str] = Field(default_factory=dict, max_length=30)

    protocol: Literal["json", "chat_completions"] = "json"
    observation_token: str | None = Field(default=None, pattern=r"^eyes_obs_[A-Za-z0-9_-]{43}$")

    @model_validator(mode="after")
    def validate_connection(self):
        # An HTTP binding without a URL cannot execute; Python must not smuggle HTTP config.
        if self.adapter in {"http", "tunnel"}:
            if self.adapter == "http" and self.url is None:
                raise ValueError("HTTP connection requires url")
            if self.url is not None:
                http_origin(self.url)
            if self.output_pointer and not self.output_pointer.startswith("/"):
                raise ValueError("output_pointer must be a JSON Pointer")
        elif self.url is not None or self.output_pointer:
            raise ValueError("URL and output_pointer apply only to HTTP connections")
        # The server stores references, never the Agent's actual secrets.
        import re

        if any(
            not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", v) for v in self.secret_refs.values()
        ):
            raise ValueError("secrets must reference local environment variable names")
        if self.adapter == "http" and set(self.secret_refs) - {"agent_auth"}:
            raise ValueError("HTTP connection only accepts agent_auth")
        # Tunnel secrets stay on the client; separate telemetry credentials are mandatory.
        if self.adapter == "tunnel":
            if self.secret_refs or not self.observation_token:
                raise ValueError(
                    "tunnel requires observation_token and no remote secret references"
                )
        elif self.observation_token or self.protocol != "json":
            raise ValueError("protocol and observation_token apply only to tunnel connections")
        return self
