from uuid import UUID

from fastapi import APIRouter, Request

from eyes.contracts.evidence import EventBatch
from eyes.server.api.dependencies import Database, RunnerIdentity, ShortRunner, transaction
from eyes.server.domain import DomainError, public_record, scoped
from eyes.server.event_queue.service import prepare
from eyes.server.storage.models import EventSubmission

router = APIRouter(prefix="/v1/event-submissions", tags=["events"])


@router.post("")
def submit(body: EventBatch, request: Request, runner: ShortRunner):
    if request.app.state.settings.rabbitmq_url is None:
        raise DomainError(503, "event_queue_disabled", "configure EYES_RABBITMQ_URL")
    with transaction(request) as session:
        result = prepare(session, runner, body)
    submission_id = result["id"]
    if result["status"] == "pending":
        request.app.state.event_publisher.publish(submission_id, body.model_dump_json().encode())
    return {"schema_version": "1.0", **result}


@router.get("/{submission_id}")
def status(submission_id: UUID, session: Database, runner: RunnerIdentity):
    record = scoped(session, EventSubmission, submission_id, runner.project_id)
    if record.credential_id != runner.id:
        raise DomainError(
            403, "submission_scope", "submission belongs to another Runner credential"
        )
    return {"schema_version": "1.0", **public_record(record)}
