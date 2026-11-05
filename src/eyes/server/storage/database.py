from time import perf_counter

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from eyes.server.config import Settings
from eyes.server.diagnostics import runtime_metrics
from eyes.server.realtime.transactions import ControlSession

SCHEMA_REVISION = "0008_report_retention"


class MeasuredQueuePool(QueuePool):
    def connect(self):
        started, acquired = perf_counter(), False
        try:
            connection = super().connect()
            acquired = True
            return connection
        finally:
            runtime_metrics.observe(
                "database_checkout", perf_counter() - started, failed=not acquired
            )


def database(settings: Settings):
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        poolclass=MeasuredQueuePool,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
    return engine, sessionmaker(engine, class_=ControlSession, expire_on_commit=False)


def scheduling_lock(session: Session) -> None:
    # Fixed order across claim, finish, cancellation and recovery. Global reservations
    # are counted only while these locks are held; keep all network I/O outside them.
    # Acquire the connection first so pool acquisition is not counted as lock wait.
    session.connection()
    started, acquired = perf_counter(), False
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(74001)"))
        session.execute(text("SELECT pg_advisory_xact_lock(74002)"))
        acquired = True
    finally:
        runtime_metrics.observe("scheduling_lock", perf_counter() - started, failed=not acquired)


def maintenance_lock(session: Session, *, exclusive=False) -> None:
    # Always acquire before scheduling/row locks. Backup and retention use the
    # exclusive form; normal control-plane transactions take a shared lock.
    suffix = "" if exclusive else "_shared"
    session.execute(text(f"SELECT pg_advisory_xact_lock{suffix}(74000)"))
