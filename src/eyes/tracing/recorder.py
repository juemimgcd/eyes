"""Translate proxy boundaries into durable Eyes observation batches."""

import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from eyes.contracts.base import SENSITIVE_KEYS
from eyes.contracts.observation import ObservationBatch, ObservationEvent
from eyes.runner.files import redact, write_json


def is_sensitive_field(key, value, extra):
    return key.lower().replace("-", "_") in SENSITIVE_KEYS | set(extra)


class ExecutionRecorder:
    def __init__(
        self, *, output, source, secret_values, redacted_fields, max_data_chars, active_runs
    ):
        self.output = output.with_suffix(".json")
        self.secrets = secret_values
        self.fields = redacted_fields or set()
        self.events = []
        self.trace_id = uuid4().hex
        self.active_runs = active_runs
        self.active_runs.add(self.trace_id)
        self.metadata = None
        self.failed = False

    def emit(self, *, data, phase, status, span_id, duration_ms=None, **fields):
        # Observation failure must not replace a response already being streamed to the Agent.
        try:
            self._emit(data, phase, status, span_id, duration_ms)
        except Exception:
            self.failed = True
            logging.getLogger(__name__).exception("proxy observation capture failed")

    def _emit(self, data, phase, status, span_id, duration_ms):
        if phase == "start":
            self.metadata = dict(
                run_id=self.trace_id,
                session_id=data["session_id"],
                agent="model-proxy (模型请求观测)",
                model=str(data["request"].get("model", "unknown"))[:200],
                capture_body=True,
            )
        payload = redact(data, self.secrets)
        if "model_requested_tools" in payload:
            payload["tool_calls"] = payload.pop("model_requested_tools")
        if "agent_reported_tool_results" in payload:
            payload["output"] = {
                "agent_reported_tool_results": payload.pop("agent_reported_tool_results")
            }

        # Custom sensitive keys are explicitly selected by the operator.
        def mask(value):
            if isinstance(value, dict):
                return {k: "[redacted]" if k in self.fields else mask(v) for k, v in value.items()}
            if isinstance(value, list):
                return [mask(v) for v in value]
            return value

        payload = mask(payload)
        metadata = {}
        if phase == "end":
            response = payload.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            # Providers may omit usage or individual counters; unknown values stay unknown.
            if isinstance(usage, dict):
                metadata["usage"] = {
                    target: usage[source]
                    for source, target in (
                        ("prompt_tokens", "input_tokens"),
                        ("completion_tokens", "output_tokens"),
                        ("total_tokens", "total_tokens"),
                    )
                    if type(usage.get(source)) is int and usage[source] >= 0
                }
            if status == "error":
                provider_error = response.get("error") if isinstance(response, dict) else None
                # Provider errors can contain a full echoed prompt; keep the UI diagnostic bounded.
                provider_error = json.dumps(provider_error, ensure_ascii=False)[:2000]
                metadata["error"] = {
                    "type": payload.get("error_type"),
                    "http_status": payload.get("http_status"),
                    "provider_error": provider_error,
                }
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        truncated = len(encoded) > 20000
        if truncated:
            payload = {"preview": encoded[:20000].decode(errors="ignore"), "truncated": True}
        payload.update(
            source="model_proxy",
            coverage="model_protocol_only",
            tool_execution_observed=False,
            status="ERROR" if status == "error" else status,
        )
        payload.update(metadata)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        def add(kind, value, cut=False):
            self.events.append(
                ObservationEvent(
                    event_id=uuid4(),
                    sequence=len(self.events) + 1,
                    occurred_at=datetime.now(UTC),
                    type=kind,
                    trace_id=self.trace_id,
                    span_id=span_id[:16],
                    data=value,
                    truncated=cut,
                )
            )

        if phase == "start":
            add(
                "run_start",
                {"prompt": "模型协议请求（非完整 Agent 任务）", "coverage": "model_protocol_only"},
            )
            add("model_start", payload, truncated)
        else:
            add("model_end", payload, truncated or bool(data.get("capture_issues")))
            add(
                "run_end",
                {
                    "status": "completed"
                    if status == "ok"
                    else "cancelled"
                    if status == "cancelled"
                    else "failed",
                    "dropped_events": int(self.failed),
                    "coverage": "model_protocol_only",
                },
            )
        write_json(
            self.output,
            ObservationBatch(**self.metadata, events=self.events).model_dump(mode="json"),
        )

    def finish(self):
        self.active_runs.discard(self.trace_id)
