from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from eyes.contracts.pairing import PairingCreate, PairingRedeem
from eyes.server.api.dependencies import Database, Manager
from eyes.server.domain import DomainError, scoped
from eyes.server.pairing import service
from eyes.server.storage.models import AgentPairing

router = APIRouter(prefix="/v1/agent-pairings", tags=["pairing"])


@router.post("", status_code=201)
def create(body: PairingCreate, session: Database, user: Manager, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return service.create(session, user, body)


@router.post("/redeem")
def redeem(body: PairingRedeem, session: Database, response: Response, request: Request):
    response.headers["Cache-Control"] = "no-store"
    if body.adapter == "tunnel" and not request.app.state.settings.tunnel_enabled:
        raise DomainError(503, "tunnel_disabled", "tunnel deployment is not enabled")
    return service.redeem(session, body, request.app.state.settings.tunnel_gateway_url)


@router.get("")
def listing(session: Database, user: Manager):
    items = session.scalars(
        select(AgentPairing)
        .where(AgentPairing.project_id == user.project_id)
        .order_by(AgentPairing.created_at.desc())
        .limit(100)
    )
    return {"items": [service.view(session, item) for item in items]}


@router.get("/{pairing_id}")
def detail(pairing_id: UUID, session: Database, user: Manager):
    return service.view(session, scoped(session, AgentPairing, pairing_id, user.project_id))


@router.post("/{pairing_id}/revoke")
def revoke(pairing_id: UUID, session: Database, user: Manager):
    return service.revoke(session, scoped(session, AgentPairing, pairing_id, user.project_id))
