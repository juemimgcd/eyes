import threading

import pika

from eyes.server.domain import DomainError


def connect(settings):
    params = pika.URLParameters(settings.rabbitmq_url.get_secret_value())
    params.socket_timeout = settings.event_broker_timeout_seconds
    params.stack_timeout = settings.event_broker_timeout_seconds + 1
    params.blocked_connection_timeout = settings.event_broker_timeout_seconds
    params.heartbeat = 30
    params.connection_attempts = 1
    return pika.BlockingConnection(params)


def declare(channel, settings):
    declare_queue(channel, settings.event_queue, settings.event_queue_max_bytes, reliable=False)


def declare_queue(channel, queue, max_bytes, *, reliable):
    rejected = f"{queue}.rejected"
    channel.queue_declare(queue=rejected, durable=True, arguments={"x-queue-type": "quorum"})
    arguments = {
        "x-queue-type": "quorum",
        # Database outages can cause repeated requeues; never discard valid
        # evidence merely because a delivery-count limit was reached.
        "x-delivery-limit": -1,
        "x-max-length-bytes": max_bytes,
        "x-overflow": "reject-publish",
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": rejected,
    }
    if reliable:
        # Without this policy, dead-letter transfer itself may lose a result when
        # its destination is unavailable. Result messages require confirmed transfer.
        arguments["x-dead-letter-strategy"] = "at-least-once"
    channel.queue_declare(queue=queue, durable=True, arguments=arguments)


class Publisher:
    def __init__(self, settings, queue, *, reliable):
        self.settings = settings
        self.queue = queue
        self.reliable = reliable
        self.slots = threading.BoundedSemaphore(settings.event_publish_concurrency)

    def publish(self, submission_id, body):
        # A broker outage must not fill every API worker with blocked connections.
        if not self.slots.acquire(blocking=False):
            raise DomainError(503, "broker_busy", "publisher budget is exhausted")
        try:
            # BlockingConnection is thread-affine. Each bounded publish owns its
            # connection; no idle connection can silently lose its heartbeat.
            with connect(self.settings) as connection:
                channel = connection.channel()
                declare_queue(
                    channel, self.queue, self.settings.event_queue_max_bytes, reliable=self.reliable
                )
                channel.confirm_delivery()
                channel.basic_publish(
                    exchange="",
                    routing_key=self.queue,
                    body=body,
                    mandatory=True,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type="application/json",
                        message_id=str(submission_id),
                    ),
                )
        except pika.exceptions.AMQPError as exc:
            raise DomainError(
                503, "broker_unavailable", "publish not confirmed; retain and retry"
            ) from exc
        finally:
            self.slots.release()


class EventPublisher(Publisher):
    def __init__(self, settings):
        super().__init__(settings, settings.event_queue, reliable=False)
