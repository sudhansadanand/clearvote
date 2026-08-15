"""Several meetings served from one process, each under /<event>/.

The interesting property is isolation. Two meetings on one laptop must not be
able to reach each other: not their databases, not their voting codes, and not
their operator sessions. A leak here would let a signed-in operator from a
rehearsal act on the real AGM.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgoa_vote.app import create_multi_event_app, discover_events
from sgoa_vote.config import Config
from sgoa_vote.seed import seed
from sgoa_vote.services import Services

EVENTS = ("agm2026", "trial-run")


@pytest.fixture
def events_root(tmp_path):
    """Two independent meetings, each in its own folder."""
    root = tmp_path / "events"
    root.mkdir()
    codes = {}
    for name in EVENTS:
        cfg = Config()
        cfg.data_dir = str(root / name)
        service = Services(cfg)
        result = seed(service)
        codes[name] = {row["name"]: row["code"] for row in result["credentials"]}
        service.close()          # the app opens its own connections
    root_codes = codes
    return root, root_codes


@pytest.fixture
def multi(events_root):
    root, codes = events_root
    app = create_multi_event_app(root, Config())
    with TestClient(app) as client:
        yield client, codes, app


# -- discovery --------------------------------------------------------------

def test_a_folder_is_a_meeting(events_root):
    root, _ = events_root
    assert discover_events(root) == sorted(EVENTS)


def test_deleting_the_folder_deletes_the_meeting(events_root):
    import shutil

    root, _ = events_root
    shutil.rmtree(root / "trial-run")
    assert discover_events(root) == ["agm2026"]


def test_hidden_and_scratch_folders_are_not_meetings(events_root):
    root, _ = events_root
    (root / ".git").mkdir()
    (root / "_scratch").mkdir()
    (root / "not a valid name").mkdir()
    assert discover_events(root) == sorted(EVENTS)


def test_a_missing_events_directory_is_not_an_error(tmp_path):
    assert discover_events(tmp_path / "nothing-here") == []


# -- routing ----------------------------------------------------------------

def test_each_meeting_is_served_under_its_own_prefix(multi):
    client, _, _ = multi
    for name in EVENTS:
        response = client.get(f"/{name}/")
        assert response.status_code == 200
        assert "JOIN AGM" in response.text


def test_the_index_lists_every_meeting(multi):
    client, _, _ = multi
    response = client.get("/")
    assert response.status_code == 200
    for name in EVENTS:
        assert name in response.text
        assert f'href="/{name}/"' in response.text


def test_pages_link_within_their_own_meeting(multi):
    client, _, _ = multi
    response = client.get("/agm2026/login")
    assert 'data-base="/agm2026"' in response.text
    assert 'href="/agm2026/static/app.css"' in response.text
    assert 'src="/agm2026/static/app.js"' in response.text
    # and nothing points at the other meeting
    assert "trial-run" not in response.text


def test_the_event_name_is_shown_on_the_page(multi):
    client, _, _ = multi
    assert "agm2026" in client.get("/agm2026/login").text


def test_an_unknown_event_is_not_served(multi):
    client, _, _ = multi
    assert client.get("/no-such-meeting/").status_code == 404


def test_signing_in_redirects_within_the_same_meeting(multi):
    client, _, _ = multi
    response = client.get("/trial-run/mc", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/trial-run/login?next=/mc"


# -- isolation --------------------------------------------------------------

def test_each_meeting_has_its_own_key_and_databases(events_root):
    root, _ = events_root
    key_a = (root / "agm2026" / "agm_key").read_bytes()
    key_b = (root / "trial-run" / "agm_key").read_bytes()
    assert key_a != key_b
    for name in EVENTS:
        for filename in ("eligibility.db", "ballot.db", "audit.db"):
            assert (root / name / filename).exists()


def test_an_operator_session_does_not_carry_to_another_meeting(multi):
    client, _, _ = multi

    signed_in = client.post("/agm2026/api/v1/auth/login",
                            json={"username": "mc", "password": "sgoa-demo"})
    assert signed_in.status_code == 200

    # Same browser, same cookie jar, other meeting.
    assert client.get("/agm2026/api/v1/admin/resolutions").status_code == 200
    assert client.get("/trial-run/api/v1/admin/resolutions").status_code == 401


def test_a_voter_session_does_not_carry_to_another_meeting(multi):
    client, codes, _ = multi

    joined = client.post("/agm2026/api/v1/voter/join",
                         json={"code": codes["agm2026"]["Kavitha Iyer"]})
    assert joined.status_code == 200

    assert client.get("/agm2026/api/v1/voter/state").status_code == 200
    assert client.get("/trial-run/api/v1/voter/state").status_code == 401


def test_votes_cast_in_one_meeting_do_not_appear_in_the_other(multi):
    client, codes, app = multi

    mc = TestClient(app)
    login = mc.post("/agm2026/api/v1/auth/login",
                    json={"username": "mc", "password": "sgoa-demo"})
    mc.headers["X-CSRF-Token"] = login.json()["csrf_token"]

    listing = mc.get("/agm2026/api/v1/admin/resolutions").json()["resolutions"]
    ident = listing[0]["resolution_id"]
    mc.post(f"/agm2026/api/v1/admin/resolutions/{ident}/finalize", json={})
    mc.post(f"/agm2026/api/v1/admin/resolutions/{ident}/open", json={})

    voter = TestClient(app)
    voter.post("/agm2026/api/v1/voter/join",
               json={"code": codes["agm2026"]["Meera Raghavan"]})
    state = voter.get("/agm2026/api/v1/voter/state").json()
    voter.post(f"/agm2026/api/v1/resolutions/{ident}/vote", json={
        "client_submission_id": "iso-test-1",
        "resolution_version": state["resolution"]["version"],
        "resolution_hash": state["resolution"]["text_hash"],
        "allocation": {"FOR": 3}, "confirmed": True})

    counts = {}
    for name, entry in zip(EVENTS, app.state.events):
        with entry["services"].db.reader() as conn:
            counts[entry["name"]] = conn.execute(
                "SELECT COUNT(*) AS n FROM ballot.ballots").fetchone()["n"]

    assert counts["agm2026"] == 3
    assert counts["trial-run"] == 0


def test_each_meeting_exports_and_backs_up_inside_its_own_folder(events_root):
    root, _ = events_root
    app = create_multi_event_app(root, Config())
    for entry in app.state.events:
        service = entry["services"]
        assert service.export_path == root / entry["name"] / "export"
        assert service.backup_path == root / entry["name"] / "backups"
