"""Health endpoint (work order §7). Admin-authenticated, and free of ballot data."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Request

from ..config import APP_VERSION
from ..domain import agm, audit, credentials
from . import deps

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health(request: Request):
    svc = deps.services(request)
    with svc.db.reader() as conn:
        deps.require_operator(conn, request, "ADMIN", "SCRUTINEER")
        agm_row = agm.get(conn)
        chain = audit.verify_chain(conn)
        sessions = credentials.active_session_count(conn)
        open_resolutions = conn.execute(
            "SELECT COUNT(*) AS n FROM ballot.resolutions WHERE status='VOTING_OPEN'"
        ).fetchone()["n"]

    usage = shutil.disk_usage(svc.data_path)
    return {
        "status": "ok",
        "version": APP_VERSION,
        "agm_status": agm_row["status"] if agm_row else "NO_AGM",
        "resolutions_open": open_resolutions,
        "active_voter_sessions": sessions,
        "database_integrity": svc.db.integrity(),
        "journal_modes": svc.db.journal_modes(),
        "audit_chain": {"ok": chain["ok"], "events": chain["events"],
                        "first_bad_sequence": chain["first_bad_sequence"]},
        "disk_free_mb": round(usage.free / (1024 * 1024)),
        "disk_total_mb": round(usage.total / (1024 * 1024)),
    }
