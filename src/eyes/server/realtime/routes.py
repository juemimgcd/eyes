import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from eyes.server.api.dependencies import Database, Reader
from eyes.server.domain import DomainError, public_record, scoped
from eyes.server.realtime.hub import sse
from eyes.server.storage.models import (
    Attempt,
    CacheEpoch,
    Credential,
    Event,
    EventReceipt,
    ObservedEvent,
    ObservedRun,
    ResourceRevision,
)

router = APIRouter(prefix="/v1/realtime", tags=["realtime"])


@router.get("/stream")
async def updates(request: Request, user: Reader):
    hub = request.app.state.updates
    settings = request.app.state.settings
    credential_id, project_id, token_digest = user.id, user.project_id, user.token_digest
    queue = hub.subscribe(project_id)

    def authorized():
        with request.app.state.sessions() as session:
            credential = session.get(Credential, credential_id)
            return bool(
                credential
                and not credential.revoked
                and credential.role in {"read", "manage"}
                and credential.project_id == project_id
                and credential.token_digest == token_digest
            )

    async def release():
        hub.unsubscribe(queue)

    async def stream():
        clock = asyncio.get_running_loop().time
        auth_due = clock() + settings.realtime_reauth_seconds
        resync_due = clock() + settings.realtime_resync_seconds
        try:
            yield sse(
                {
                    "event": "resync",
                    "reason": "connected",
                    "mode": "live" if hub.connected else "polling",
                }
            )
            while True:
                if clock() >= auth_due:
                    try:
                        allowed = await asyncio.to_thread(authorized)
                    except SQLAlchemyError:
                        yield sse({"event": "unavailable", "reason": "authentication_unavailable"})
                        return
                    if not allowed:
                        yield sse({"event": "auth_error", "reason": "credential_revoked"})
                        return
                    auth_due = clock() + settings.realtime_reauth_seconds
                if clock() >= resync_due:
                    # Pub/Sub can omit a message without disconnecting. Periodic
                    # resync also covers that case and an unconfigured Redis.
                    yield sse(
                        {
                            "event": "resync",
                            "reason": "periodic",
                            "mode": "live" if hub.connected else "polling",
                        }
                    )
                    resync_due = clock() + settings.realtime_resync_seconds
                timeout = max(0.01, min(auth_due, resync_due) - clock())
                try:
                    value = await asyncio.wait_for(queue.get(), timeout)
                except TimeoutError:
                    continue
                yield sse({**value, "mode": "live" if hub.connected else "polling"})
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        background=BackgroundTask(release),
    )


@router.get("/{kind}/{resource_id}/events")
def received_events(
    kind: Literal["attempt", "observed_run"],
    resource_id: UUID,
    session: Database,
    user: Reader,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    epoch: UUID | None = None,
):
    parent = scoped(
        session, Attempt if kind == "attempt" else ObservedRun, resource_id, user.project_id
    )
    if kind == "attempt" and parent.evidence_expired_at:
        raise DomainError(410, "evidence_expired", "event content has expired")
    current_epoch = session.scalar(select(CacheEpoch.epoch).where(CacheEpoch.id == 1))
    reset = epoch is not None and epoch != current_epoch
    if reset:
        after = 0
    model = Event if kind == "attempt" else ObservedEvent
    rows = session.execute(
        select(model, EventReceipt.receive_version)
        .join(
            EventReceipt,
            (EventReceipt.event_id == model.id) & (EventReceipt.kind == kind),
        )
        .where(
            EventReceipt.project_id == user.project_id,
            EventReceipt.resource_id == resource_id,
            EventReceipt.receive_version > after,
        )
        .order_by(EventReceipt.receive_version)
        .limit(limit + 1)
    ).all()
    items = rows[:limit]
    revision = session.get(ResourceRevision, (user.project_id, kind, resource_id))
    return {
        "schema_version": "1.0",
        "epoch": current_epoch,
        "reset": reset,
        "items": [{**public_record(item), "receive_version": version} for item, version in items],
        # Do not advance to resource.version: a concurrent commit could update it
        # after the page query, hiding events not read by this response.
        "next_cursor": items[-1][1] if items else after,
        "has_more": len(rows) > limit,
        "resource_version": revision.version if revision else 0,
    }
