"""Privacy and security gates. A failure here should block a release.

The central claim of this system is that the ballot store cannot be joined back
to the eligibility store. These tests try to break that claim directly, by
reading the raw database rather than by asking the application politely.
"""

from __future__ import annotations

import sqlite3

import pytest
from helpers import credential_id_for

BALLOT_COLUMNS = {"ballot_id", "resolution_id", "choice", "accepted_at"}

# Note operator_id is absent on purpose: the spec requires administrative
# actions to name the operator who took them. What must never appear in the
# ballot database is anything identifying a *voter*.
IDENTIFYING_COLUMN_NAMES = {
    "credential_id", "credential_code", "code", "code_hash", "attendee_id",
    "attendee_name", "apartment_id", "session_id", "session_id_hash",
    "device_fingerprint", "ip", "ip_address", "mac", "mac_address",
    "proxy_ref", "proxy_source_apartment", "representation_id",
}


def sensitive_values(svc) -> set[str]:
    """Everything that would identify a voter if it appeared beside a choice."""
    values = set()
    with svc.db.reader() as conn:
        for row in conn.execute("SELECT credential_id, code_hash FROM credentials"):
            values.update({row["credential_id"], row["code_hash"]})
        for row in conn.execute("SELECT attendee_id, display_name FROM attendees"):
            values.update({row["attendee_id"], row["display_name"]})
        for row in conn.execute("SELECT apartment_id FROM apartments"):
            values.add(row["apartment_id"])
        for row in conn.execute("SELECT session_id_hash FROM sessions"):
            values.add(row["session_id_hash"])
        for row in conn.execute("SELECT representation_id, proxy_ref FROM representations"):
            values.add(row["representation_id"])
            if row["proxy_ref"]:
                values.add(row["proxy_ref"])
    return {v for v in values if v}


@pytest.fixture
def after_voting(seeded, open_resolution, voter_factory):
    """A resolution with real ballots on it, from a proxy holder and singles."""
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 2, "AGAINST": 1})
    voter_factory(seeded.demo_codes["Sunil Menon"]).vote(ident, {"AGAINST": 2})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"ABSTAIN": 1})
    return ident


# -- AC-04: no linkage anywhere in durable storage --------------------------

def test_ac04_ballot_table_has_exactly_four_columns(seeded, after_voting):
    with seeded.db.reader() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA ballot.table_info(ballots)")}
    assert columns == BALLOT_COLUMNS, (
        f"the ballot table gained columns: {columns - BALLOT_COLUMNS}")


def test_ac04_no_table_in_the_ballot_database_names_an_identifier(seeded, after_voting):
    offenders = []
    with seeded.db.reader() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM ballot.sqlite_master WHERE type='table'")]
        for table in tables:
            for column in conn.execute(f"PRAGMA ballot.table_info({table})"):
                if column["name"] in IDENTIFYING_COLUMN_NAMES:
                    offenders.append(f"{table}.{column['name']}")
    assert offenders == [], f"identifying columns in the ballot database: {offenders}"


def test_ac04_no_ballot_row_contains_any_identifying_value(seeded, after_voting):
    secrets = sensitive_values(seeded)
    with seeded.db.reader() as conn:
        rows = conn.execute("SELECT * FROM ballot.ballots").fetchall()
    assert rows, "expected ballots to exist for this test to mean anything"

    for row in rows:
        for value in tuple(row):
            text = str(value)
            for secret in secrets:
                assert secret not in text, (
                    f"ballot row leaks {secret!r} in {text!r}")


def test_ac04_audit_payloads_never_name_a_credential_or_attendee(seeded, after_voting):
    secrets = sensitive_values(seeded)
    # Apartment identifiers legitimately appear in registration audit events
    # (spec §15.4: the registration audit may show which apartments were
    # represented). What must never appear is a credential, session or code.
    with seeded.db.reader() as conn:
        apartments = {r["apartment_id"] for r in conn.execute(
            "SELECT apartment_id FROM apartments")}
        names = {r["display_name"] for r in conn.execute(
            "SELECT display_name FROM attendees")}
    checked = secrets - apartments

    with seeded.db.reader() as conn:
        events = conn.execute(
            "SELECT sequence, event_type, payload_json FROM audit.audit_log").fetchall()

    for event in events:
        payload = event["payload_json"]
        for secret in checked:
            assert secret not in payload, (
                f"audit event {event['sequence']} ({event['event_type']}) leaks {secret!r}")
        for name in names:
            assert name not in payload, (
                f"audit event {event['sequence']} names attendee {name!r}")


def test_ac04_entitlements_consumed_events_are_aggregate_only(seeded, after_voting):
    import json

    with seeded.db.reader() as conn:
        events = conn.execute(
            """SELECT payload_json FROM audit.audit_log
                WHERE event_type = 'ENTITLEMENTS_CONSUMED'""").fetchall()
    assert events, "voting should have produced consumption events"

    for event in events:
        payload = json.loads(event["payload_json"])
        assert set(payload) <= {"resolution_id", "resolution_number", "count"}
        assert isinstance(payload["count"], int)


def test_ac04_the_audit_module_refuses_to_write_an_identifying_payload(seeded):
    from sgoa_vote.domain import audit
    from sgoa_vote.domain.audit import AuditPayloadError

    with seeded.db.writer() as conn:
        with pytest.raises(AuditPayloadError):
            audit.append(conn, "VOTING_OPENED", {"credential_id": "cred_123"})
        with pytest.raises(AuditPayloadError):
            audit.append(conn, "ENTITLEMENTS_CONSUMED",
                         {"resolution_id": "R1", "count": 1, "apartment": "A7"})


# -- AC-06: ballots are immutable in the database itself --------------------

def test_ac06_updating_a_ballot_is_blocked_even_from_raw_sql(seeded, after_voting):
    path = seeded.db.paths["ballot"]
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            conn.execute("UPDATE ballots SET choice = 'FOR'")
        assert "immutable" in str(caught.value).lower()
    finally:
        conn.close()


def test_ac06_deleting_a_ballot_is_blocked_even_from_raw_sql(seeded, after_voting):
    path = seeded.db.paths["ballot"]
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            conn.execute("DELETE FROM ballots")
        assert "immutable" in str(caught.value).lower()
    finally:
        conn.close()


def test_ac06_result_snapshots_are_immutable_too(seeded, after_voting, close_resolution):
    close_resolution(after_voting)
    conn = sqlite3.connect(seeded.db.paths["ballot"])
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE result_snapshots SET for_count = 99")
    finally:
        conn.close()


def test_the_ledger_cannot_be_pushed_past_the_entitlement_by_raw_sql(seeded, after_voting):
    """The over-vote guard is a CHECK constraint, not only an application test."""
    conn = sqlite3.connect(seeded.db.paths["eligibility"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE credential_resolution_ledger SET consumed_count = 99")
    finally:
        conn.close()


# -- AC-07: no early sight of the totals ------------------------------------

def test_ac07_results_are_sealed_while_voting_is_open_even_for_admin(
        seeded, open_resolution, admin, mc, scrutineer):
    ident = open_resolution("R1")

    for operator in (admin, mc, scrutineer):
        response = operator.get(f"/api/v1/admin/results/{ident}")
        assert response.status_code == 403, (
            f"{operator.client} saw results while voting was open")
        assert response.json()["error"] == "results_sealed"

    # Participation is available, and shows no breakdown by choice.
    participation = mc.get(f"/api/v1/admin/participation/{ident}").json()
    assert set(participation) == {
        "resolution", "resolution_id", "title", "status",
        "eligible_entitlements", "votes_received", "not_yet_cast", "percent"}


def test_ac07_the_reconciliation_view_is_also_sealed_while_open(
        seeded, open_resolution, scrutineer):
    open_resolution("R1")
    rows = scrutineer.get("/api/v1/admin/reconciliation").json()["resolutions"]
    open_row = next(r for r in rows if r["status"] == "VOTING_OPEN")
    assert open_row["sealed"] is True
    assert "for" not in open_row


def test_results_become_available_once_voting_closes(
        seeded, after_voting, close_resolution, mc):
    close_resolution(after_voting)
    response = mc.get(f"/api/v1/admin/results/{after_voting}")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["for_count"] == 2
    assert result["against_count"] == 3
    assert result["abstain_count"] == 1


# -- credential guessing ----------------------------------------------------

def test_repeated_wrong_codes_are_rate_limited_and_audited(seeded, app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    statuses = []
    for _ in range(10):
        response = client.post("/api/v1/voter/join", json={"code": "ZZZ9"})
        statuses.append(response.status_code)

    assert 429 in statuses, "brute-force attempts were never throttled"
    assert statuses.index(429) <= 6

    with seeded.db.reader() as conn:
        events = conn.execute(
            """SELECT COUNT(*) AS n FROM audit.audit_log
                WHERE event_type = 'INVALID_CREDENTIAL_THRESHOLD'""").fetchone()["n"]
    assert events >= 1


def test_a_second_device_cannot_use_a_code_that_is_already_in_use(
        seeded, voter_factory):
    code = seeded.demo_codes["Kavitha Iyer"]
    assert voter_factory(code).joined

    second = voter_factory(code)
    assert not second.joined
    assert second.join_response.status_code == 409
    assert "assistance desk" in second.join_response.json()["next_action"]


def test_an_expired_or_absent_session_cannot_read_voter_state(app):
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/api/v1/voter/state")
    assert response.status_code == 401


# -- audit tampering --------------------------------------------------------

def test_tampering_with_an_audit_event_breaks_the_chain(seeded, after_voting):
    from sgoa_vote.domain import audit

    with seeded.db.reader() as conn:
        assert audit.verify_chain(conn)["ok"] is True
        target = conn.execute(
            """SELECT sequence FROM audit.audit_log
                WHERE event_type = 'ENTITLEMENTS_CONSUMED' LIMIT 1""").fetchone()["sequence"]

    # Someone with the file edits history. The triggers are dropped first,
    # which is exactly why the chain, not the trigger, is the real defence.
    conn = sqlite3.connect(seeded.db.paths["audit"])
    try:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET payload_json = ? WHERE sequence = ?",
                     ('{"count":99,"resolution_id":"R1","resolution_number":"R1"}', target))
        conn.commit()
    finally:
        conn.close()

    with seeded.db.reader() as conn:
        verification = audit.verify_chain(conn)
    assert verification["ok"] is False
    assert verification["first_bad_sequence"] == target


def test_deleting_an_audit_event_breaks_the_chain(seeded, after_voting):
    from sgoa_vote.domain import audit

    conn = sqlite3.connect(seeded.db.paths["audit"])
    try:
        conn.execute("DROP TRIGGER audit_log_no_delete")
        conn.execute("DELETE FROM audit_log WHERE sequence = 3")
        conn.commit()
    finally:
        conn.close()

    with seeded.db.reader() as conn:
        assert audit.verify_chain(conn)["ok"] is False


# -- injection and CSRF -----------------------------------------------------

def test_sql_injection_in_resolution_text_is_stored_as_literal_text(seeded, mc):
    payload = "'); DROP TABLE ballots; --"
    response = mc.post("/api/v1/admin/resolutions",
                       {"title": payload, "full_text": f"Wording {payload}"})
    assert response.status_code == 200
    assert response.json()["title"] == payload

    with seeded.db.reader() as conn:
        assert conn.execute(
            "SELECT name FROM ballot.sqlite_master WHERE name='ballots'").fetchone()
        stored = conn.execute(
            "SELECT title FROM ballot.resolutions WHERE title = ?", (payload,)).fetchone()
    assert stored is not None


def test_injection_in_registration_fields_is_stored_as_literal_text(seeded, registrar):
    payload = "Robert'); DROP TABLE attendees; --"
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "C10", "attendee_name": payload,
                               "rep_type": "OWN"})
    assert response.status_code == 200

    with seeded.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM attendees").fetchone()["n"] > 1
        assert conn.execute("SELECT 1 FROM attendees WHERE display_name = ?",
                            (payload,)).fetchone()


def test_state_changing_admin_calls_require_the_csrf_token(seeded, mc, find_resolution):
    ident = find_resolution("R1")
    naked = mc.client            # same session, but drop the token header
    del naked.headers["X-CSRF-Token"]

    response = naked.post(f"/api/v1/admin/resolutions/{ident}/finalize", json={})
    assert response.status_code == 403
    assert "expired" in response.json()["message"].lower()


def test_a_wrong_csrf_token_is_rejected(seeded, registrar):
    registrar.client.headers["X-CSRF-Token"] = "not-the-right-token"
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "C11", "attendee_name": "Someone",
                               "rep_type": "OWN"})
    assert response.status_code == 403


def test_an_unauthenticated_caller_cannot_reach_the_admin_api(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/api/v1/admin/resolutions").status_code == 401
    assert client.post("/api/v1/admin/backup", json={}).status_code == 401
    assert client.get("/api/v1/registration/apartments").status_code == 401


def test_role_separation_keeps_the_registration_desk_out_of_the_mc_console(
        seeded, registrar, find_resolution):
    ident = find_resolution("R1")
    response = registrar.post(f"/api/v1/admin/resolutions/{ident}/finalize")
    assert response.status_code == 403
