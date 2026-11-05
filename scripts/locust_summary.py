"""Locust workload for immutable synthetic experiment summaries.

Required environment: EYES_LOAD_TOKEN, EYES_LOAD_EXPERIMENT, EYES_LOAD_OUTPUT.
Use --run-time equal to warmup + measurement, --stop-timeout 10, no think time.
"""

import csv
import json
import os
import time
from pathlib import Path

import gevent
from locust import HttpUser, constant, events, task

output = Path(os.environ["EYES_LOAD_OUTPUT"])
path = f"/v1/experiments/{os.environ['EYES_LOAD_EXPERIMENT']}/results"
token = os.environ["EYES_LOAD_TOKEN"]
measuring = False
samples = []
started_at = None
warmup = float(os.environ.get("EYES_LOAD_WARMUP", "10"))


@events.test_start.add_listener
def start_measurement(environment, **kwargs):
    def after_warmup():
        global measuring, started_at
        environment.stats.reset_all()
        started_at = time.time()
        measuring = True

    gevent.spawn_later(warmup, after_warmup)


@events.request.add_listener
def record_request(response_time, response, exception, **kwargs):
    if measuring:
        samples.append(
            {
                "elapsed_seconds": time.time() - started_at,
                "latency_ms": response_time,
                "status": response.status_code if response is not None else 0,
                "error": str(exception) if exception is not None else "",
            }
        )


@events.test_stop.add_listener
def save_measurement(environment, **kwargs):
    global measuring
    measuring = False
    with output.with_suffix(".requests.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "elapsed_seconds",
                "latency_ms",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(samples)
    total = environment.stats.total
    output.with_suffix(".measurement.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "ended_at": time.time(),
                "duration_seconds": time.time() - started_at,
                "requests": total.num_requests,
                "failures": total.num_failures,
                "locust_p50_ms": total.get_response_time_percentile(0.5),
                "locust_p95_ms": total.get_response_time_percentile(0.95),
                "locust_p99_ms": total.get_response_time_percentile(0.99),
                "locust_rps": total.total_rps,
                "raw_samples": len(samples),
            },
            indent=2,
        )
    )


class ExperimentReader(HttpUser):
    wait_time = constant(0)

    @task
    def read_summary(self):
        with self.client.get(
            path,
            name="experiment summary",
            headers={"Authorization": f"Bearer {token}"},
            catch_response=True,
            timeout=15,
        ) as response:
            # A 200 with incorrect summary contents must count as a failed request.
            if response.status_code == 200:
                value = response.json()
                if (
                    value["planned_runs"] != 1000
                    or value["execution_successes"] != 1000
                    or value["scorers"][0]["passed"] != 800
                ):
                    response.failure("summary content mismatch")
