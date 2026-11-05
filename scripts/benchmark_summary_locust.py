"""Run Locust ABBA and increasing-user cache comparisons on isolated services."""

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import subprocess
import time
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url
from validate_storage import seed, start_api

from eyes.server.config import Settings
from eyes.server.storage.database import database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = Settings(_env_file=None)
    engine, sessions = database(settings)
    fixture = seed(sessions, 1000)
    processes = []
    report = {
        "environment": {
            "host": platform.platform(),
            "server_python": platform.python_version(),
            "locust": "2.47.0",
            "locust_python": "3.13.14",
            "database": make_url(settings.database_url.get_secret_value()).database,
            "data": "1000 synthetic CaseRun/Attempt/ScoreRun; 800 pass, 200 fail",
            "experiment_id": fixture["experiment_id"],
            "warmup_seconds": 10,
            "think_time_seconds": 0,
            "cache_ttl_seconds": 3600,
            "summary_slots_per_api": 4,
            "database_pool_per_api": 8,
            "database_overflow": 0,
            "scope": "local HTTP; stable completed experiment; no Agent/model/browser",
            "load_model": "closed loop HttpUser, one request at a time per user",
        },
        "phases": [],
    }
    clients = {}
    try:
        for name, port, cached in [("A", 58081, False), ("B", 58082, True)]:
            processes.append(start_api(settings, port, args.output, cached))
            clients[name] = httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=30,
                trust_env=False,
                headers={"Authorization": f"Bearer {fixture['manager_token']}"},
            )
        path = f"/v1/experiments/{fixture['experiment_id']}/results"
        start = time.perf_counter()
        cold = clients["B"].get(path)
        cold.raise_for_status()
        report["cold_cache_single_request_ms"] = (time.perf_counter() - start) * 1000
        plan = [
            ("A1", "A", 4, 60),
            ("B1", "B", 4, 60),
            ("B2", "B", 4, 60),
            ("A2", "A", 4, 60),
            ("A10", "A", 10, 30),
            ("B10", "B", 10, 30),
            ("A20", "A", 20, 30),
            ("B20", "B", 20, 30),
        ]
        for label, mode, users, seconds in plan:
            prefix = (args.output / label).resolve()
            before = clients[mode].get("/v1/operations").json()["summary_cache"]
            command = [
                "uv",
                "tool",
                "run",
                "--python",
                "3.13",
                "--from",
                "locust==2.47.0",
                "locust",
                "-f",
                "scripts/locust_summary.py",
                "--headless",
                "--only-summary",
                "--host",
                str(clients[mode].base_url).rstrip("/"),
                "--users",
                str(users),
                "--spawn-rate",
                str(users),
                "--run-time",
                str(seconds + 10),
                "--stop-timeout",
                "10",
                "--csv",
                str(prefix),
                "--csv-full-history",
                "--html",
                f"{prefix}.html",
            ]
            env = {
                **os.environ,
                "EYES_LOAD_TOKEN": fixture["manager_token"],
                "EYES_LOAD_EXPERIMENT": fixture["experiment_id"],
                "EYES_LOAD_OUTPUT": str(prefix),
            }
            print(f"START {label}: {users} users, 10s warmup + {seconds}s measurement", flush=True)
            with prefix.with_suffix(".log").open("w") as log:
                completed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            # Locust code 1 denotes observed request failures, retained as results.
            # Other process failures cannot be interpreted as a completed load phase.
            if completed.returncode not in (0, 1):
                raise RuntimeError(
                    f"Locust {label} exited {completed.returncode}; inspect phase log"
                )
            measurement = json.loads(prefix.with_suffix(".measurement.json").read_text())
            with prefix.with_suffix(".requests.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            successful = sorted(float(r["latency_ms"]) for r in rows if not r["error"])
            after = clients[mode].get("/v1/operations").json()["summary_cache"]
            phase = {
                "phase": label,
                "mode": "no Redis" if mode == "A" else "warm Redis",
                "users": users,
                "planned_measurement_seconds": seconds,
                "exit_code": completed.returncode,
                **measurement,
                "successful_requests": len(successful),
                "successful_p95_ms": successful[math.ceil(len(successful) * 0.95) - 1],
                "http_status_counts": {
                    status: sum(r["status"] == status for r in rows)
                    for status in sorted({r["status"] for r in rows})
                },
                "cache_counter_scope": "includes 10s warmup",
                "cache_counter_delta": {
                    key: after[key] - before[key]
                    for key in ["hits", "misses", "database_reads", "busy_rejections"]
                },
                "command": command,
            }
            report["phases"].append(phase)
            (args.output / "report.json").write_text(json.dumps(report, indent=2))
            print(
                f"DONE {label}: {phase['requests']} requests, {phase['failures']} failures, "
                f"Locust P95={phase['locust_p95_ms']}ms, RPS={phase['locust_rps']:.2f}",
                flush=True,
            )
        report["status"] = "completed"
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        for client in clients.values():
            client.close()
        for process in processes:
            process.terminate()
            process.wait(timeout=15)
        engine.dispose()
        manifest = {
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "source_sha256": {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for root in ["src/eyes", "scripts"]
                for p in Path(root).rglob("*.py")
            },
        }
        (args.output / "source-manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
