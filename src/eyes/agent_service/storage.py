import hashlib
import os
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from eyes.contracts.agent_protocol import AgentRun


class Base(DeclarativeBase):
    pass


class ServiceSchema(Base):
    __tablename__ = "eyes_agent_schema"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]


class ServiceInstance(Base):
    __tablename__ = "eyes_agent_instances"
    service_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    generation: Mapped[UUID]


class Run(Base):
    __tablename__ = "eyes_agent_runs"
    __table_args__ = (
        UniqueConstraint("service_id", "principal", "idempotency_key"),
        UniqueConstraint("service_id", "principal", "attempt_id"),
        Index("ix_eyes_agent_service_status", "service_id", "status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[str] = mapped_column(String(100))
    principal: Mapped[str] = mapped_column(String(100))
    attempt_id: Mapped[UUID]
    task_id: Mapped[UUID]
    idempotency_key: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(64))
    task: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30))
    revision: Mapped[int] = mapped_column(default=1)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    event_count: Mapped[int] = mapped_column(default=0)
    history: Mapped[list] = mapped_column(JSONB, default=list)
    expired: Mapped[bool] = mapped_column(default=False)


class Event(Base):
    __tablename__ = "eyes_agent_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("eyes_agent_runs.id"), index=True)
    sequence: Mapped[int]
    content: Mapped[dict] = mapped_column(JSONB)


def database(config):
    url = os.environ[config.database_url_env]
    if make_url(url).drivername != "postgresql+psycopg":
        raise ValueError("Agent service database must use postgresql+psycopg")
    engine = create_engine(
        url,
        pool_size=config.database_pool_size,
        max_overflow=0,
        pool_timeout=config.database_pool_timeout_seconds,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    return engine, sessionmaker(engine, expire_on_commit=False)


def lock_key(service_id, purpose):
    return int.from_bytes(
        hashlib.sha256(f"eyes-agent:{service_id}:{purpose}".encode()).digest()[:8],
        "big",
        signed=True,
    )


def snapshot(run):
    return AgentRun(
        protocol_version="1.0",
        run_id=run.id,
        task_id=run.task_id,
        attempt_id=run.attempt_id,
        status=run.status,
        revision=run.revision,
        observed_at=datetime.now(UTC),
        result=run.result,
    )


def transition(run, status, result=None, reason=None):
    run.history = [
        *run.history,
        {
            "status": run.status,
            "revision": run.revision,
            "at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "result": run.result,
        },
    ]
    run.status = status
    run.result = result.model_dump(mode="json") if result else None
    run.revision += 1
