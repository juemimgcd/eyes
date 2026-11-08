import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select

from eyes.contracts.target import TargetCapabilities, TargetPublish
from eyes.server.catalog.service import publish
from eyes.server.domain import DomainError, digest
from eyes.server.storage.models import AgentPairing, Credential, ObservationSource, Runner, now


def view(session, item):
    credential = session.get(Credential, item.credential_id) if item.credential_id else None
    runner = session.scalar(select(Runner).where(Runner.credential_id == item.credential_id))
    last_seen = (
        item.last_seen_at
        if credential and credential.role == "tunnel"
        else (runner.last_seen_at if runner else None)
    )
    if item.revoked or (credential and credential.revoked):
        status = "revoked"
    elif credential:
        status = "online" if last_seen and last_seen > now() - timedelta(seconds=30) else "offline"
    else:
        status = "expired" if item.expires_at <= now() else "pending"
    return {
        "id": item.id,
        "name": item.name,
        "purpose": item.purpose,
        "observation_source_id": item.observation_source_id,
        "expires_at": item.expires_at,
        "status": status,
        "target_version_id": item.target_version_id,
        "runner_id": runner.id if runner else None,
        "last_seen_at": last_seen,
        "transport": "tunnel" if credential and credential.role == "tunnel" else "runner",
    }


def create(session, user, request):
    code = secrets.token_urlsafe(24)
    item = AgentPairing(
        project_id=user.project_id,
        name=request.name,
        purpose=request.purpose,
        code_digest=hashlib.sha256(code.encode()).hexdigest(),
        expires_at=now() + timedelta(minutes=10),
    )
    session.add(item)
    session.flush()
    return {**view(session, item), "code": code}


def redeem(session, request, gateway_url="http://gateway:8090"):
    item = session.scalar(
        select(AgentPairing)
        .where(AgentPairing.code_digest == hashlib.sha256(request.code.encode()).hexdigest())
        .with_for_update()
    )
    # Unknown, expired and revoked codes cannot grant access to a project.
    if item is None or item.revoked:
        raise DomainError(401, "pairing_unavailable", "pairing code is invalid or revoked")
    # Observation-only pairings must not be redeemed into a task-executing Runner.
    if item.purpose == "observe" and request.adapter != "tunnel":
        raise DomainError(
            422, "observation_connector_required", "use the unified observation connector"
        )
    # Evaluation needs a callable endpoint; observation-only connections do not.
    if item.purpose == "evaluate" and request.adapter == "tunnel" and request.url is None:
        raise DomainError(
            422, "task_endpoint_required", "evaluation requires an Agent HTTP endpoint"
        )
    # Existing Python/HTTP redemption fingerprints predate the tunnel-only fields.
    fingerprint = digest(
        request.model_dump(
            mode="json",
            exclude={"protocol", "observation_token"} if request.adapter != "tunnel" else set(),
        )
    )
    if item.credential_id:
        credential = session.get(Credential, item.credential_id)
        # Replay only the exact redemption, using the secret already persisted by this connector.
        if credential.revoked or item.redemption_digest != fingerprint:
            raise DomainError(409, "pairing_used", "pairing code has already been used")
        return view(session, item)
    if item.expires_at <= now():
        raise DomainError(410, "pairing_expired", "generate a new pairing code")
    config = (
        {"agent": "connected"}
        if request.adapter == "python"
        else {
            "url": request.url,
            "request_mode": "input",
            "response_mode": "output",
            "output_pointer": request.output_pointer,
        }
    )
    if request.adapter == "http" and request.secret_refs:
        config["secret_headers"] = [{"name": "Authorization", "secret_ref": "agent_auth"}]
    if request.adapter == "tunnel":
        config = {
            "url": f"{gateway_url.rstrip('/')}/invoke/{item.id}",
            "request_mode": "input",
            "response_mode": "output",
            "output_pointer": request.output_pointer,
            "tunnel": True,
            "protocol": request.protocol,
            "request_timeout_seconds": 900,
        }
        source = ObservationSource(
            project_id=item.project_id,
            name=item.name,
            token_digest=hashlib.sha256(request.observation_token.encode()).hexdigest(),
        )
        session.add(source)
        session.flush()
        item.observation_source_id = source.id
    target = None
    if item.purpose == "evaluate":
        target = publish(
            session,
            item.project_id,
            TargetPublish(
                name=item.name,
                capabilities=TargetCapabilities(
                    adapter="http" if request.adapter == "tunnel" else request.adapter,
                    session_isolation=False,
                    environment_isolation=False,
                    observation=["task", "sdk"] if request.adapter == "python" else ["task"],
                ),
                config=config,
                secret_refs=request.secret_refs,
            ),
        )
    credential = Credential(
        project_id=item.project_id,
        role="tunnel" if request.adapter == "tunnel" else "runner",
        token_digest=hashlib.sha256(request.token.encode()).hexdigest(),
        target_ids=[str(target.id)] if target else [],
        work_kinds=[] if request.adapter == "tunnel" else ["execute"],
    )
    session.add(credential)
    session.flush()
    item.credential_id = credential.id
    item.target_version_id = target.id if target else None
    item.redemption_digest = fingerprint
    return view(session, item)


def revoke(session, item):
    session.refresh(item, with_for_update=True)
    item.revoked = True
    if item.observation_source_id:
        session.get(ObservationSource, item.observation_source_id).revoked = True
    if item.credential_id:
        session.get(Credential, item.credential_id).revoked = True
    return view(session, item)
