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
    method, properties, body = channel.basic_get(queue=settings.result_queue, auto_ack=False)
    if method is None:
        return False
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
    return True


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
                    while not stop.is_set():
                        received = receive_one(channel, sessions, settings, store)
                        if args.once:
                            return
                        if not received:
                            connection.sleep(0.2)
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
