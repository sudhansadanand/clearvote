"""Shared request helpers: cookies, current voter, current operator, CSRF."""

from __future__ import annotations

from fastapi import Request

from ..domain import auth, credentials
from ..domain.errors import Unauthorized, ASSISTANCE

VOTER_COOKIE = "sgoa_voter"
OPERATOR_COOKIE = "sgoa_op"


def services(request: Request):
    return request.app.state.services


def config(request: Request):
    return request.app.state.services.config


def client_key(request: Request) -> str:
    """Rate-limit bucket. Never stored anywhere durable or in the audit trail."""
    return request.client.host if request.client else "unknown"


def base_path(request: Request) -> str:
    """"" when this meeting is served at the root, "/<event>" when mounted."""
    return (request.scope.get("root_path") or "").rstrip("/")


def cookie_path(request: Request) -> str:
    """Scope a session to one meeting.

    With several meetings served from one process, a cookie on "/" would make a
    session issued by one of them travel to all the others. The browser only
    sends a cookie to paths under its own, so scoping it to the event prefix
    keeps sessions, and the CSRF token inside them, from crossing over.
    """
    return base_path(request) + "/"


def set_voter_cookie(response, session_id: str, cfg, request: Request) -> None:
    response.set_cookie(
        VOTER_COOKIE, session_id,
        httponly=True, samesite="strict", secure=cfg.cookie_secure,
        max_age=cfg.voter_session_hours * 3600, path=cookie_path(request),
    )


def set_operator_cookie(response, session_id: str, cfg, request: Request) -> None:
    response.set_cookie(
        OPERATOR_COOKIE, session_id,
        httponly=True, samesite="strict", secure=cfg.cookie_secure,
        max_age=12 * 3600, path=cookie_path(request),
    )


def voter_session_id(request: Request) -> str | None:
    return request.cookies.get(VOTER_COOKIE)


def operator_session_id(request: Request) -> str | None:
    return request.cookies.get(OPERATOR_COOKIE)


def current_voter(conn, request: Request):
    return credentials.resolve_session(conn, voter_session_id(request))


def require_voter(conn, request: Request):
    row = current_voter(conn, request)
    if row is None:
        raise Unauthorized("Your voting session has ended.",
                           next_action="Please enter your code again, or " + ASSISTANCE.lower())
    return row


def current_operator(conn, request: Request):
    return auth.resolve_operator_session(conn, operator_session_id(request),
                                         request.app.state.services.config)


def require_operator(conn, request: Request, *roles: str):
    row = current_operator(conn, request)
    if row is None:
        raise Unauthorized("Please sign in to continue.")
    if roles:
        auth.require_role(row, *roles)
    return row


def enforce_csrf(conn, request: Request, supplied: str | None = None):
    """Every state-changing operator action must present the session's token."""
    session_row = current_operator(conn, request)
    token = supplied or request.headers.get("x-csrf-token")
    auth.check_csrf(session_row, token)
    return session_row
