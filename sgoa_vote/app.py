"""FastAPI application factory."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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


# ---------------------------------------------------------------------------
# several meetings from one process
# ---------------------------------------------------------------------------

EVENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def discover_events(events_dir: Path) -> list[str]:
    """Every subdirectory of the events root is a meeting, named by its folder.

    Creating a meeting is creating a directory; deleting one is deleting the
    directory. Nothing else keeps a list, so the filesystem cannot disagree with
    the application about which meetings exist.
    """
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        return []
    names = []
    for child in sorted(events_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not EVENT_NAME.match(child.name):
            continue
        names.append(child.name)
    return names


def create_multi_event_app(events_dir: Path, config=None) -> FastAPI:
    """Serve every meeting under its own path prefix: /<event>/.

    Each event gets its own Services -- its own three databases, its own HMAC
    key, its own export and backup directories -- and its own mounted
    application. Cookies are scoped to the event prefix, so a session or CSRF
    token issued by one meeting is never sent to another.

    Events are discovered at startup. Adding or removing one means restarting,
    which is the right trade for a system where an operator needs to be able to
    say exactly what was being served during a meeting.
    """
    from .config import Config

    events_dir = Path(events_dir)
    base_config = config or Config.load()

    root = FastAPI(title="SGOA AGM Voting", version=APP_VERSION,
                   docs_url=None, redoc_url=None, openapi_url=None)

    mounted = []
    for name in discover_events(events_dir):
        event_config = replace(base_config)
        event_config.data_dir = str(events_dir / name)
        event_config.export_dir = str(events_dir / name / "export")
        event_config.backup_dir = str(events_dir / name / "backups")

        svc = Services(event_config)
        svc.event_name = name
        sub = create_app(svc)
        root.mount(f"/{name}", sub, name=name)
        mounted.append({"name": name, "services": svc})

    root.state.events = mounted
    root.state.events_dir = events_dir

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

    @root.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = []
        for entry in mounted:
            svc = entry["services"]
            try:
                with svc.db.reader() as conn:
                    agm_row = conn.execute(
                        "SELECT title, agm_date, status, is_demo FROM agms LIMIT 1"
                    ).fetchone()
                    ballots = conn.execute(
                        "SELECT COUNT(*) AS n FROM ballot.ballots").fetchone()["n"]
            except Exception:                     # a half-built event directory
                agm_row, ballots = None, 0
            rows.append({
                "name": entry["name"],
                "title": agm_row["title"] if agm_row else "not set up yet",
                "date": agm_row["agm_date"] if agm_row else "",
                "status": agm_row["status"] if agm_row else "NO AGM",
                "demo": bool(agm_row["is_demo"]) if agm_row else False,
                "ballots": ballots,
            })
        return templates.TemplateResponse(
            request, "events.html",
            {"events": rows, "events_dir": str(events_dir), "version": APP_VERSION})

    @root.get("/api/v1/events")
    def list_events():
        return {"events": [entry["name"] for entry in mounted]}

    root.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    return root
