"""Bounded process-local measurements with explicit timing boundaries."""

import math
import threading
from collections import deque
from datetime import UTC, datetime


class RuntimeMetrics:
    def __init__(self):
        self.since = datetime.now(UTC).isoformat()
        self.lock = threading.Lock()
        self.series = {
            name: {"count": 0, "failures": 0, "samples": deque(maxlen=512)}
            for name in (
                "database_checkout",
                "scheduling_lock",
                "http_response_headers",
                "claim_response_headers",
            )
        }

    def observe(self, name, seconds, *, failed=False):
        with self.lock:
            series = self.series[name]
            series["count"] += 1
            series["failures"] += int(failed)
            series["samples"].append(seconds)

    def snapshot(self):
        with self.lock:
            values = {
                name: (series["count"], series["failures"], sorted(series["samples"]))
                for name, series in self.series.items()
            }
        return {
            "scope": "process",
            "since": self.since,
            "window_size": 512,
            "timings": {
                name: {
                    "count": count,
                    "failures": failures,
                    "window_count": len(samples),
                    "mean_seconds": sum(samples) / len(samples) if samples else None,
                    "p95_seconds": samples[math.ceil(len(samples) * 0.95) - 1] if samples else None,
                    "max_seconds": samples[-1] if samples else None,
                }
                for name, (count, failures, samples) in values.items()
            },
        }


runtime_metrics = RuntimeMetrics()
