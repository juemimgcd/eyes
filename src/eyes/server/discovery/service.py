from datetime import timedelta

from sqlalchemy import select

from eyes.agent_protocol.target import validate_target
from eyes.contracts.target import http_origin
from eyes.server.domain import DomainError, public_record, scoped
from eyes.server.scheduling.service import authorized_runner
from eyes.server.storage.models import AgentDiscovery, Credential, Runner, now


def view(item):
    return {
        **public_record(item),
        "status": ("failed" if item.result.get("error") else "completed")
        if item.result is not None
        else ("expired" if item.expires_at <= now() else "pending"),
    }


def create(session, user, request):
    runner = session.scalar(
        select(Runner)
        .join(Credential)
        .where(
            Runner.id == request.runner_id,
            Credential.project_id == user.project_id,
            Credential.revoked.is_(False),
        )
        .with_for_update(of=Runner)
    )
    if runner is None:
        raise DomainError(404, "runner_not_found", "Runner is unavailable in this project")
    if http_origin(request.base_url) not in runner.capabilities.get("http_origins", []):
        raise DomainError(422, "origin_not_allowed", "Agent origin is not allowed by this Runner")
    previous = session.scalar(select(AgentDiscovery).where(AgentDiscovery.runner_id == runner.id))
    if previous is not None:
        if previous.result is None and previous.expires_at > now():
            raise DomainError(
                409, "discovery_pending", "Runner already has a pending connection check"
            )
        session.delete(previous)
        session.flush()
    item = AgentDiscovery(
        project_id=user.project_id,
        runner_id=runner.id,
        request=request.model_dump(mode="json"),
        expires_at=now() + timedelta(minutes=2),
    )
    session.add(item)
    session.flush()
    return view(item)


def pending(session, user, runner_id):
    authorized_runner(session, user, runner_id)
    item = session.scalar(
        select(AgentDiscovery).where(
            AgentDiscovery.runner_id == runner_id,
            AgentDiscovery.result.is_(None),
            AgentDiscovery.expires_at > now(),
        )
    )
    return {"check": view(item) if item else None}


def finish(session, user, runner_id, check_id, result):
    authorized_runner(session, user, runner_id)
    item = scoped(session, AgentDiscovery, check_id, user.project_id)
    if item.runner_id != runner_id:
        raise DomainError(403, "runner_identity", "connection check belongs to another Runner")
    session.refresh(item, with_for_update=True)
    value = result.model_dump(mode="json")
    if item.result is not None:
        if item.result != value:
            raise DomainError(
                409, "discovery_result_conflict", "connection check already has a result"
            )
        return view(item)
    if item.expires_at <= now():
        raise DomainError(
            409, "discovery_expired", "connection check expired; request another check"
        )
    if result.target is not None:
        try:
            config = validate_target(result.target)
        except ValueError as error:
            raise DomainError(422, "discovery_invalid_target", str(error)) from error
        expected = item.request
        refs = {"token": "env:" + expected["credential_env"]} if expected["credential_env"] else {}
        if (
            config.base_url != expected["base_url"]
            or result.target.name != expected["name"]
            or result.target.secret_refs != refs
            or (
                expected["concurrency"] is not None
                and result.target.concurrency_limit != expected["concurrency"]
            )
        ):
            raise DomainError(
                422, "discovery_mismatch", "result does not match the connection check"
            )
    item.result = value
    return view(item)
