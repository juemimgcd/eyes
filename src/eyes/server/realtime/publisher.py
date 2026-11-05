import argparse
import json
import logging
import signal
import threading
from datetime import timedelta
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from eyes.server.config import Settings
from eyes.server.storage.database import database, maintenance_lock
from eyes.server.storage.models import NotificationOutbox, now

logger = logging.getLogger(__name__)


def redis_client(settings):
    if settings.redis_url is None:
        raise ValueError("set EYES_REDIS_URL to enable notification publishing")
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        max_connections=settings.redis_max_connections,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
        decode_responses=True,
    )


def publish(sessions, client, settings):
    token, current = uuid4(), now()
    with sessions.begin() as session:
        maintenance_lock(session)
        rows = session.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.available_at <= current,
                or_(
                    NotificationOutbox.lease_until.is_(None),
                    NotificationOutbox.lease_until <= current,
                ),
            )
            .order_by(
                NotificationOutbox.created_at,
                NotificationOutbox.project_id,
                NotificationOutbox.kind,
                NotificationOutbox.resource_id,
            )
            .limit(settings.notification_batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        batch = []
        for row in rows:
            row.claim_token, row.lease_until = token, current + timedelta(seconds=60)
            batch.append(
                {
                    "project_id": str(row.project_id),
                    "kind": row.kind,
                    "resource_id": str(row.resource_id),
                    "version": row.version,
                }
            )
    if not batch:
        return 0
    failure = None
    try:
        # The claim transaction is closed before Redis I/O. Pub/Sub is a hint;
        # absence of subscribers does not imply any durable event was lost.
        with client.pipeline(transaction=False) as pipeline:
            for item in batch:
                pipeline.publish(
                    f"{settings.redis_namespace}:updates:{item['project_id']}",
                    json.dumps(item, separators=(",", ":")),
                )
            pipeline.execute()
    except RedisError as error:
        failure = error
        logger.exception("notification_publish_failed; durable rows retained")
    with sessions.begin() as session:
        maintenance_lock(session)
        rows = session.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.claim_token == token,
            )
            .order_by(
                NotificationOutbox.project_id,
                NotificationOutbox.kind,
                NotificationOutbox.resource_id,
            )
            .with_for_update()
        ).all()
        sent = {
            (item["project_id"], item["kind"], item["resource_id"]): item["version"]
            for item in batch
        }
        for row in rows:
            version = sent[str(row.project_id), row.kind, str(row.resource_id)]
            if failure is None and row.version == version:
                session.delete(row)
                continue
            row.claim_token, row.lease_until = None, None
            row.failures = row.failures + 1 if failure else 0
            delay = min(60, 2 ** min(row.failures, 6)) if failure else 0
            row.available_at = now() + timedelta(seconds=delay)
    if failure:
        raise failure
    return len(batch)


def main():
    parser = argparse.ArgumentParser(description="Publish committed Eyes updates to Redis")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine, sessions = database(settings)
    client = redis_client(settings)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                sent = publish(sessions, client, settings)
                if sent:
                    logger.info("notifications_published count=%s", sent)
            except RedisError, SQLAlchemyError:
                logger.exception("notification_cycle_failed")
                if args.once:
                    raise
            if args.once:
                break
            stop.wait(settings.notification_poll_seconds)
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
