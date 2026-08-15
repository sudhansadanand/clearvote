"""MC, scrutineer and administrator API (work order §7).

The rule that matters most in this file: /results/{id} is refused while voting is
open, for every role including ADMIN. It is enforced here, in the endpoint, not
in a template -- there is a test that calls it directly with an admin session.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..domain import agm, audit, auth, entitlements, reports, resolutions, results
from ..domain.errors import Forbidden
from . import deps

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

MC_ROLES = ("MC", "ADMIN")
READ_ROLES = ("MC", "ADMIN", "SCRUTINEER")


class CreateResolution(BaseModel):
    title: str
    full_text: str
    number: str | None = None
    voting_rule: str = "FOR_GT_AGAINST"


class EditResolution(BaseModel):
    title: str | None = None
    full_text: str | None = None
    voting_rule: str | None = None


class NoteRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class ReauthRequest(BaseModel):
    password: str = ""


# --------------------------------------------------------------------------
# resolution lifecycle
# --------------------------------------------------------------------------

@router.get("/resolutions")
def list_resolutions(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        rows = resolutions.list_all(conn, include_superseded=True)
        for row in rows:
            row["result_shown"] = results.is_result_shown(conn, row["resolution_id"])
        return {"resolutions": rows, "next_number": resolutions.next_number(conn)}


@router.post("/resolutions")
def create(request: Request, body: CreateResolution):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.create_draft(
            conn, body.title, body.full_text, number=body.number,
            voting_rule=body.voting_rule, eligible_pool_id=svc.config.eligible_pool_id,
            operator_id=operator["operator_id"])


@router.patch("/resolutions/{ident}")
def edit(ident: str, request: Request, body: EditResolution):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.edit_draft(conn, ident, title=body.title,
                                      full_text=body.full_text,
                                      voting_rule=body.voting_rule,
                                      operator_id=operator["operator_id"])


@router.post("/resolutions/{ident}/finalize")
def finalize(ident: str, request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.finalize(conn, ident, operator["operator_id"])


@router.post("/resolutions/{ident}/amend")
def amend(ident: str, request: Request, body: EditResolution):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.amend(conn, ident, title=body.title, full_text=body.full_text,
                                 voting_rule=body.voting_rule,
                                 operator_id=operator["operator_id"])


@router.post("/resolutions/{ident}/open")
def open_voting(ident: str, request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.open_voting(conn, ident, operator["operator_id"])


@router.post("/resolutions/{ident}/close")
def close_voting(ident: str, request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return results.close_voting(conn, ident, operator["operator_id"],
                                    auto_publish=svc.config.auto_publish_results)


@router.post("/resolutions/{ident}/show-result")
def show_result(ident: str, request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return results.show_result(conn, ident, operator["operator_id"])


@router.post("/resolutions/{ident}/withdraw")
def withdraw(ident: str, request: Request, body: NoteRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.withdraw(conn, ident, body.note, operator["operator_id"])


@router.post("/resolutions/{ident}/not-put-to-vote")
def not_put_to_vote(ident: str, request: Request, body: NoteRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.not_put_to_vote(conn, ident, body.note, operator["operator_id"])


@router.post("/resolutions/{ident}/disposition")
def disposition(ident: str, request: Request, body: NoteRequest):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, *MC_ROLES)
        deps.enforce_csrf(conn, request)
        return resolutions.record_disposition(conn, ident, body.note,
                                              operator["operator_id"])


# --------------------------------------------------------------------------
# participation and results
# --------------------------------------------------------------------------

@router.get("/participation/{ident}")
def participation(ident: str, request: Request):
    """Eligible, cast, not cast. No breakdown by choice, at any point."""
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        return results.participation(conn, ident)


@router.get("/results/{ident}")
def get_results(ident: str, request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        row = resolutions.require(conn, ident)

        # AC-07. No role escapes this, and no query parameter turns it off.
        if row["status"] == "VOTING_OPEN":
            raise Forbidden(
                "Results are sealed while voting is open. "
                "Close the voting to see the counts.",
                code="results_sealed",
            )

        snapshot = results.result_for(conn, ident)
        if snapshot is None:
            return {"resolution": row["number"], "status": row["status"],
                    "result": None,
                    "reconciliation": results.reconciliation(conn, row["resolution_id"])}
        return {"resolution": row["number"], "status": row["status"], "result": snapshot,
                "reconciliation": results.reconciliation(conn, row["resolution_id"])}


@router.get("/reconciliation")
def full_reconciliation(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        out = []
        for row in resolutions.list_all(conn):
            if row["status"] == "VOTING_OPEN":
                out.append({"resolution": row["number"], "status": row["status"],
                            "sealed": True})
                continue
            rec = results.reconciliation(conn, row["resolution_id"])
            rec.update({"resolution": row["number"], "status": row["status"],
                        "title": row["title"], "sealed": False})
            out.append(rec)
        return {"resolutions": out}


# --------------------------------------------------------------------------
# audit, backup, report
# --------------------------------------------------------------------------

@router.get("/audit/verify")
def verify_audit(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        return {"verification": audit.verify_chain(conn),
                "checkpoints": audit.checkpoints(conn)}


@router.get("/audit/log")
def audit_log(request: Request, limit: int = 200):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES)
        return {"events": audit.recent(conn, limit)}


@router.post("/backup")
def backup(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, "ADMIN")
        deps.enforce_csrf(conn, request)
    return reports.create_backup(svc)


@router.post("/agm/open-registration")
def open_registration(request: Request):
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, "ADMIN")
        deps.enforce_csrf(conn, request)
        return agm.open_registration(conn, operator["operator_id"])


@router.post("/reports/final")
def final_report(request: Request, body: ReauthRequest):
    """Certification bundle. Requires the operator to re-enter their password."""
    svc = deps.services(request)
    with svc.db.writer() as conn:
        operator = deps.require_operator(conn, request, "ADMIN")
        deps.enforce_csrf(conn, request)
        auth.verify_operator(conn, operator["username"], body.password)
        agm.assert_ready_to_finalize(conn)
    return reports.generate_certification_bundle(svc, operator_id=None)


@router.get("/summary")
def summary(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, *READ_ROLES, "REGISTRATION")
        row = agm.require(conn)
        return {"agm": dict(row),
                "summary": entitlements.representation_summary(conn),
                "active_sessions": conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]}
