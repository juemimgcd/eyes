import hashlib
import secrets
from datetime import timedelta

from pwdlib import PasswordHash
from sqlalchemy import select

from eyes.server.domain import DomainError
from eyes.server.storage.models import Account, Credential, Project, now

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def register(session, username, password):
    if session.scalar(select(Account.id).where(Account.username == username)) is not None:
        raise DomainError(409, "username_taken", "该用户名已被使用，请换一个用户名。")
    project = Project(name=f"{username} 的工作空间")
    session.add(project)
    session.flush()
    account = Account(
        username=username, password_hash=passwords.hash(password), project_id=project.id
    )
    session.add(account)
    session.flush()
    return account


def login(session, username, password):
    account = session.scalar(select(Account).where(Account.username == username))
    valid = passwords.verify(password, account.password_hash if account else DUMMY_HASH)
    if not valid or account is None:
        raise DomainError(401, "invalid_login", "用户名或密码不正确。")
    return account


def start_session(session, account, lifetime_hours):
    token = secrets.token_urlsafe(32)
    credential = Credential(
        project_id=account.project_id,
        account_id=account.id,
        expires_at=now() + timedelta(hours=lifetime_hours),
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        role="manage",
    )
    session.add(credential)
    session.flush()
    return token, credential


def view(account):
    return {
        "id": str(account.id),
        "username": account.username,
        "project_id": str(account.project_id),
    }
