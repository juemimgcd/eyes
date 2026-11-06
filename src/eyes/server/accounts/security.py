"""Browser session boundary, shared by account and project endpoints."""

from fastapi import Request

from eyes.server.domain import DomainError

COOKIE_NAME = "eyes_session"


def browser_write(request: Request):
    # Cross-site forms cannot set this header; cross-origin fetch requires a
    # preflight, and this API does not grant CORS access to other origins.
    if request.headers.get("x-eyes-client") != "console":
        raise DomainError(403, "browser_request_required", "请从 Eyes 页面提交此操作。")
