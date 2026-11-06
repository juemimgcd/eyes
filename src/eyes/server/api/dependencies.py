import hashlib
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session

from eyes.server.accounts.security import COOKIE_NAME, browser_write
from eyes.server.domain import DomainError
from eyes.server.storage.models import Credential, now

bearer = HTTPBearer(auto_error=False)


@contextmanager
def transaction(request: Request):
    try:
        with request.app.state.sessions() as session, session.begin():
            # Acquire only when a database transaction actually starts. Requests
            # missing a bearer token can still fail with 401 while DB is offline.
            event.listen(
                session,
                "after_begin",
                lambda session, transaction, connection: connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock_shared(74000)"
                ),
            )
            yield session
    except PoolTimeout as exc:
        raise DomainError(503, "database_busy", "database connection budget is exhausted") from exc
    except OperationalError as exc:
        raise DomainError(503, "database_unavailable", "database is not available") from exc


def db_session(request: Request):
    with transaction(request) as session:
        yield session


Database = Annotated[Session, Depends(db_session, scope="function")]


def identity(
    session: Database,
    supplied: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    request: Request,
):
    token = supplied.credentials if supplied else request.cookies.get(COOKIE_NAME)
    if not token:
        raise DomainError(401, "authentication_required", "请登录后继续。")
    if supplied is None and request.method not in {"GET", "HEAD", "OPTIONS"}:
        browser_write(request)
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    credential = session.scalar(
        select(Credential).where(
            Credential.token_digest == token_digest, Credential.revoked.is_(False)
        )
    )
    if (
        credential is None
        or (credential.expires_at is not None and credential.expires_at <= now())
        or (supplied is None and credential.account_id is None)
    ):
        raise DomainError(401, "invalid_token", "token is invalid or revoked")
    expected_account = request.headers.get("x-eyes-account")
    if expected_account and expected_account != str(credential.account_id):
        raise DomainError(401, "account_changed", "账号已切换，请重新登录。")
    return credential


def roles(*allowed):
    def authorize(credential: Annotated[Credential, Depends(identity)]):
        if credential.role not in allowed:
            raise DomainError(
                403, "permission_denied", "credential does not authorize this operation"
            )
        return credential

    return authorize


Reader = Annotated[Credential, Depends(roles("read", "manage"))]
Manager = Annotated[Credential, Depends(roles("manage"))]
RunnerIdentity = Annotated[Credential, Depends(roles("runner"))]
ArtifactReader = Annotated[Credential, Depends(roles("read", "manage", "runner"))]


def short_identity(request, supplied, allowed):
    with transaction(request) as session:
        credential = identity(session, supplied, request)
        if credential.role not in allowed:
            raise DomainError(
                403, "permission_denied", "credential does not authorize this operation"
            )
        session.expunge(credential)
        return credential


def short_reader(
    request: Request,
    supplied: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    return short_identity(request, supplied, {"read", "manage"})


def short_runner(
    request: Request,
    supplied: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    return short_identity(request, supplied, {"runner"})


ShortReader = Annotated[Credential, Depends(short_reader)]
ShortRunner = Annotated[Credential, Depends(short_runner)]
