"""The five web surfaces, plus operator sign-in.

Pages are server-rendered shells; the live parts poll small JSON endpoints. No
framework, no build step, and nothing fetched from the internet.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import APP_VERSION
from ..domain import agm, resolutions, results
from ..api import deps

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter(tags=["web"])


def _page(request: Request, name: str, **context) -> HTMLResponse:
    svc = deps.services(request)
    with svc.db.reader() as conn:
        agm_row = agm.get(conn)
        operator = deps.current_operator(conn, request)
    context.setdefault("agm", dict(agm_row) if agm_row else None)
    context.setdefault("demo", bool(agm_row["is_demo"]) if agm_row else False)
    context.setdefault("operator", dict(operator) if operator else None)
    context.setdefault("csrf_token", operator["csrf_token"] if operator else "")
    context.setdefault("version", APP_VERSION)
    context.setdefault("cfg", svc.config)
    return templates.TemplateResponse(request, name, context)


def _require_operator_page(request: Request, *roles: str):
    """Returns a redirect to the sign-in page, or None if the operator may pass."""
    svc = deps.services(request)
    with svc.db.reader() as conn:
        operator = deps.current_operator(conn, request)
    if operator is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if roles and operator["role"] != "ADMIN" and operator["role"] not in roles:
        return RedirectResponse("/login?denied=1", status_code=303)
    return None


# --------------------------------------------------------------------------
# 1. voter
# --------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def voter_page(request: Request):
    return _page(request, "voter.html")


# --------------------------------------------------------------------------
# operator sign-in
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/mc", denied: int = 0):
    return _page(request, "login.html", next_url=next, denied=bool(denied))


# --------------------------------------------------------------------------
# 2. MC console
# --------------------------------------------------------------------------

@router.get("/mc", response_class=HTMLResponse)
def mc_page(request: Request):
    redirect = _require_operator_page(request, "MC")
    return redirect or _page(request, "mc.html")


# --------------------------------------------------------------------------
# 3. registration desk
# --------------------------------------------------------------------------

@router.get("/registration", response_class=HTMLResponse)
def registration_page(request: Request):
    redirect = _require_operator_page(request, "REGISTRATION")
    return redirect or _page(request, "registration.html")


@router.get("/checkin", response_class=HTMLResponse)
def checkin_page(request: Request):
    """Assign representations only, sized for a phone or tablet in the queue.

    Deliberately does not issue voting codes: a code has to be printed, folded
    and handed over privately, which is a desk job. This page is for the person
    walking the queue capturing who is representing what.
    """
    redirect = _require_operator_page(request, "REGISTRATION")
    return redirect or _page(request, "checkin.html")


# --------------------------------------------------------------------------
# 4. scrutineer
# --------------------------------------------------------------------------

@router.get("/scrutineer", response_class=HTMLResponse)
def scrutineer_page(request: Request):
    redirect = _require_operator_page(request, "SCRUTINEER", "MC")
    return redirect or _page(request, "scrutineer.html")


# --------------------------------------------------------------------------
# 5. projector
# --------------------------------------------------------------------------

@router.get("/projector", response_class=HTMLResponse)
def projector_page(request: Request):
    redirect = _require_operator_page(request, "MC", "SCRUTINEER")
    return redirect or _page(request, "projector.html")


@router.get("/api/v1/projector/state")
def projector_state(request: Request):
    """Wording while open; the result only once the MC has shown it."""
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, "MC", "ADMIN", "SCRUTINEER")
        agm_row = agm.require(conn)
        open_row = resolutions.active(conn)

        if open_row is not None:
            part = results.participation(conn, open_row["resolution_id"])
            return {
                "mode": "voting_open",
                "agm_title": agm_row["title"],
                "resolution": {"number": open_row["number"], "title": open_row["title"],
                               "full_text": open_row["full_text"],
                               "version": open_row["version"]},
                "participation": part,
            }

        # Anchor on the resolution the meeting is actually on -- the most recently
        # opened one -- not on whichever result was last projected. Falling back
        # to an older result would put the previous resolution's numbers on the
        # hall screen during the gap between CLOSE VOTING and SHOW RESULT, where
        # the room would read them as the current outcome.
        current = conn.execute(
            """SELECT * FROM ballot.resolutions
                WHERE opened_at IS NOT NULL
                ORDER BY opened_at DESC LIMIT 1"""
        ).fetchone()

        if current is not None:
            snapshot = None
            if results.is_result_shown(conn, current["resolution_id"]):
                snapshot = results.result_for(conn, current["resolution_id"])
            if snapshot is not None:
                return {"mode": "result", "agm_title": agm_row["title"],
                        "result": snapshot, "status": current["status"]}
            # Closed, but the MC has not released the result yet. Hold on this
            # resolution and show no counts.
            return {"mode": "closed", "agm_title": agm_row["title"],
                    "status": current["status"],
                    "resolution": {"number": current["number"], "title": current["title"],
                                   "full_text": current["full_text"],
                                   "version": current["version"]}}

        # Step 1 of the MC's sequence is to display and discuss the wording while
        # it is still a draft, so drafts belong on this screen -- clearly labelled
        # as not yet final, so nobody mistakes them for the wording being voted on.
        pending = conn.execute(
            """SELECT * FROM ballot.resolutions
                WHERE status IN ('DRAFT','FINALIZED') AND superseded_by IS NULL
                ORDER BY seq ASC LIMIT 1"""
        ).fetchone()
        return {"mode": "idle", "agm_title": agm_row["title"],
                "next": {"number": pending["number"], "title": pending["title"],
                         "full_text": pending["full_text"],
                         "finalized": pending["status"] == "FINALIZED"} if pending else None}


# --------------------------------------------------------------------------
# administrator console
# --------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    redirect = _require_operator_page(request, "ADMIN")
    return redirect or _page(request, "admin.html")
