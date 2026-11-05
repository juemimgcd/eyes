import asyncio
import json
import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from eyes.server.domain import DomainError

logger = logging.getLogger(__name__)


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    kind: Literal[
        "catalog", "operations", "experiment", "batch", "attempt", "observed_run", "comparison"
    ]
    resource_id: UUID
    version: int = Field(ge=1)


class UpdateHub:
    """One Redis subscription per API process; bounded per-browser mailboxes."""

    def __init__(self, settings):
        self.settings = settings
        self.queues = {}
        self.connected = False
        self.task = None
        self.redis = None

    async def start(self):
        if self.settings.redis_url is not None:
            self.redis = Redis.from_url(
                self.settings.redis_url.get_secret_value(),
                max_connections=1,
                socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                socket_timeout=self.settings.redis_timeout_seconds,
                decode_responses=True,
            )
            self.task = asyncio.create_task(self.listen())

    def subscribe(self, project_id):
        if len(self.queues) >= self.settings.realtime_max_clients:
            raise DomainError(429, "stream_capacity", "live update connection budget is exhausted")
        queue = asyncio.Queue(maxsize=self.settings.realtime_queue_size)
        self.queues[queue] = str(project_id)
        return queue

    def unsubscribe(self, queue):
        self.queues.pop(queue, None)

    def resync(self, reason):
        for queue in self.queues:
            self.deliver(queue, {"event": "resync", "reason": reason})

    def deliver(self, queue, value):
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
            value = {"event": "resync", "reason": "slow_client"}
        queue.put_nowait(value)

    async def listen(self):
        while True:
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.psubscribe(f"{self.settings.redis_namespace}:updates:*")
                    self.connected = True
                    self.resync("redis_connected")
                    while True:
                        message = await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if message is None:
                            continue
                        try:
                            update = Update.model_validate_json(message["data"])
                        except ValidationError, TypeError:
                            logger.warning("invalid_resource_notification; requesting resync")
                            self.resync("invalid_notification")
                            continue
                        value = {"event": "update", **update.model_dump(mode="json")}
                        for queue, project_id in self.queues.items():
                            if project_id == str(update.project_id):
                                self.deliver(queue, value)
            except RedisError:
                # Repeated failed reconnects must not turn low-frequency
                # fallback into a database refresh every two seconds.
                if self.connected:
                    self.resync("redis_unavailable")
                self.connected = False
                logger.warning("Redis update subscription unavailable", exc_info=True)
                await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.redis:
            await self.redis.aclose()


def sse(value):
    return f"event: {value['event']}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n"
