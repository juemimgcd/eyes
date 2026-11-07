"""Local model proxy with replayable observation uploads."""

import argparse
import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from eyes.contracts.observation import ObservationBatch
from eyes.runner.files import atomic_write, write_json
from eyes.tracing.model_proxy import create_proxy_app


async def upload(directory, server, token, active_runs=()):
    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=False,
        base_url=server.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        for path in sorted(directory.glob("*.json")):
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            acknowledgement = path.with_suffix(".ack")
            # A replayed start record must not refresh a dead request's liveness.
            if acknowledgement.exists() and acknowledgement.read_text() == digest:
                continue
            rejection = None
            try:
                body = ObservationBatch.model_validate_json(raw)
            except ValueError as error:
                rejection = {"error": type(error).__name__, "reason": "invalid observation batch"}
            else:
                response = await client.post(
                    "v1/observation/events",
                    json=body.model_dump(mode="json"),
                )
                # These statuses reject this batch; authentication, rate limits and outages
                # affect the destination and must leave the queue intact.
                if response.status_code in {400, 409, 413, 422}:
                    rejection = {
                        "error": "batch_rejected",
                        "http_status": response.status_code,
                        "response": response.text[:2000].replace(token, "[redacted]"),
                    }
                else:
                    response.raise_for_status()
            if rejection is not None:
                # An active request may have written its end while the upload was in flight.
                # Preserve that newer snapshot for the next pass rather than quarantining it.
                if path.read_bytes() == raw:
                    rejected = directory / "rejected"
                    rejected.mkdir(exist_ok=True, mode=0o700)
                    path.replace(rejected / path.name)
                    write_json(rejected / (path.stem + ".error.json"), rejection)
                    acknowledgement.unlink(missing_ok=True)
                logging.getLogger(__name__).error(
                    "observation batch rejected: %s %s", path.name, rejection
                )
                continue
            if body.events[-1].type == "run_end":
                path.unlink()
                acknowledgement.unlink(missing_ok=True)
            else:
                atomic_write(acknowledgement, digest.encode())
        # Only requests owned by this live proxy may refresh activity.
        for run_id in tuple(active_runs):
            response = await client.post("v1/observation/heartbeat", params={"run_id": run_id})
            response.raise_for_status()


def main(arguments):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["proxy", "upload"])
    parser.add_argument("--upstream-base-url")
    parser.add_argument("--server", default=os.environ.get("EYES_OBSERVATION_URL"))
    parser.add_argument("--token-env", default="EYES_OBSERVATION_TOKEN")
    parser.add_argument("--output-dir", type=Path, default=Path(".eyes/model-proxy"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--redact-field", action="append", default=[])
    args = parser.parse_args(arguments)
    token = os.environ.get(args.token_env)
    if not args.server or not token:
        parser.error("set --server and the observation token environment variable")
    url = urlsplit(args.server)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        parser.error("remote observation requires HTTPS")
    if url.username or url.password or url.query or url.fragment:
        parser.error("server must be an API base URL without credentials, query or fragment")
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "upload":
        asyncio.run(upload(args.output_dir, args.server, token))
        return
    if not args.upstream_base_url:
        parser.error("proxy requires --upstream-base-url")
    app = create_proxy_app(
        upstream_base_url=args.upstream_base_url,
        output_dir=args.output_dir,
        redacted_fields=set(args.redact_field),
    )
    original = app.router.lifespan_context

    async def publish():
        while True:
            try:
                await upload(args.output_dir, args.server, token, app.state.active_runs)
            except httpx.HTTPError, OSError, ValueError:
                # Cloud outages retain batches; the model response does not depend on upload.
                logging.getLogger(__name__).exception("observation upload failed; batches retained")
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            task = asyncio.create_task(publish())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port)
