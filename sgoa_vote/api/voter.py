"""Voter API (work order §7).

Nothing here returns an apartment list, an attendee name, or anything else that
would let a voter's phone reveal who is in the room.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..domain import credentials, resolutions, voting
from ..domain.errors import DomainError, RateLimited, ValidationError
from . import deps

router = APIRouter(prefix="/api/v1", tags=["voter"])


class JoinRequest(BaseModel):
    code: str = Field(default="", max_length=32)


class Allocation(BaseModel):
    FOR: int = 0
    AGAINST: int = 0
    ABSTAIN: int = 0


class VoteRequest(BaseModel):
    client_submission_id: str | None = None
    resolution_version: int | None = None
    resolution_hash: str | None = None
    allocation: Allocation = Allocation()
    confirmed: bool = False


@router.post("/voter/join")
def join(request: Request, body: JoinRequest):
    svc = deps.services(request)
    cfg = svc.config
    client_key = deps.client_key(request)

    try:
        with svc.db.writer() as conn:
            result = credentials.authenticate(
                conn, svc.agm_key, body.code, cfg, client_key,
                current_session_id=deps.voter_session_id(request),
            )
            credentials.record_attempt(conn, client_key, True, cfg)
            state = voting.voter_state(conn, credentials.resolve_session(
                conn, result["session_id"]), cfg)
    except DomainError as exc:
        # The failed attempt has to survive the rollback that just discarded the
        # authentication transaction, or the rate limiter would never see it.
        if not isinstance(exc, RateLimited):
            with svc.db.writer() as conn:
                credentials.record_attempt(conn, client_key, False, cfg)
        raise

    payload = {"status": "JOINED", "entitlement_count": result["entitlement_count"],
               "resumed": result["resumed"], "state": state}
    response = JSONResponse(payload)
    deps.set_voter_cookie(response, result["session_id"], cfg)
    return response


@router.get("/voter/state")
def state(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        credential = deps.require_voter(conn, request)
        return voting.voter_state(conn, credential, svc.config)


@router.get("/resolutions/active")
def active_resolution(request: Request):
    """Wording, version and hash of whatever is open right now."""
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_voter(conn, request)
        row = resolutions.active(conn)
        if row is None:
            return {"open": False, "resolution": None}
        return {
            "open": True,
            "resolution": {
                "resolution_id": row["resolution_id"],
                "number": row["number"],
                "version": row["version"],
                "title": row["title"],
                "full_text": row["full_text"],
                "text_hash": row["text_hash"],
                "voting_rule": row["voting_rule"],
                "choices": list(voting.CHOICES),
            },
        }


@router.post("/resolutions/{ident}/vote/preview")
def preview(ident: str, request: Request, body: VoteRequest):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        credential = deps.require_voter(conn, request)
        return voting.preview(conn, credential, ident, body.allocation.model_dump(),
                              resolution_version=body.resolution_version,
                              resolution_hash=body.resolution_hash)


@router.post("/resolutions/{ident}/vote")
def cast_vote(ident: str, request: Request, body: VoteRequest):
    """Atomic, idempotent. The only path that writes a ballot row."""
    svc = deps.services(request)
    if not body.client_submission_id:
        raise ValidationError("Missing submission reference.",
                              next_action="Please reload the page and try again.")
    with svc.db.writer() as conn:
        credential = deps.require_voter(conn, request)
        return voting.submit(
            conn, credential, ident, body.allocation.model_dump(),
            client_submission_id=body.client_submission_id,
            resolution_version=body.resolution_version,
            resolution_hash=body.resolution_hash,
            confirmed=body.confirmed,
        )


@router.post("/voter/logout")
def logout(request: Request):
    """Ends the device session. The voting ledger is untouched."""
    svc = deps.services(request)
    with svc.db.writer() as conn:
        credentials.end_session(conn, deps.voter_session_id(request))
    response = JSONResponse({"status": "LOGGED_OUT"})
    response.delete_cookie(deps.VOTER_COOKIE, path="/")
    return response
