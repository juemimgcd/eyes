from uuid import UUID

from fastapi import APIRouter, Request

from eyes.contracts.work import ExecutionSubmission
from eyes.server.api.dependencies import Database, RunnerIdentity, ShortRunner, transaction
from eyes.server.domain import DomainError, public_record, scoped
from eyes.server.result_queue.service import prepare
from eyes.server.storage.models import ResultSubmission

router = APIRouter(prefix="/v1", tags=["execution-results"])


@router.post("/work/{work_id}/result-submissions")
def submit(work_id: UUID, body: ExecutionSubmission, request: Request, runner: ShortRunner):
    if request.app.state.settings.rabbitmq_url is None:
        raise DomainError(503, "result_queue_disabled", "configure EYES_RABBITMQ_URL")
    with transaction(request) as session:
        receipt = prepare(session, runner, work_id, body)
    if receipt["status"] == "pending":
        request.app.state.result_publisher.publish(receipt["id"], body.model_dump_json().encode())
    return {"schema_version": "1.0", **receipt}


@router.get("/result-submissions/{receipt_id}")
def status(receipt_id: UUID, session: Database, runner: RunnerIdentity):
    receipt = scoped(session, ResultSubmission, receipt_id, runner.project_id)
    if receipt.credential_id != runner.id:
        raise DomainError(403, "submission_scope", "result belongs to another Runner credential")
    return {"schema_version": "1.0", **public_record(receipt)}
