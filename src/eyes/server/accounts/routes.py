from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from eyes.server.accounts import service
from eyes.server.accounts.security import COOKIE_NAME, browser_write
from eyes.server.api.dependencies import Database, Manager, transaction
from eyes.server.domain import DomainError
from eyes.server.storage.models import Account, AuthRateLimit, now

router = APIRouter(prefix="/v1/auth", tags=["accounts"])
Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=40,
        pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$",
    ),
]


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Username
    password: SecretStr = Field(min_length=1, max_length=128)


class Register(Login):
    password: SecretStr = Field(min_length=12, max_length=128)


def auth_limit(request: Request):
    browser_write(request)
    # Count before password hashing, across workers and failed transactions.
    # One row per direct peer, with old buckets deleted, bounds persistent state.
    import hashlib
    from datetime import timedelta

    key = hashlib.sha256(request.client.host.encode()).hexdigest()
    cutoff = now() - timedelta(minutes=5)
    with transaction(request) as session:
        session.execute(delete(AuthRateLimit).where(AuthRateLimit.started_at < cutoff))
        statement = insert(AuthRateLimit).values(key=key, started_at=now(), attempts=1)
        attempts = session.scalar(
            statement.on_conflict_do_update(
                index_elements=[AuthRateLimit.key],
                set_={"attempts": AuthRateLimit.attempts + 1},
            ).returning(AuthRateLimit.attempts)
        )
    if attempts > 30:
        raise DomainError(429, "auth_rate_limited", "登录或注册尝试过于频繁，请 5 分钟后再试。")


def authenticated(response, request, session, account):
    settings = request.app.state.settings
    token, credential = service.start_session(session, account, settings.session_hours)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"account": service.view(account), "expires_at": credential.expires_at}


@router.post("/register", status_code=201, dependencies=[Depends(auth_limit)])
def register(body: Register, request: Request, response: Response, session: Database):
    account = service.register(session, body.username, body.password.get_secret_value())
    return authenticated(response, request, session, account)


@router.post("/login", dependencies=[Depends(auth_limit)])
def login(body: Login, request: Request, response: Response, session: Database):
    account = service.login(session, body.username, body.password.get_secret_value())
    return authenticated(response, request, session, account)


@router.get("/me")
def me(session: Database, user: Manager, response: Response):
    if user.account_id is None:
        raise DomainError(401, "account_required", "请使用个人账号登录。")
    account = session.scalar(select(Account).where(Account.id == user.account_id))
    response.headers["Cache-Control"] = "no-store"
    return {"account": service.view(account), "expires_at": user.expires_at}


@router.post("/logout", status_code=204)
def logout(session: Database, user: Manager, response: Response):
    if user.account_id is None:
        raise DomainError(401, "account_required", "请使用个人账号登录。")
    user.revoked = True
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
