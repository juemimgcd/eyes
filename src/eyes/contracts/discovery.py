from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from eyes.contracts.base import Contract
from eyes.contracts.target import TargetPublish, http_origin


class DiscoveryCreate(Contract):
    runner_id: UUID
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(max_length=2000)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    concurrency: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        http_origin(value)
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment:
            raise ValueError("Agent base URL cannot contain a query or fragment")
        return value.rstrip("/")


class DiscoveryResult(Contract):
    target: TargetPublish | None = None
    error: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def outcome(self):
        if (self.target is None) == (self.error is None):
            raise ValueError("provide exactly one target or error")
        return self
