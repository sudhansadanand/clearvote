"""Operator sign-in. Separate cookie and lifetime from voter sessions."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..domain import auth
from . import deps

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@router.post("/login")
def login(request: Request, body: LoginRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = auth.verify_operator(conn, body.username, body.password)
        session = auth.create_operator_session(conn, operator["operator_id"], svc.config)
    payload = {"status": "SIGNED_IN", "username": operator["username"],
               "role": operator["role"], "csrf_token": session["csrf_token"]}
    response = JSONResponse(payload)
    deps.set_operator_cookie(response, session["session_id"], svc.config, request)
    return response


@router.post("/logout")
def logout(request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        auth.end_operator_session(conn, deps.operator_session_id(request))
    response = JSONResponse({"status": "SIGNED_OUT"})
    response.delete_cookie(deps.OPERATOR_COOKIE, path=deps.cookie_path(request))
    return response


@router.get("/me")
def me(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        operator = deps.current_operator(conn, request)
        if operator is None:
            return {"signed_in": False}
        return {"signed_in": True, "username": operator["username"],
                "role": operator["role"], "csrf_token": operator["csrf_token"]}
