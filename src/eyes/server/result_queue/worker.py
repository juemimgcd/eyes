import argparse
import logging
import signal
import threading
from uuid import UUID

import pika
from sqlalchemy.exc import SQLAlchemyError

from eyes.contracts.work import ExecutionSubmission
from eyes.server.config import Settings
from eyes.server.domain import DomainError
from eyes.server.event_queue.broker import connect, declare_queue
from eyes.server.evidence.service import LocalArtifactStore
from eyes.server.result_queue.service import consume
from eyes.server.storage.database import database

logger = logging.getLogger(__name__)


def receive_one(channel, sessions, settings, store):
    """Process at most one delivery through a short-lived SAC subscription."""
    channel.basic_qos(prefetch_count=1)
    method, properties, body = next(
        channel.consume(settings.result_queue, auto_ack=False, inactivity_timeout=0.2)
    )
    if method is None:
        channel.cancel()
        return False
    process_message(channel, method, properties, body, sessions, settings, store)
    channel.cancel()
    return True


def process_message(channel, method, properties, body, sessions, settings, store):
    try:
        receipt_id = UUID(properties.message_id)
        submission = ExecutionSubmission.model_validate_json(body)
        status = consume(sessions, receipt_id, submission, store, settings)
    except ValueError, TypeError:
        logger.exception("invalid_result_message; preserving in dead-letter queue")
        channel.basic_reject(method.delivery_tag, requeue=False)
    else:
        if status == "rejected":
            # Preserve the original final result for diagnosis, even if current
            # task state no longer permits automatic application (e.g. stale lease).
            channel.basic_reject(method.delivery_tag, requeue=False)
        else:
            channel.basic_ack(method.delivery_tag)


def main():
    parser = argparse.ArgumentParser(description="Consume Python execution results into PostgreSQL")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.rabbitmq_url is None:
        parser.error("configure EYES_RABBITMQ_URL before starting the result consumer")
    engine, sessions = database(settings)
    store = LocalArtifactStore(settings.artifact_root)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                with connect(settings) as connection:
                    channel = connection.channel()
                    declare_queue(
                        channel,
                        settings.result_queue,
                        settings.event_queue_max_bytes,
                        reliable=True,
                    )
                    channel.basic_qos(prefetch_count=1)
                    for method, properties, body in channel.consume(
                        settings.result_queue, auto_ack=False, inactivity_timeout=0.2
                    ):
                        # Wake while idle so SIGTERM also stops an empty/standby consumer.
                        if stop.is_set():
                            break
                        if method is not None:
                            process_message(
                                channel, method, properties, body, sessions, settings, store
                            )
                        if args.once:
                            return
            except pika.exceptions.AMQPError, SQLAlchemyError, DomainError:
                logger.exception(
                    "result_consumer_failed; unacknowledged result will be redelivered"
                )
                if args.once:
                    raise
                stop.wait(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
