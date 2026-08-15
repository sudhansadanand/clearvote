"""The phone/tablet check-in page.

It is a thin surface over the same registration API, so the interesting
assertions are about what it is allowed to do and what it deliberately cannot.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from helpers import representation_id_for


def test_checkin_requires_sign_in(app):
    response = TestClient(app).get("/checkin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/checkin"


def test_checkin_renders_for_the_registration_desk(registrar):
    response = registrar.get("/checkin")
    assert response.status_code == 200
    assert "CHECK IN" in response.text
    assert "ASSIGN REPRESENTATION" in response.text


def test_checkin_is_closed_to_roles_that_do_not_register_attendees(app):
    from conftest import Operator

    mc_operator = Operator(app, "mc")
    response = mc_operator.client.get("/checkin", follow_redirects=False)
    assert response.status_code == 303
    assert "denied" in response.headers["location"]


def test_checkin_offers_no_way_to_issue_a_voting_code(registrar):
    """A code must be printed, folded and handed over -- that stays at the desk."""
    page = registrar.get("/checkin").text
    assert "credentials/issue" not in page
    assert "ISSUE CODE" not in page
    assert "Reset code" not in page
    # It points the operator at the desk instead of pretending it can.
    assert "/registration" in page


def test_a_representation_assigned_from_the_phone_reaches_the_register(
        seeded, registrar):
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "C10", "attendee_name": "Nikhil Sharma",
                               "rep_type": "OWN"})
    assert response.status_code == 200
    assert response.json()["entitlement_count"] == 1

    register = registrar.get("/api/v1/registration/apartments").json()
    row = next(a for a in register["apartments"] if a["apartment_id"] == "C10")
    assert row["holder_name"] == "Nikhil Sharma"
    assert row["rep_type"] == "OWN"


def test_adding_a_second_apartment_raises_the_running_entitlement_count(
        seeded, registrar):
    first = registrar.post("/api/v1/registration/representations",
                           {"apartment_id": "C10", "attendee_name": "Nikhil Sharma",
                            "rep_type": "OWN"}).json()
    assert first["entitlement_count"] == 1

    second = registrar.post("/api/v1/registration/representations",
                            {"apartment_id": "C11", "attendee_name": "Nikhil Sharma",
                             "rep_type": "PROXY", "proxy_ref": "PROXY-C11"}).json()
    assert second["entitlement_count"] == 2
    assert second["attendee_id"] == first["attendee_id"]   # same person, not a duplicate


def test_the_configured_proxy_limit_is_enforced_from_this_surface_too(
        seeded, registrar):
    for unit in range(5, 10):        # C5..C9, five proxies, the configured maximum
        response = registrar.post(
            "/api/v1/registration/representations",
            {"apartment_id": f"C{unit}", "attendee_name": "Proxy Collector",
             "rep_type": "PROXY", "proxy_ref": f"PROXY-C{unit}"})
        assert response.status_code == 200, response.text

    response = registrar.post(
        "/api/v1/registration/representations",
        {"apartment_id": "C10", "attendee_name": "Proxy Collector",
         "rep_type": "PROXY", "proxy_ref": "PROXY-C10"})
    assert response.status_code == 409
    assert "maximum" in response.json()["message"]


def test_an_already_represented_apartment_needs_an_explicit_override(
        seeded, registrar):
    """A2 is held by proxy in the seed data."""
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "A2", "attendee_name": "Actual Owner",
                               "rep_type": "OWN"})
    assert response.status_code == 409
    # The page uses this field to decide whether to prompt for a reason.
    assert "existing_representation_id" in response.json()

    accepted = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "A2", "attendee_name": "Actual Owner",
                               "rep_type": "OWN",
                               "override_reason": "owner attended; proxy withdrawn"})
    assert accepted.status_code == 200


def test_an_ineligible_apartment_cannot_be_checked_in(seeded, registrar):
    from sgoa_vote.domain import entitlements

    with seeded.db.writer() as conn:
        entitlements.set_apartment_eligibility(conn, "C12", False, reason="dues in arrears")

    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "C12", "attendee_name": "Someone",
                               "rep_type": "OWN"})
    assert response.status_code == 409
    assert "not eligible" in response.json()["message"]


def test_the_register_payload_carries_what_the_phone_page_needs(seeded, registrar):
    """The page filters on these fields to build its apartment picker."""
    register = registrar.get("/api/v1/registration/apartments").json()
    sample = register["apartments"][0]
    for field in ("apartment_id", "eligible", "owner_display_name", "representation_id"):
        assert field in sample
    assert any(a["representation_id"] is None for a in register["apartments"])
    assert any(a["representation_id"] is not None for a in register["apartments"])


def test_revoking_from_the_desk_frees_the_apartment_for_check_in_again(
        seeded, registrar):
    rep = representation_id_for(seeded, "A2")
    assert registrar.post(f"/api/v1/registration/representations/{rep}/revoke",
                          {"reason": "proxy withdrawn"}).status_code == 200

    register = registrar.get("/api/v1/registration/apartments").json()
    row = next(a for a in register["apartments"] if a["apartment_id"] == "A2")
    assert row["representation_id"] is None      # reappears in the picker
