from uuid import UUID

from fastapi import APIRouter

from eyes.contracts.discovery import DiscoveryCreate, DiscoveryResult
from eyes.server.api.dependencies import Database, Manager, RunnerIdentity
from eyes.server.discovery import service
from eyes.server.domain import scoped
from eyes.server.storage.models import AgentDiscovery

router = APIRouter(prefix="/v1", tags=["discovery"])


@router.post("/agent-discoveries", status_code=201)
def create(body: DiscoveryCreate, session: Database, user: Manager):
    return service.create(session, user, body)


@router.get("/agent-discoveries/{check_id}")
def detail(check_id: UUID, session: Database, user: Manager):
    return service.view(scoped(session, AgentDiscovery, check_id, user.project_id))


@router.get("/runners/{runner_id}/discovery")
def pending(runner_id: UUID, session: Database, user: RunnerIdentity):
    return service.pending(session, user, runner_id)


@router.post("/runners/{runner_id}/discovery/{check_id}")
def finish(
    runner_id: UUID, check_id: UUID, body: DiscoveryResult, session: Database, user: RunnerIdentity
):
    return service.finish(session, user, runner_id, check_id, body)
