import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select

from eyes.contracts.target import TargetCapabilities, TargetPublish
from eyes.server.catalog.service import publish
from eyes.server.domain import DomainError, digest
from eyes.server.storage.models import AgentPairing, Credential, Runner, now


def view(session, item):
    credential = session.get(Credential, item.credential_id) if item.credential_id else None
    runner = session.scalar(select(Runner).where(Runner.credential_id == item.credential_id))
    if item.revoked or (credential and credential.revoked):
        status = "revoked"
    elif credential:
        status = (
            "online"
            if runner and runner.last_seen_at > now() - timedelta(seconds=30)
            else "offline"
        )
    else:
        status = "expired" if item.expires_at <= now() else "pending"
    return {
        "id": item.id,
        "name": item.name,
        "expires_at": item.expires_at,
        "status": status,
        "target_version_id": item.target_version_id,
        "runner_id": runner.id if runner else None,
        "last_seen_at": runner.last_seen_at if runner else None,
    }


def create(session, user, request):
    code = secrets.token_urlsafe(24)
    item = AgentPairing(
        project_id=user.project_id,
        name=request.name,
        code_digest=hashlib.sha256(code.encode()).hexdigest(),
        expires_at=now() + timedelta(minutes=10),
    )
    session.add(item)
    session.flush()
    return {**view(session, item), "code": code}


def redeem(session, request):
    item = session.scalar(
        select(AgentPairing)
        .where(AgentPairing.code_digest == hashlib.sha256(request.code.encode()).hexdigest())
        .with_for_update()
    )
    # Unknown, expired and revoked codes cannot grant access to a project.
    if item is None or item.revoked:
        raise DomainError(401, "pairing_unavailable", "pairing code is invalid or revoked")
    fingerprint = digest(request.model_dump(mode="json"))
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
    target = publish(
        session,
        item.project_id,
        TargetPublish(
            name=item.name,
            capabilities=TargetCapabilities(
                adapter=request.adapter,
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
        role="runner",
        token_digest=hashlib.sha256(request.token.encode()).hexdigest(),
        target_ids=[str(target.id)],
        work_kinds=["execute"],
    )
    session.add(credential)
    session.flush()
    item.credential_id, item.target_version_id = credential.id, target.id
    item.redemption_digest = fingerprint
    return view(session, item)


def revoke(session, item):
    session.refresh(item, with_for_update=True)
    item.revoked = True
    if item.credential_id:
        session.get(Credential, item.credential_id).revoked = True
    return view(session, item)
