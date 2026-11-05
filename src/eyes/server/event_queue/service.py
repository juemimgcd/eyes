import logging
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from eyes.server.domain import DomainError, digest, public_record
from eyes.server.evidence.service import authorize_attempt, ingest
from eyes.server.storage.database import maintenance_lock
from eyes.server.storage.models import Credential, EventSubmission, now

logger = logging.getLogger(__name__)


def prepare(session, runner, batch):
    content_digest = digest(batch.model_dump(mode="json"))
    submission_id = uuid5(NAMESPACE_URL, f"eyes:events:{runner.id}:{content_digest}")
    # Authorization precedes broker I/O; mutable rights are checked again when consumed.
    for attempt_id in sorted({event.attempt_id for event in batch.events}, key=str):
        authorize_attempt(session, runner, attempt_id)
    session.execute(
        insert(EventSubmission)
        .values(
            id=submission_id,
            created_at=now(),
            credential_id=runner.id,
            project_id=runner.project_id,
            digest=content_digest,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return public_record(session.get(EventSubmission, submission_id))


def consume(sessions, submission_id, batch):
    with sessions.begin() as session:
        maintenance_lock(session)
        record = session.scalar(
            select(EventSubmission).where(EventSubmission.id == submission_id).with_for_update()
        )
        # Broker payload is a separate trust boundary. Reject tampering or an
        # orphaned message after database restore without altering another receipt.
        if record is None or record.digest != digest(batch.model_dump(mode="json")):
            raise ValueError("broker message does not match a stored event submission")
        if record.status != "pending":
            return record.status
        try:
            with session.begin_nested():
                credential = session.get(Credential, record.credential_id)
                if credential.revoked or credential.role != "runner":
                    raise DomainError(403, "invalid_runner", "Runner credential revoked or changed")
                ingest(session, credential, batch)
        except DomainError as exc:
            if exc.status >= 500:
                raise
            logger.exception("event_submission_rejected submission=%s", submission_id)
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
