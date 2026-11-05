from collections import defaultdict

from sqlalchemy import event, inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from eyes.server.storage import models as m


class ControlSession(Session):
    """Business writes and their update notifications share one commit."""


@event.listens_for(ControlSession, "after_flush")
def remember_changes(session, context):
    changes = session.info.setdefault("resource_changes", {})
    receipts = session.info.setdefault("new_event_receipts", {})
    for item in [*session.new, *session.dirty, *session.deleted]:
        if isinstance(
            item,
            (
                m.ResourceRevision,
                m.NotificationOutbox,
                m.EventReceipt,
                m.CacheEpoch,
                m.EventSubmission,
                m.ResultSubmission,
            ),
        ):
            continue
        if item in session.dirty and not session.is_modified(item, include_collections=False):
            continue
        if (
            isinstance(item, m.WorkItem)
            and item not in session.new
            and not any(
                inspect(item).attrs[name].history.has_changes() for name in ("status", "reserved")
            )
        ):
            # Extending a lease changes no experiment result or evidence view.
            continue
        changes[type(item), item.id] = item
        if item in session.new and isinstance(item, (m.Event, m.ObservedEvent)):
            receipts[type(item), item.id] = item


@event.listens_for(ControlSession, "after_rollback")
def discard_changes(session):
    session.info.pop("resource_changes", None)
    session.info.pop("new_event_receipts", None)


@event.listens_for(ControlSession, "before_commit")
def publish_changes(session):
    session.flush()
    changes = session.info.pop("resource_changes", {})
    new_events = session.info.pop("new_event_receipts", {})
    resources = set()
    event_groups = defaultdict(list)

    def experiment(experiment_id):
        value = session.get(m.Experiment, experiment_id)
        resources.add((value.project_id, "experiment", value.id))
        if value.batch_id:
            resources.add((value.project_id, "batch", value.batch_id))
        return value.project_id

    def attempt(attempt_id):
        value = session.get(m.Attempt, attempt_id)
        resources.add((value.project_id, "attempt", value.id))
        experiment(session.get(m.CaseRun, value.case_run_id).experiment_id)
        return value.project_id

    # Resolve relationships before acquiring revision locks. No business row is
    # changed after that point, so writers lock revisions in one stable order.
    for item in changes.values():
        if isinstance(
            item, (m.Event, m.Artifact, m.Manifest, m.ScoreRun, m.EvidenceWait, m.ResolutionRecord)
        ):
            project = attempt(item.attempt_id)
        elif isinstance(item, m.Attempt):
            project = attempt(item.id)
        elif isinstance(item, m.CaseRun):
            project = experiment(item.experiment_id)
        elif isinstance(item, m.WorkItem):
            project = experiment(item.experiment_id)
        elif isinstance(item, m.Experiment):
            project = experiment(item.id)
        elif isinstance(item, m.ExperimentBatch):
            project = item.project_id
            resources.add((project, "batch", item.id))
        elif isinstance(item, (m.ObservedEvent, m.ObservedRun)):
            project = item.project_id
            resources.add(
                (
                    project,
                    "observed_run",
                    item.run_id if isinstance(item, m.ObservedEvent) else item.id,
                )
            )
        elif isinstance(item, m.Runner):
            project = session.get(m.Credential, item.credential_id).project_id
        elif isinstance(item, m.CaseVersion):
            project = session.get(m.DatasetVersion, item.dataset_id).project_id
            resources.add((project, "catalog", project))
        elif isinstance(item, m.Project):
            project = item.id
        else:
            project = item.project_id
            if isinstance(item, m.ComparisonReport):
                resources.add((project, "comparison", item.id))
            else:
                resources.add((project, "catalog", project))
        resources.add((project, "operations", project))
    for item in new_events.values():
        kind = "attempt" if isinstance(item, m.Event) else "observed_run"
        resource_id = item.attempt_id if isinstance(item, m.Event) else item.run_id
        event_groups[item.project_id, kind, resource_id].append(item.id)

    current = m.now()
    for project_id, kind, resource_id in sorted(
        resources, key=lambda value: tuple(map(str, value))
    ):
        event_ids = event_groups[project_id, kind, resource_id]
        count = max(1, len(event_ids))
        key = {"project_id": project_id, "kind": kind, "resource_id": resource_id}
        version = session.scalar(
            insert(m.ResourceRevision)
            .values(
                **key,
                version=count,
                updated_at=current,
            )
            .on_conflict_do_update(
                index_elements=["project_id", "kind", "resource_id"],
                set_={"version": m.ResourceRevision.version + count, "updated_at": current},
            )
            .returning(m.ResourceRevision.version)
        )
        for index, event_id in enumerate(event_ids):
            session.execute(
                insert(m.EventReceipt).values(
                    **key,
                    event_id=event_id,
                    receive_version=version - len(event_ids) + index + 1,
                )
            )
        session.execute(
            insert(m.NotificationOutbox)
            .values(
                **key,
                version=version,
                created_at=current,
                updated_at=current,
                available_at=current,
                failures=0,
            )
            .on_conflict_do_update(
                index_elements=["project_id", "kind", "resource_id"],
                # Keep a publisher's lease, token and retry schedule. Its conditional
                # acknowledgement cannot delete a newer coalesced notification.
                set_={"version": version, "updated_at": current},
            )
        )
