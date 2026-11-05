import logging
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from eyes.server.domain import DomainError, digest, public_record
from eyes.server.scheduling import service as scheduling
from eyes.server.storage.database import maintenance_lock
from eyes.server.storage.models import Credential, ResultSubmission, now

logger = logging.getLogger(__name__)


def prepare(session, runner, work_id, submission):
    content_digest = digest(submission.model_dump(mode="json"))
    receipt_id = uuid5(NAMESPACE_URL, f"eyes:result:{runner.id}:{work_id}:{content_digest}")
    work, _ = scheduling.lease(
        session,
        runner,
        work_id,
        submission,
        completion=digest(submission.result.model_dump(mode="json")),
        read_only=True,
    )
    if work.kind != "execute":
        raise DomainError(409, "work_kind", "only execution results use this queue")
    session.execute(
        insert(ResultSubmission)
        .values(
            id=receipt_id,
            created_at=now(),
            project_id=runner.project_id,
            credential_id=runner.id,
            work_id=work_id,
            digest=content_digest,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return public_record(session.get(ResultSubmission, receipt_id))


def consume(sessions, receipt_id, submission, store, settings):
    with sessions.begin() as session:
        maintenance_lock(session)
        record = session.scalar(
            select(ResultSubmission).where(ResultSubmission.id == receipt_id).with_for_update()
        )
        if record is None or record.digest != digest(submission.model_dump(mode="json")):
            raise ValueError("broker result does not match its stored receipt")
        if record.status != "pending":
            return record.status
        try:
            with session.begin_nested():
                credential = session.get(Credential, record.credential_id)
                if credential.revoked or credential.role != "runner":
                    raise DomainError(403, "invalid_runner", "Runner credential revoked or changed")
                scheduling.finish(
                    session,
                    credential,
                    record.work_id,
                    submission,
                    submission.result,
                    store,
                    settings,
                )
        except DomainError as exc:
            if exc.status >= 500:
                raise
            # This rejects delivery/application of a result, not an Agent's failed
            # test outcome. A valid result.status='failed' follows normal persistence.
            logger.exception(
                "result_submission_rejected receipt=%s work=%s", receipt_id, record.work_id
            )
            record.status = "rejected"
            record.error = {
                "status": exc.status,
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        else:
            record.status = "persisted"
        record.completed_at = now()
        return record.status
