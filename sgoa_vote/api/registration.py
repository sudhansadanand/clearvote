"""Registration desk API (work order §7).

This is the only place identities are handled. It never reads ballots and it is
blocked from results while a resolution is open, same as everyone else.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..domain import credentials, entitlements
from ..domain.errors import Conflict, NotFound
from . import deps

router = APIRouter(prefix="/api/v1/registration", tags=["registration"])

ROLES = ("REGISTRATION", "ADMIN")


class RepresentationRequest(BaseModel):
    apartment_id: str
    attendee_name: str | None = None
    attendee_id: str | None = None
    rep_type: str = "OWN"
    proxy_ref: str | None = None
    override_reason: str | None = None


class RevokeRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class IssueRequest(BaseModel):
    attendee_id: str | None = None
    attendee_name: str | None = None


class ResetRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.get("/apartments")
def apartments(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *ROLES)
        return {
            "apartments": entitlements.apartment_register(conn),
            "attendees": entitlements.attendee_register(conn),
            "summary": entitlements.representation_summary(conn),
        }


@router.post("/representations")
def assign(request: Request, body: RepresentationRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *ROLES)
        deps.enforce_csrf(conn, request)

        attendee_id = body.attendee_id
        if not attendee_id:
            attendee_id = entitlements.ensure_attendee(conn, body.attendee_name or "")

        result = entitlements.assign_representation(
            conn, body.apartment_id.strip().upper(), attendee_id, body.rep_type.upper(),
            svc.config, proxy_ref=body.proxy_ref, operator_id=operator["operator_id"],
            override_reason=body.override_reason,
        )
        # Keep a credential already in hand in step with the new entitlement total.
        cred = credentials.active_credential_for_attendee(conn, attendee_id)
        if cred is not None:
            entitlements.add_credential_to_open_resolutions(
                conn, cred["credential_id"], result["entitlement_count"])
        result["attendee_id"] = attendee_id
        return result


@router.post("/representations/{representation_id}/revoke")
def revoke(representation_id: str, request: Request, body: RevokeRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *ROLES)
        deps.enforce_csrf(conn, request)
        return entitlements.revoke_representation(
            conn, representation_id, operator["operator_id"], body.reason)


@router.post("/credentials/issue")
def issue(request: Request, body: IssueRequest):
    """Returns the plaintext code exactly once, for the printable card."""
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *ROLES)
        deps.enforce_csrf(conn, request)

        attendee_id = body.attendee_id
        if not attendee_id:
            if not body.attendee_name:
                raise NotFound("Select an attendee first.")
            attendee_id = entitlements.ensure_attendee(conn, body.attendee_name)

        count = entitlements.count_active_representations(conn, attendee_id)
        if count == 0:
            raise Conflict("Assign at least one apartment to this attendee before "
                           "issuing a voting code.")
        result = credentials.issue_credential(conn, svc.agm_key, attendee_id, count,
                                              operator["operator_id"])
        entitlements.add_credential_to_open_resolutions(
            conn, result["credential_id"], count)
        return result


@router.post("/credentials/{credential_id}/reset")
def reset(credential_id: str, request: Request, body: ResetRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *ROLES)
        deps.enforce_csrf(conn, request)
        result = credentials.reset_credential(conn, svc.agm_key, credential_id,
                                              operator["operator_id"], body.reason)
        attendee = conn.execute(
            """SELECT display_name FROM attendees
                WHERE attendee_id = (SELECT attendee_id FROM credentials WHERE credential_id = ?)""",
            (result["credential_id"],),
        ).fetchone()
        result["attendee_name"] = attendee["display_name"] if attendee else ""
        return result
