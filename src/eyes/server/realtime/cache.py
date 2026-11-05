"""Versioned summaries; Redis never owns authoritative state or a DB connection."""

import json
import logging
import threading
import time
from collections import Counter

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from eyes.server.api.dependencies import transaction
from eyes.server.domain import DomainError, scoped
from eyes.server.experiments.service import summary
from eyes.server.storage.models import CacheEpoch, Experiment, ResourceRevision, now

logger = logging.getLogger(__name__)


class SummaryCache:
    def __init__(self, settings):
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.cache_query_concurrency)
        self.retry_at = 0.0
        self.started_at = now().isoformat()
        self.metrics_lock = threading.Lock()
        self.counters = Counter(
            hits=0,
            misses=0,
            bypasses=0,
            cache_errors=0,
            database_reads=0,
            busy_rejections=0,
            active_reads=0,
        )
        self.redis = (
            Redis.from_url(
                settings.redis_url.get_secret_value(),
                max_connections=settings.redis_max_connections,
                socket_connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_timeout_seconds,
                decode_responses=True,
            )
            if settings.redis_url is not None
            else None
        )

    def key(self, session, project_id, experiment_id):
        epoch = session.scalar(select(CacheEpoch.epoch).where(CacheEpoch.id == 1))
        revision = session.get(ResourceRevision, (project_id, "experiment", experiment_id))
        version = revision.version if revision else 0
        return (
            f"{self.settings.redis_namespace}:summary:v2:{epoch}:"
            f"{project_id}:{experiment_id}:{version}"
        )

    def record(self, **counts):
        with self.metrics_lock:
            self.counters.update(counts)

    def diagnostics(self):
        with self.metrics_lock:
            counts = dict(self.counters)
        lookups = counts["hits"] + counts["misses"]
        return {
            "scope": "api_process",
            "since": self.started_at,
            "redis_configured": self.redis is not None,
            "cooldown_remaining_seconds": max(0.0, self.retry_at - time.monotonic()),
            "query_concurrency_limit": self.settings.cache_query_concurrency,
            "hit_ratio": counts["hits"] / lookups if lookups else None,
            **counts,
        }

    def unavailable(self):
        # An outage otherwise makes every browser wait for the same socket timeout.
        self.retry_at = time.monotonic() + 5
        self.record(cache_errors=1)
        logger.warning("summary cache unavailable; using bounded database reads", exc_info=True)

    def results(self, request, project_id, experiment_id):
        with transaction(request) as session:
            scoped(session, Experiment, experiment_id, project_id)
            key = self.key(session, project_id, experiment_id)
        if self.redis is not None and time.monotonic() >= self.retry_at:
            try:
                cached = self.redis.get(key)
                if cached is not None:
                    value = json.loads(cached)
                    self.record(hits=1)
                    return value
                self.record(misses=1)
            except RedisError, ValueError:
                self.unavailable()
        else:
            self.record(bypasses=1)
        # Cache eviction and Redis outages can release many concurrent misses.
        # Reject surplus work instead of making it consume the API's whole pool.
        if not self.slots.acquire(blocking=False):
            self.record(busy_rejections=1)
            raise DomainError(503, "summary_busy", "summary query budget is exhausted; retry later")
        self.record(database_reads=1, active_reads=1)
        try:
            with transaction(request) as session:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                experiment = scoped(session, Experiment, experiment_id, project_id)
                key = self.key(session, project_id, experiment_id)
                value = {**summary(session, experiment), "computed_at": now().isoformat()}
            if self.redis is not None and time.monotonic() >= self.retry_at:
                try:
                    self.redis.set(key, json.dumps(value), ex=self.settings.cache_ttl_seconds)
                except RedisError:
                    self.unavailable()
            return value
        finally:
            self.record(active_reads=-1)
            self.slots.release()

    def close(self):
        if self.redis is not None:
            self.redis.close()
