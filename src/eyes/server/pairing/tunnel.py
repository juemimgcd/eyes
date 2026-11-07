"""Gateway authorization reuses control-plane project and target ownership."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from eyes.server.api.dependencies import Database, Manager, RunnerIdentity, roles
from eyes.server.domain import DomainError, scoped
from eyes.server.storage.models import AgentPairing, Credential, now

router = APIRouter(prefix="/v1/tunnel", tags=["tunnel"])
TunnelIdentity = Annotated[Credential, Depends(roles("tunnel"))]


@router.post("/session")
def session_status(session: Database, user: TunnelIdentity):
    item = session.scalar(
        select(AgentPairing).where(AgentPairing.credential_id == user.id).with_for_update()
    )
    # Revocation must also terminate established connections on the next gateway check.
    if item is None or item.revoked:
        raise DomainError(401, "tunnel_revoked", "tunnel connection is unavailable")
    item.last_seen_at = now()
    return {"id": item.id, "target_version_id": item.target_version_id}


@router.post("/authorize/{pairing_id}")
def authorize(pairing_id: UUID, session: Database, user: RunnerIdentity):
    item = scoped(session, AgentPairing, pairing_id, user.project_id)
    credential = session.get(Credential, item.credential_id) if item.credential_id else None
    if item.revoked or credential is None or credential.revoked or credential.role != "tunnel":
        raise DomainError(401, "tunnel_revoked", "tunnel connection is unavailable")
    if "execute" not in user.work_kinds or (
        user.target_ids and str(item.target_version_id) not in user.target_ids
    ):
        raise DomainError(403, "target_forbidden", "Runner is not authorized for this target")
    return {"id": item.id}


@router.get("/config")
def configuration(request: Request, user: Manager):
    return {"enabled": request.app.state.settings.tunnel_enabled}
