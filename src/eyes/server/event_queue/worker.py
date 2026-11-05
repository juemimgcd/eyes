import argparse
import logging
import signal
import threading
from uuid import UUID

import pika
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from eyes.contracts.evidence import EventBatch
from eyes.server.config import Settings
from eyes.server.domain import DomainError
from eyes.server.event_queue.broker import connect, declare
from eyes.server.event_queue.service import consume
from eyes.server.storage.database import database

logger = logging.getLogger(__name__)


def receive_one(channel, sessions, settings):
    method, properties, body = channel.basic_get(queue=settings.event_queue, auto_ack=False)
    if method is None:
        return False
    try:
        submission_id = UUID(properties.message_id)
        batch = EventBatch.model_validate_json(body)
        consume(sessions, submission_id, batch)
    except ValueError, ValidationError, TypeError:
        logger.exception("invalid_event_queue_message; moving to rejected queue")
        channel.basic_reject(method.delivery_tag, requeue=False)
    else:
        # Both events and the persisted/rejected receipt have committed. A crash
        # before this ACK redelivers safely through receipt and event deduplication.
        channel.basic_ack(method.delivery_tag)
    return True


def main():
    parser = argparse.ArgumentParser(description="Persist RabbitMQ execution events to PostgreSQL")
    parser.add_argument("--once", action="store_true", help="process at most one message")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.rabbitmq_url is None:
        parser.error("configure EYES_RABBITMQ_URL before starting the event consumer")
    engine, sessions = database(settings)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                with connect(settings) as connection:
                    channel = connection.channel()
                    declare(channel, settings)
                    channel.basic_qos(prefetch_count=1)
                    while not stop.is_set():
                        received = receive_one(channel, sessions, settings)
                        if args.once:
                            return
                        if not received:
                            connection.sleep(0.2)
            except pika.exceptions.AMQPError, SQLAlchemyError, DomainError:
                # Closing the connection requeues an unacknowledged message.
                # Pause after a DB/broker outage instead of hot-loop redelivery.
                logger.exception("event_consumer_cycle_failed; unacknowledged message retained")
                if args.once:
                    raise
                stop.wait(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
