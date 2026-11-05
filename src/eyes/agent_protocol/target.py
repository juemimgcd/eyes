from eyes.agent_protocol.client import ProtocolConfig
from eyes.contracts.target import TargetCapabilities, TargetPublish


def discovered_target(name, base_url, capabilities, token_env=None, concurrency=None):
    config = ProtocolConfig(base_url=base_url, capabilities=capabilities)
    return TargetPublish(
        name=name,
        external_version=capabilities.agent_version,
        concurrency_limit=capabilities.max_concurrency if concurrency is None else concurrency,
        capabilities=TargetCapabilities(
            adapter="agent_protocol",
            session_isolation=capabilities.session_isolation,
            environment_isolation=capabilities.environment_isolation,
            cancellation=capabilities.cancellation,
            idempotency=True,
            reconciliation=True,
            observation=["task", "target_trace"] if capabilities.events else ["task"],
        ),
        config=config.model_dump(mode="json"),
        secret_refs={"token": f"env:{token_env}"} if token_env else {},
    )


def validate_target(target: TargetPublish):
    config = ProtocolConfig.model_validate(target.config)
    expected = discovered_target(target.name, config.base_url, config.capabilities)
    if target.capabilities != expected.capabilities:
        raise ValueError("target capabilities do not match the frozen Agent discovery")
    if target.concurrency_limit > config.capabilities.max_concurrency:
        raise ValueError("target concurrency exceeds the discovered Agent capacity")
    if target.external_version != config.capabilities.agent_version:
        raise ValueError("target version does not match the frozen Agent discovery")
    if set(target.secret_refs) - {"token"}:
        raise ValueError("Agent protocol accepts only the token secret reference")
    return config
