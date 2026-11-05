"""Connection checks execute in the selected Runner's network and secret scope."""

import asyncio
import logging
import traceback

import httpx

from eyes.agent_protocol.client import AgentClient
from eyes.agent_protocol.target import discovered_target, validate_target
from eyes.contracts.discovery import DiscoveryCreate
from eyes.runner.client import ApiError
from eyes.runner.files import error_text, redact, resolve_secrets

logger = logging.getLogger(__name__)


async def check(config, request):
    refs = {"token": f"env:{request.credential_env}"} if request.credential_env else {}
    secrets = resolve_secrets(refs, config.secret_env_allowlist, config.token_env)
    client = AgentClient(
        request.base_url,
        secrets.get("token"),
        allowed_origins=config.allowed_http_origins,
        connections=1,
        timeout=config.agent_timeout_seconds,
        pool_timeout=config.http_pool_timeout_seconds,
        max_response_bytes=config.max_message_bytes,
    )
    try:
        capabilities = await client.capabilities()
        target = discovered_target(
            request.name,
            request.base_url,
            capabilities,
            request.credential_env,
            request.concurrency,
        )
        validate_target(target)
        return {"target": target.model_dump(mode="json")}
    except Exception as error:
        # This is the diagnostic task boundary: preserve the failure as its
        # result without terminating the Runner's execution/heartbeat workers.
        logger.warning("discovery_failed %s", redact(traceback.format_exc(), secrets.values()))
        return {"error": error_text(error, secrets.values())}
    finally:
        await client.close()


async def serve(service):
    path = f"v1/runners/{service.runner_id}/discovery"
    while not service.stopping.is_set():
        try:
            response = await service.client.request("GET", path)
            item = response["check"]
            if item is not None:
                try:
                    request = DiscoveryCreate.model_validate(item["request"])
                    async with asyncio.timeout(30):
                        result = await check(service.config, request)
                except (ValueError, TimeoutError) as error:
                    result = {"error": error_text(error)}
                await service.client.request("POST", f"{path}/{item['id']}", json=result)
        except (httpx.HTTPError, ApiError) as error:
            logger.warning("discovery_control_unavailable type=%s", type(error).__name__)
        try:
            await asyncio.wait_for(service.stopping.wait(), 5)
        except TimeoutError:
            pass
