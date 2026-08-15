"""Creating a meeting: the demo AGM for rehearsal, and the real one for the day.

Both refuse to run against a database that already holds ballots -- the one
thing you must never do is overwrite a real meeting.
"""

from __future__ import annotations

import csv
import secrets
from pathlib import Path

from .domain import agm, auth, credentials, entitlements
from .domain import resolutions as resolutions_module
from .domain.errors import Conflict, ValidationError

DEMO_PASSWORD = "sgoa-demo"

OPERATORS = [
    ("admin", "ADMIN"),
    ("registration", "REGISTRATION"),
    ("mc", "MC"),
    ("scrutineer", "SCRUTINEER"),
]

BLOCKS = ("A", "B", "C")
UNITS_PER_BLOCK = 17

RESOLUTIONS = [
    ("R1", "Lift replacement",
     "That the Association approve the replacement of the Block A lift with the model and "
     "scope of work set out in the committee's proposal dated 2 August 2026, at a cost not "
     "exceeding the amount tabled at this meeting.",
     "FOR_GT_AGAINST"),
    ("R2", "External painting contract",
     "That the Association award the external painting contract for all three blocks to the "
     "contractor recommended by the committee, subject to the warranty terms tabled at this "
     "meeting.",
     "FOR_GT_AGAINST"),
    ("R3", "Annual budget and maintenance charge",
     "That the Association adopt the annual budget for the financial year 2026-27 as "
     "circulated, and set the monthly maintenance charge accordingly with effect from "
     "1 October 2026.",
     "FOR_GT_AGAINST"),
    ("R4", "Appointment of auditor",
     "That the Association appoint the auditor named in the agenda papers for the financial "
     "year 2026-27 on the terms circulated with the notice of this meeting.",
     "FOR_GT_AGAINST"),
    ("R5", "Amendment to the by-laws on common area use",
     "That the by-laws be amended as set out in Annexure 2 to the notice of this meeting, "
     "governing the use of common areas for private functions.",
     "TWO_THIRDS_OF_CAST"),
]

# name, [(apartment, OWN|PROXY)] -- one three-entitlement holder, one with two.
ATTENDEES = [
    ("Meera Raghavan",   [("A1", "OWN"), ("A2", "PROXY"), ("A3", "PROXY")]),
    ("Sunil Menon",      [("B1", "OWN"), ("B2", "PROXY")]),
    ("Kavitha Iyer",     [("A4", "OWN")]),
    ("Rajesh Nair",      [("A5", "OWN")]),
    ("Fatima Sheikh",    [("A6", "OWN")]),
    ("George Mathew",    [("B3", "OWN")]),
    ("Anjali Desai",     [("B4", "OWN")]),
    ("Prakash Rao",      [("B5", "OWN")]),
    ("Lakshmi Krishnan", [("C1", "OWN")]),
    ("Imran Qureshi",    [("C2", "OWN")]),
    ("Deepa Venkatesh",  [("C3", "OWN")]),
    ("Thomas Abraham",   [("C4", "PROXY")]),
]


def already_voted(conn) -> bool:
    return conn.execute("SELECT 1 FROM ballot.ballots LIMIT 1").fetchone() is not None


def seed(svc, *, title: str = "SGOA Annual General Meeting 2026",
         agm_date: str = "2026-09-20", location: str = "Community Hall, Shanti Gulmohar",
         password: str = DEMO_PASSWORD) -> dict:
    cfg = svc.config
    issued = []

    with svc.db.writer() as conn:
        if already_voted(conn):
            raise Conflict(
                "This database already contains accepted ballots. Seeding would corrupt a "
                "real meeting. Use a fresh --data-dir."
            )
        if agm.get(conn) is not None:
            raise Conflict("An AGM already exists in this data directory. Use a fresh --data-dir.")

        agm.create(conn, title, agm_date, location, cfg, is_demo=True)

        for username, role in OPERATORS:
            auth.create_operator(conn, username, role, password)

        for block in BLOCKS:
            for unit in range(1, UNITS_PER_BLOCK + 1):
                entitlements.add_apartment(
                    conn, f"{block}{unit}", owner_display_name=f"Owner {block}{unit}")

        agm.open_registration(conn)

        for number, title_text, wording, rule in RESOLUTIONS:
            resolutions_module.create_draft(conn, title_text, wording, number=number,
                                            voting_rule=rule,
                                            eligible_pool_id=cfg.eligible_pool_id)

        for name, holdings in ATTENDEES:
            attendee_id = entitlements.ensure_attendee(conn, name)
            for apartment_id, rep_type in holdings:
                entitlements.assign_representation(
                    conn, apartment_id, attendee_id, rep_type, cfg,
                    proxy_ref=f"PROXY-{apartment_id}" if rep_type == "PROXY" else None)
            count = entitlements.count_active_representations(conn, attendee_id)
            result = credentials.issue_credential(conn, svc.agm_key, attendee_id, count)
            issued.append({"name": name, "code": result["code"],
                           "entitlements": count,
                           "apartments": ", ".join(a for a, _ in holdings)})

    return {"agm": title, "operators": [u for u, _ in OPERATORS],
            "password": password, "credentials": issued,
            "apartments": len(BLOCKS) * UNITS_PER_BLOCK,
            "resolutions": len(RESOLUTIONS)}


# ---------------------------------------------------------------------------
# the real meeting
# ---------------------------------------------------------------------------

PRODUCTION_OPERATORS = [
    ("admin", "ADMIN"),
    ("registration", "REGISTRATION"),
    ("mc", "MC"),
    ("scrutineer1", "SCRUTINEER"),
    ("scrutineer2", "SCRUTINEER"),
]

# Same unambiguous alphabet as the voting codes, so a password read off a
# printed sheet is not misheard as a different character.
_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def strong_password() -> str:
    """Three readable groups, ~60 bits. Printed once, changeable afterwards."""
    return "-".join("".join(secrets.choice(_PW_ALPHABET) for _ in range(4))
                    for _ in range(3))


def parse_blocks(spec: str) -> list[dict]:
    """`A:17,B:17,C:17` -> A1..A17, B1..B17, C1..C17."""
    apartments = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValidationError(
                f"Block specification '{part}' should look like A:17 "
                "(block letter, colon, how many flats).")
        block, count = part.split(":", 1)
        if not count.strip().isdigit():
            raise ValidationError(f"'{count}' is not a number of flats.")
        for unit in range(1, int(count) + 1):
            apartments.append({"apartment_id": f"{block.strip().upper()}{unit}",
                               "owner_display_name": "", "eligible": True})
    return apartments


def load_apartments_csv(path: str | Path) -> list[dict]:
    """Columns: apartment_id, owner_name (optional), eligible (optional yes/no)."""
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"No apartment file at {path}.")

    apartments = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "apartment_id" not in [
                f.strip().lower() for f in reader.fieldnames]:
            raise ValidationError(
                "The apartment file needs a header row with an 'apartment_id' column.")
        for line_number, row in enumerate(reader, start=2):
            clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            apartment_id = clean.get("apartment_id", "").upper()
            if not apartment_id:
                continue
            eligible = clean.get("eligible", "yes").lower() not in ("no", "n", "0", "false")
            apartments.append({"apartment_id": apartment_id,
                               "owner_display_name": clean.get("owner_name", ""),
                               "eligible": eligible})
    if not apartments:
        raise ValidationError(f"No apartments were read from {path}.")

    seen = set()
    for entry in apartments:
        if entry["apartment_id"] in seen:
            raise ValidationError(
                f"Apartment {entry['apartment_id']} appears more than once in {path.name}.")
        seen.add(entry["apartment_id"])
    return apartments


RULE_ALIASES = {
    "": "FOR_GT_AGAINST",
    "simple": "FOR_GT_AGAINST",
    "ordinary": "FOR_GT_AGAINST",
    "majority": "FOR_GT_AGAINST",
    "for_gt_against": "FOR_GT_AGAINST",
    "two thirds": "TWO_THIRDS_OF_CAST",
    "two-thirds": "TWO_THIRDS_OF_CAST",
    "twothirds": "TWO_THIRDS_OF_CAST",
    "special": "TWO_THIRDS_OF_CAST",
    "two_thirds_of_cast": "TWO_THIRDS_OF_CAST",
    "all eligible": "MAJORITY_OF_ALL_ELIGIBLE",
    "majority of all eligible": "MAJORITY_OF_ALL_ELIGIBLE",
    "majority_of_all_eligible": "MAJORITY_OF_ALL_ELIGIBLE",
}

_TEXT_COLUMNS = ("full_text", "text", "wording", "resolution", "resolution_text")
_TITLE_COLUMNS = ("title", "short_title", "subject")
_RULE_COLUMNS = ("voting_rule", "rule", "majority")
_NUMBER_COLUMNS = ("number", "resolution_number", "no", "num")
_KNOWN_COLUMNS = set(_TEXT_COLUMNS + _TITLE_COLUMNS + _RULE_COLUMNS + _NUMBER_COLUMNS)


def _derive_title(text: str) -> str:
    """A short handle for the MC console, taken from the wording itself.

    Nearly every resolution opens "That the Association ...", which would make
    every derived title start the same way and tell the MC nothing, so that
    opening is dropped before the first few words are taken.
    """
    first = text.replace("\n", " ").strip()
    for stop in (". ", "; "):
        if stop in first[:90]:
            first = first.split(stop)[0]
            break

    lowered = first.lower()
    for prefix in ("that the association ", "that the committee ", "that the ", "that "):
        if lowered.startswith(prefix):
            first = first[len(prefix):]
            break

    words = first.split()
    title = " ".join(words[:8]).strip(" ,;:")
    if title:
        title = title[0].upper() + title[1:]
    return (title[:70] + "...") if len(title) > 70 else (title or "Untitled resolution")


def ignored_apartment_columns(path: str | Path) -> list[str]:
    """Columns present in the file that the importer does not use.

    Worth reporting: a committee that put a `code` column in the sheet expects
    those codes to be used, and silently dropping them would be discovered at
    the registration desk on the day.
    """
    known = {"apartment_id", "owner_name", "eligible"}
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle), [])
    return [cell.strip() for cell in header
            if cell and cell.strip().lower().replace(" ", "_") not in known]


def _normalise_rule(value: str) -> str:
    key = (value or "").strip().lower()
    if key in RULE_ALIASES:
        return RULE_ALIASES[key]
    raise ValidationError(
        f"'{value}' is not a voting rule. Use one of: blank or 'simple' for "
        "FOR > AGAINST, 'two-thirds', or 'majority of all eligible'.")


def load_resolutions_csv(path: str | Path) -> list[dict]:
    """Read the agenda.

    The simplest accepted file is one resolution per row in a single column,
    which is what a committee typing an agenda into a spreadsheet produces.
    A header row unlocks the optional columns: number, title, voting_rule.
    """
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"No resolutions file at {path}.")

    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.reader(handle) if any((cell or "").strip() for cell in row)]
    if not rows:
        raise ValidationError(f"No resolutions were read from {path}.")

    header = [(cell or "").strip().lower().replace(" ", "_") for cell in rows[0]]
    has_header = bool(set(header) & _KNOWN_COLUMNS)
    body = rows[1:] if has_header else rows

    def column(names, row):
        for name in names:
            if name in header:
                index = header.index(name)
                if index < len(row):
                    return (row[index] or "").strip()
        return ""

    resolutions = []
    for row in body:
        if has_header:
            text = column(_TEXT_COLUMNS, row)
            title = column(_TITLE_COLUMNS, row)
            rule = _normalise_rule(column(_RULE_COLUMNS, row))
            number = column(_NUMBER_COLUMNS, row).upper() or None
            # A sheet with only a title column and no separate wording still works.
            if not text and title:
                text, title = title, ""
        else:
            # A headerless agenda is one resolution per line. Resolution wording
            # is full of commas -- "approve the budget, as circulated" -- and if
            # the cell was not quoted, csv.reader will have split the line into
            # several fields. Rejoining restores the wording exactly; a properly
            # quoted cell arrives as one field and is unaffected either way.
            # The wording is what gets hashed and voted on, so a quiet
            # truncation here would put different words to the meeting than the
            # committee approved.
            text = ",".join(row).strip()
            title, rule, number = "", "FOR_GT_AGAINST", None

        if not text:
            continue
        resolutions.append({"number": number, "title": title or _derive_title(text),
                            "full_text": text, "voting_rule": rule})

    if not resolutions:
        raise ValidationError(
            f"No resolution wording was found in {path.name}. Put one resolution per "
            "row, with the exact wording in the first column.")

    seen = set()
    for entry in resolutions:
        if entry["number"] and entry["number"] in seen:
            raise ValidationError(
                f"Resolution number {entry['number']} appears more than once.")
        seen.add(entry["number"])
    return resolutions


def init_agm(svc, *, title: str, agm_date: str, location: str,
             apartments: list[dict], resolutions: list[dict] | None = None) -> dict:
    """Create the real meeting: AGM record, apartment register, operator accounts.

    Deliberately does NOT open registration. The administrator does that on the
    day, so the audit trail carries the real time the desk opened.
    """
    cfg = svc.config
    accounts = []

    with svc.db.writer() as conn:
        if already_voted(conn):
            raise Conflict("This database already contains accepted ballots.")
        if agm.get(conn) is not None:
            raise Conflict(
                "An AGM already exists in this data directory. Use a fresh --data-dir "
                "for a separate meeting.")

        agm.create(conn, title, agm_date, location, cfg, is_demo=False)

        for entry in apartments:
            entitlements.add_apartment(conn, entry["apartment_id"],
                                       owner_display_name=entry["owner_display_name"],
                                       eligible=entry["eligible"])

        for username, role in PRODUCTION_OPERATORS:
            password = strong_password()
            auth.create_operator(conn, username, role, password)
            accounts.append({"username": username, "role": role, "password": password})

        # Every resolution starts as a DRAFT. The MC still finalizes each one on
        # the day, which is what freezes the wording and produces its hash.
        for entry in (resolutions or []):
            resolutions_module.create_draft(
                conn, entry["title"], entry["full_text"], number=entry["number"],
                voting_rule=entry["voting_rule"], eligible_pool_id=cfg.eligible_pool_id)

    eligible = sum(1 for a in apartments if a["eligible"])
    return {"title": title, "date": agm_date, "location": location,
            "apartments": len(apartments), "eligible": eligible,
            "resolutions": resolutions or [],
            "operators": accounts, "config_hash": cfg.config_hash()}


def print_init_summary(result: dict) -> None:
    print()
    print("=" * 74)
    print(f"  AGM CREATED: {result['title']}")
    print("=" * 74)
    print(f"  {result['date']}   {result['location']}")
    print(f"  {result['apartments']} apartments on the register, "
          f"{result['eligible']} eligible to vote")
    if result["resolutions"]:
        print(f"  {len(result['resolutions'])} resolutions loaded as drafts:")
        for entry in result["resolutions"]:
            rule = "" if entry["voting_rule"] == "FOR_GT_AGAINST" \
                else f"   [{entry['voting_rule']}]"
            print(f"    {entry['number'] or '(auto)':<5} {entry['title']}{rule}")
        print("    Check the exact wording on the MC console before the meeting.")
    else:
        print("  No resolutions loaded. Enter them on the MC console, or re-run")
        print("  with --resolutions agenda.csv.")
    print()
    print("  OPERATOR ACCOUNTS -- write these down now. They are shown once.")
    print(f"  {'USERNAME':<16}{'ROLE':<16}PASSWORD")
    print("  " + "-" * 62)
    for account in result["operators"]:
        print(f"  {account['username']:<16}{account['role']:<16}{account['password']}")
    print()
    print("  Change any of them with:  python -m sgoa_vote set-password <user> <password>")
    print()
    print("  CONFIGURATION HASH -- both scrutineers should record this now:")
    print(f"  {result['config_hash']}")
    print()
    print("  Next: python -m sgoa_vote run, sign in as admin, enter the resolutions")
    print("        as drafts on the MC console, then rehearse before the meeting.")
    print("=" * 74)
    print()


def print_summary(result: dict) -> None:
    print()
    print("=" * 72)
    print(f"  DEMO AGM SEEDED: {result['agm']}")
    print("=" * 72)
    print(f"  {result['apartments']} apartments, {result['resolutions']} draft resolutions")
    print()
    print(f"  Operator accounts (password: {result['password']})")
    for username in result["operators"]:
        print(f"    {username}")
    print()
    print("  Voting codes -- in a real meeting these are printed and handed over privately")
    print(f"  {'CODE':<8}{'VOTES':<7}{'ATTENDEE':<20}APARTMENTS")
    print("  " + "-" * 62)
    for row in result["credentials"]:
        print(f"  {row['code']:<8}{row['entitlements']:<7}{row['name']:<20}{row['apartments']}")
    print()
    print("  Start the server with:  python -m sgoa_vote run")
    print("=" * 72)
    print()
