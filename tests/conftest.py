"""Shared fixtures. Every test gets its own throwaway three-database store."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # so tests can import helpers

from sgoa_vote.app import create_app          # noqa: E402
from sgoa_vote.config import Config           # noqa: E402
from sgoa_vote.seed import seed               # noqa: E402
from sgoa_vote.services import Services       # noqa: E402


@pytest.fixture
def svc(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.export_dir = str(tmp_path / "export")
    cfg.backup_dir = str(tmp_path / "backups")
    service = Services(cfg)
    yield service
    service.close()


@pytest.fixture
def seeded(svc):
    """Demo AGM plus a lookup from attendee name to voting code."""
    result = seed(svc)
    codes = {row["name"]: row["code"] for row in result["credentials"]}
    svc.demo_codes = codes
    svc.demo_result = result
    return svc


@pytest.fixture
def app(seeded):
    return create_app(seeded)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


class Operator:
    """A signed-in console user. Carries its own cookie jar and CSRF token."""

    def __init__(self, app, username: str, password: str = "sgoa-demo"):
        self.client = TestClient(app)
        response = self.client.post("/api/v1/auth/login",
                                    json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf

    def post(self, url, json=None):
        return self.client.post(url, json=json if json is not None else {})

    def patch(self, url, json=None):
        return self.client.patch(url, json=json if json is not None else {})

    def get(self, url):
        return self.client.get(url)


class Voter:
    """A phone. Joins with a code and then behaves like the browser does."""

    def __init__(self, app, code: str):
        self.client = TestClient(app)
        self.code = code
        self.join_response = self.client.post("/api/v1/voter/join", json={"code": code})

    @property
    def joined(self) -> bool:
        return self.join_response.status_code == 200

    def state(self):
        return self.client.get("/api/v1/voter/state").json()

    def vote(self, resolution_id, allocation, submission_id="auto",
             version=None, text_hash=None, confirmed=True):
        if submission_id == "auto":
            import uuid
            submission_id = str(uuid.uuid4())
        state = self.state()
        resolution = state.get("resolution") or {}
        body = {
            "client_submission_id": submission_id,
            "resolution_version": version if version is not None else resolution.get("version"),
            "resolution_hash": text_hash if text_hash is not None else resolution.get("text_hash"),
            "allocation": allocation,
            "confirmed": confirmed,
        }
        return self.client.post(f"/api/v1/resolutions/{resolution_id}/vote", json=body)

    def preview(self, resolution_id, allocation):
        state = self.state()
        resolution = state.get("resolution") or {}
        return self.client.post(
            f"/api/v1/resolutions/{resolution_id}/vote/preview",
            json={"allocation": allocation,
                  "resolution_version": resolution.get("version"),
                  "resolution_hash": resolution.get("text_hash")})


@pytest.fixture
def mc(app):
    return Operator(app, "mc")


@pytest.fixture
def admin(app):
    return Operator(app, "admin")


@pytest.fixture
def registrar(app):
    return Operator(app, "registration")


@pytest.fixture
def scrutineer(app):
    return Operator(app, "scrutineer")


@pytest.fixture
def voter_factory(app):
    def make(code):
        return Voter(app, code)
    return make


@pytest.fixture
def find_resolution(seeded):
    """Look up the current version of a resolution by its number."""
    def find(number: str) -> str:
        with seeded.db.reader() as conn:
            row = conn.execute(
                """SELECT resolution_id FROM ballot.resolutions
                    WHERE number = ? AND superseded_by IS NULL
                    ORDER BY version DESC LIMIT 1""", (number,)).fetchone()
        assert row is not None, f"no resolution numbered {number}"
        return row["resolution_id"]
    return find


@pytest.fixture
def open_resolution(mc, find_resolution):
    """Finalize and open a seeded draft, returning its id."""
    def go(number: str) -> str:
        ident = find_resolution(number)
        assert mc.post(f"/api/v1/admin/resolutions/{ident}/finalize").status_code == 200
        assert mc.post(f"/api/v1/admin/resolutions/{ident}/open").status_code == 200
        return ident
    return go


@pytest.fixture
def close_resolution(mc):
    def go(ident: str):
        response = mc.post(f"/api/v1/admin/resolutions/{ident}/close")
        assert response.status_code == 200, response.text
        return response.json()
    return go


