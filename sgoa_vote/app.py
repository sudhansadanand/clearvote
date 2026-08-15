"""FastAPI application factory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, health, registration, session, voter
from .config import APP_VERSION
from .domain.audit import AuditPayloadError
from .domain.errors import Conflict, DomainError
from .services import Services
from .web import routes as web_routes

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(svc: Services | None = None) -> FastAPI:
    svc = svc or Services()

    # No /docs or /redoc: FastAPI's interactive docs pull Swagger assets from a
    # CDN, which would break the "nothing loads from the internet" requirement.
    app = FastAPI(title="SGOA AGM Voting System", version=APP_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    @app.exception_handler(AuditPayloadError)
    async def audit_payload_handler(request: Request, exc: AuditPayloadError):
        # A programming error that would have leaked identity into the audit
        # trail. Refuse loudly rather than write it.
        return JSONResponse({"error": "audit_payload_rejected", "message": str(exc)},
                            status_code=500)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_handler(request: Request, exc: sqlite3.IntegrityError):
        text = str(exc)
        if "ux_representations_one_active" in text:
            friendly = Conflict("That apartment already has an active representation.")
        elif "consumed_count <= eligible_count" in text:
            friendly = Conflict("That would use more votes than this code was issued.")
        else:
            friendly = Conflict("That change conflicts with an existing record.")
        return JSONResponse(friendly.as_dict(), status_code=friendly.status_code)

    app.include_router(voter.router)
    app.include_router(registration.router)
    app.include_router(admin.router)
    app.include_router(session.router)
    app.include_router(health.router)
    app.include_router(web_routes.router)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    return app
