# WORK ORDER — Build the SGOA AGM Voting System (runs on localhost)

**How to use this file:** paste it in full as a single prompt to a coding agent, or hand it to a
developer as the implementation brief. It is self-contained — it restates everything needed from
"SGOA AGM Voting System, Software Specification v1.0" (Shanti Gulmohar Owners Association, 14 Aug 2026).
Every open decision in that specification has already been resolved in §2 below. **Do not ask
clarifying questions. Build exactly what is written here.**

---

## 0. What you are building, in one paragraph

A local electronic voting system for an apartment-association AGM. About 30 attendees representing up
to 51 apartments vote on a sequence of resolutions. Each attendee holds one voting entitlement per
apartment they represent — their own plus any proxies — so one person may cast 1, 2, 3 or more votes
and may split them across FOR / AGAINST / ABSTAIN. Attendees join on their own phones using a short
private code issued at a registration desk. An MC drives the meeting from a console: finalize the
wording, open voting, close voting, show the result, move on. The system must prove afterwards that
every counted ballot came from a valid unused entitlement — while making it **impossible** to determine
how any particular apartment voted.

---

## 1. The four invariants

These outrank every other instruction in this document. If any later requirement seems to conflict with
one of these, the invariant wins and you flag the conflict in the README.

| # | Invariant | What it forces in the build |
|---|---|---|
| I1 | **Eligibility is identifiable** | Registration can prove which apartments were represented and by whom, including proxy authority. |
| I2 | **Voting is anonymous** | A ballot row contains resolution + choice + a random ballot ID. Nothing else. Ever. |
| I3 | **Every entitlement is accounted for** | Per resolution: `eligible = cast + not_cast` **and** `cast = FOR + AGAINST + ABSTAIN`. Both must hold or no result is published. |
| I4 | **Ballots are immutable** | Ballot rows are append-only, enforced in the database itself, not merely in application code. |

The system must be able to prove *"31 valid entitlements were consumed and exactly 31 ballots were
accepted"* and must be **unable** to prove *"ballot 8843 came from code KTR7."* That inability is a
feature; do not add anything that erodes it, including "helpful" debug logging.

---

## 2. Decisions register — every judgment call, already made

The source specification leaves these open. They are settled here. Implement as stated; make items
marked *(config)* runtime-configurable with the given default.

| Area | Decision |
|---|---|
| Deployment target | Single machine, `uvicorn` bound to `0.0.0.0:8000`, reachable at `http://localhost:8000`. |
| Transport security | Plain HTTP. **No TLS in this build.** The spec's WPA3 / static-IP / local-DNS / certificate setup is AGM-day physical deployment; document it in the README, do not implement it. Set the session cookie `Secure` flag from config so enabling HTTPS later is a one-line change. |
| Language / framework | Python 3.12+, FastAPI, Uvicorn. |
| UI | Server-rendered Jinja2 + htmx (vendored into `static/`, never from a CDN) + a little vanilla JS. No React, no npm, no build step. |
| Storage | SQLite 3 in WAL mode, **three separate files**: `data/eligibility.db`, `data/ballot.db`, `data/audit.db`. |
| Cross-database atomicity | One dedicated writer connection opens `eligibility.db` and `ATTACH`es `ballot.db` and `audit.db`. A vote is then a single SQLite transaction spanning all three. Serialize all writes through this one connection behind a `threading.Lock`; at 100 sessions this is ample and removes every SQLite-locking failure mode. Readers use separate read-only connections. |
| Live updates | Client polling every 2 seconds. **No WebSockets, no SSE.** Simpler and more robust on flaky Wi-Fi. |
| Password hashing | `hashlib.scrypt` (stdlib) with per-user salt. Avoids a native-dependency install on the AGM laptop. |
| Voter code storage | HMAC-SHA-256 of the code under a per-AGM server key held in `data/agm_key`. Plaintext codes exist only in the HTTP response that prints the credential card. |
| Credential format | 3 uppercase letters + 1 digit, e.g. `KTR7`. Letter alphabet excludes `I`, `O`; digit alphabet excludes `0`, `1`. Generated with `secrets`, unique within the AGM. |
| PDF generation | `reportlab`. Pure Python, installs cleanly on Windows, no wkhtmltopdf/WeasyPrint native deps. Emit HTML alongside the PDF. |
| Timestamps | Store UTC ISO-8601 with `Z`. Display in Asia/Kolkata. |
| Voter session lifetime | 12 hours *(config)*, i.e. longer than any AGM. |
| Sessions per credential | Exactly 1 active *(config)*. A second join is refused with "This code is already in use on another device. Please visit the voting assistance desk." |
| Join rate limit | 5 failed attempts per client IP within 60 s → 60 s cooldown + `INVALID_CREDENTIAL_THRESHOLD` audit event *(config)*. |
| Max proxies per attendee | Default 5 *(config)*. The bylaws figure is a governance decision; surface it prominently in admin config and in the final report so SGOA can set it before production. |
| Eligible pool | One pool for all resolutions *(config)*, with `eligible_pool_id` carried in the schema so per-resolution pools can be added later. |
| Chair casting vote | **Disabled.** A tie reports `TIED` and offers a free-text disposition note. The software never invents an outcome. |
| Special majority rules | Rules engine supports `FOR_GT_AGAINST` (default), `TWO_THIRDS_OF_CAST`, `MAJORITY_OF_ALL_ELIGIBLE`. Selected explicitly per resolution by an operator; **never** inferred from resolution wording. |
| Operator inactivity lock | 15 minutes *(config)*. Re-authentication required for AGM finalization, database restore, and reset-all. |
| Not-cast accounting | `not_cast = eligible − consumed`, computed at close from the ledger. Never folded into ABSTAIN. |
| Demo credentials | Seeded operator accounts use password `sgoa-demo`, are watermarked DEMO in the UI, and a CLI subcommand sets real passwords. Seeding refuses to run if any ballot exists. |

---

## 3. Project layout

```
sgoa-vote/
  sgoa_vote/
    __main__.py             # CLI: run | seed | set-password | verify-audit | backup
    app.py                  # FastAPI factory, routers, middleware, CSRF, error pages
    config.py               # AGM config load, freeze, config_hash
    db/
      connections.py        # WAL pragmas, ATTACH writer, writer lock, read pool
      schema_eligibility.sql
      schema_ballot.sql     # includes immutability triggers
      schema_audit.sql
    domain/
      credentials.py        # generation, HMAC hashing, activation, reset
      entitlements.py       # representations, ledger, revocation
      resolutions.py        # state machine, canonical text hashing, amendments
      voting.py             # the atomic submission algorithm (§6.1)
      results.py            # rules engine + reconciliation
      audit.py              # hash chain: append, verify, checkpoint
      reports.py            # final report HTML+PDF, certification bundle, checksums
    api/
      voter.py  registration.py  admin.py  health.py
    web/
      templates/            # voter, mc, registration, scrutineer, projector, auth
      static/               # vendored htmx + css. Nothing loaded from the internet.
    seed.py
  tests/
    unit/  integration/  privacy/  acceptance/
  requirements.txt          # exact-pinned (==) versions
  README.md
```

---

## 4. Data model

### 4.1 `eligibility.db`

- `agms(agm_id, title, agm_date, location, status, config_json, config_hash, created_at)`
  status ∈ `SETUP | REGISTRATION_OPEN | VOTING_IN_PROGRESS | FINALIZED | ARCHIVED`
- `apartments(apartment_id TEXT PK, eligible INT, owner_display_name, eligibility_notes, eligibility_version INT, created_at, updated_at)`
- `attendees(attendee_id TEXT PK, display_name, created_at)`
- `representations(representation_id, apartment_id, attendee_id, rep_type OWN|PROXY, proxy_ref, status ACTIVE|REVOKED|SUPERSEDED, created_at)`
  → **`CREATE UNIQUE INDEX ... ON representations(apartment_id) WHERE status='ACTIVE'`** — this is AC-01, enforced by the database, not by a code check.
- `credentials(credential_id TEXT PK, code_hash, attendee_id, status CREATED|ACTIVE|SUSPENDED|REPLACED|CLOSED, entitlement_count INT, issued_at)`
- `credential_resolution_ledger(credential_id, resolution_id, eligible_count, consumed_count, last_submission_id, last_receipt_id, PRIMARY KEY(credential_id, resolution_id))`
  → the anti-overvote **and** idempotency record (AC-02, AC-05).
- `sessions(session_id_hash TEXT PK, credential_id, created_at, expires_at)`
- `operators(operator_id, username UNIQUE, role ADMIN|REGISTRATION|MC|SCRUTINEER, password_hash, salt, created_at)`
- `registration_events(event_id, event_type, subject_id, operator_id, reason, timestamp)` — append-only

### 4.2 `ballot.db`

- `resolutions(resolution_id TEXT PK, number, version INT, title, full_text, text_hash, status, voting_rule, eligible_pool_id, finalized_at, finalized_by, opened_at, closed_at, disposition_note, superseded_by)`
  status ∈ `DRAFT | FINALIZED | VOTING_OPEN | VOTING_CLOSED | PASSED | FAILED | TIED | WITHDRAWN | NOT_PUT_TO_VOTE | RECONCILIATION_ERROR`
- `ballots(ballot_id TEXT PK, resolution_id TEXT, choice TEXT CHECK(choice IN ('FOR','AGAINST','ABSTAIN')), accepted_at TEXT)`
  `ballot_id` is `uuid4()`. **Four columns. Nothing else, now or later.** Plus:
  ```sql
  CREATE TRIGGER ballots_no_update BEFORE UPDATE ON ballots
    BEGIN SELECT RAISE(ABORT, 'ballots are immutable'); END;
  CREATE TRIGGER ballots_no_delete BEFORE DELETE ON ballots
    BEGIN SELECT RAISE(ABORT, 'ballots are immutable'); END;
  ```
- `result_snapshots(resolution_id PK, for_count, against_count, abstain_count, cast_count, not_cast_count, eligible_count, outcome, rule_applied, snapshot_hash, created_at)` — same pair of immutability triggers.
- `resolution_events(event_id, resolution_id, event_type, operator_id, timestamp, previous_hash)`

### 4.3 `audit.db`

- `audit_log(sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp, actor_role, actor_id, event_type, payload_json, prev_hash, event_hash)`
  `event_hash = SHA256(prev_hash || sequence || timestamp || event_type || canonical_json(payload))`,
  genesis `prev_hash = "0"*64`. Canonical JSON = sorted keys, no spaces, UTF-8.
- `audit_checkpoints(checkpoint_id, head_sequence, head_hash, label, created_at)` — written at AGM start, after every resolution close, and at finalization.

Event types: `AGM_CREATED, AGM_CONFIG_FROZEN, REGISTRATION_OPENED, REPRESENTATION_ASSIGNED,
REPRESENTATION_REVOKED, CREDENTIAL_ISSUED, CREDENTIAL_RESET, INVALID_CREDENTIAL_THRESHOLD,
RESOLUTION_CREATED, RESOLUTION_EDITED, RESOLUTION_FINALIZED, RESOLUTION_AMENDED, VOTING_OPENED,
ENTITLEMENTS_CONSUMED, VOTING_CLOSED, RESULT_SNAPSHOT_CREATED, RESOLUTION_WITHDRAWN,
RESOLUTION_NOT_PUT_TO_VOTE, DISPOSITION_NOTE_RECORDED, BACKUP_CREATED, AUDIT_CHECKPOINT_CREATED,
AGM_FINALIZED, FINAL_REPORT_GENERATED`.

### 4.4 Banned from ballot rows and from every audit payload

Apartment ID, attendee name or ID, credential ID or code, session ID, device fingerprint, IP or MAC
address, proxy source apartment, or any composite token that maps back to an attendee.
`ENTITLEMENTS_CONSUMED` is aggregate-only: `{"resolution_id": "R4", "count": 3}` — no credential reference.
A test enforces this (§9, privacy gates).

---

## 5. Roles

| Role | Can do | Must never see |
|---|---|---|
| ADMIN | setup, config, backups, restore, operator accounts | ballot choices |
| REGISTRATION | eligibility, check-in, representations, credential issue/reset | results while voting is open |
| MC | resolution lifecycle, open/close, show result | interim FOR/AGAINST/ABSTAIN totals |
| SCRUTINEER | read-only reconciliation, audit verification, final report | credential-to-choice linkage (which does not exist) |
| VOTER | current resolution, own entitlement count, own session | everything else |

---

## 6. Core algorithms

### 6.1 Atomic vote submission

One transaction on the writer connection:

```
BEGIN IMMEDIATE
 1. Authenticate session cookie → credential. Reject if session expired or credential not ACTIVE.
 2. Verify resolution.status == VOTING_OPEN.
 3. Verify client-supplied resolution_version and resolution_hash match the current row.
    (This is how a stale phone page is caught. Reject with a re-fetch instruction.)
 4. SELECT the ledger row for (credential_id, resolution_id).
    If last_submission_id == client_submission_id → COMMIT nothing, return the ORIGINAL receipt.
 5. remaining = eligible_count - consumed_count
 6. Validate allocation: all three values are non-negative ints;
    0 < FOR+AGAINST+ABSTAIN <= remaining.
 7. Generate N = sum cryptographically random one-time tokens IN MEMORY (secrets.token_bytes(32)).
 8. For each token: validate-once, then INSERT one ballot row (uuid4, resolution_id, choice, now).
 9. UPDATE ledger: consumed_count += N, last_submission_id, last_receipt_id.
10. INSERT aggregate audit event ENTITLEMENTS_CONSUMED {resolution_id, count: N}.
COMMIT              -- PRAGMA synchronous=FULL for this transaction
-- tokens go out of scope here; the token↔credential mapping is never written anywhere
```

Any failure rolls the whole thing back. A crash must never leave consumed entitlements without ballots,
or ballots without consumed entitlements (AC-03). The receipt is `VOTE-` + 6 uppercase hex chars; it
proves consumption and reveals nothing about choices.

### 6.2 Resolution state machine

```
DRAFT --finalize--> FINALIZED --open--> VOTING_OPEN --close--> VOTING_CLOSED --> PASSED|FAILED|TIED
                       |                                                    \--> RECONCILIATION_ERROR
                       +--> WITHDRAWN
                       +--> NOT_PUT_TO_VOTE
```

- DRAFT text edits freely, with revision history.
- FINALIZE computes `text_hash = SHA256(canonical(full_text) + "|" + str(version))` where canonical =
  NFC-normalize, `\r\n`→`\n`, strip trailing whitespace per line, strip leading/trailing blank lines.
- AMEND (allowed only FINALIZED, before opening) creates version+1; the prior version stays visible and
  is marked superseded. Numbering is never reused, never renumbered (AC-11).
- Once the first ballot is accepted, the text is frozen permanently. The only path is close/void by
  explicit governance action plus a new resolution — never an in-place edit.
- Illegal transitions return HTTP 409 with a plain-language message.

### 6.3 Result calculation and reconciliation

On CLOSE: recount from the `ballots` table itself (never from a running counter), compute
`not_cast = eligible − consumed`, then check both equalities of invariant I3. Only if both pass, apply
the resolution's rule:

```
FOR_GT_AGAINST (default):  FOR > AGAINST -> PASSED ; FOR < AGAINST -> FAILED ; equal -> TIED
TWO_THIRDS_OF_CAST:        FOR >= ceil(2/3 * (FOR+AGAINST)) -> PASSED else FAILED
MAJORITY_OF_ALL_ELIGIBLE:  FOR > eligible/2 -> PASSED else FAILED
```

ABSTAIN is always reported but excluded from the FOR-vs-AGAINST comparison. NOT CAST is reported
separately and never converted to ABSTAIN. Write an immutable `result_snapshot` with `snapshot_hash`,
append `VOTING_CLOSED` + `RESULT_SNAPSHOT_CREATED`, and create an audit checkpoint.

If either equality fails: status becomes `RECONCILIATION_ERROR`, **no PASSED/FAILED is ever displayed**,
and a critical banner appears on the MC and scrutineer consoles blocking certification until resolved
through a documented procedure (AC-08).

### 6.4 Credentials, sessions, conflicts

- Issue: registration operator selects/creates the attendee, assigns every validated representation, then
  issues one credential carrying the total entitlement count. The system renders a printable card view
  (code at ≥24pt, high contrast, optional QR) — this is the only time the plaintext code exists.
- Reset: invalidates the session and issues a new code bound to the **same ledger rows**. Consumed
  entitlements are never restored (AC-13).
- Revocation: revokes only *future unused* entitlements — it decrements `eligible_count` on ledger rows
  for resolutions not yet opened. Already-accepted anonymous ballots are never deleted or reassigned.
- Owner/proxy conflict: if an apartment already has an ACTIVE representation, a second assignment is
  blocked. Resolving it requires an authenticated operator override with a mandatory typed reason, which
  supersedes the old representation and append-logs both events.

---

## 7. HTTP API

**Voter** — `POST /api/v1/voter/join` · `GET /api/v1/voter/state` · `GET /api/v1/resolutions/active` ·
`POST /api/v1/resolutions/{id}/vote/preview` (pure validation, zero state change) ·
`POST /api/v1/resolutions/{id}/vote` · `POST /api/v1/voter/logout` (ends the session, never touches the ledger).

**Registration** — `GET /api/v1/registration/apartments` ·
`POST /api/v1/registration/representations` · `POST /api/v1/registration/representations/{id}/revoke` ·
`POST /api/v1/registration/credentials/issue` · `POST /api/v1/registration/credentials/{id}/reset`.

**MC / Admin** — `POST /api/v1/admin/resolutions` · `PATCH /api/v1/admin/resolutions/{id}` (DRAFT only) ·
`.../finalize` · `.../amend` · `.../open` · `.../close` · `.../withdraw` · `.../not-put-to-vote` ·
`.../disposition` · `GET /api/v1/admin/participation/{id}` (eligible / cast / not-cast **only**) ·
`GET /api/v1/admin/results/{id}` · `POST /api/v1/admin/reports/final` · `POST /api/v1/admin/backup`.

> `GET /api/v1/admin/results/{id}` **returns HTTP 403 while the resolution is VOTING_OPEN**, for every
> role including ADMIN. Enforce it in the endpoint, not in the template. This is AC-07 and there is a
> test that calls the API directly.

**Health** — `GET /api/v1/health` (admin-authenticated): server up, `PRAGMA integrity_check` on all three
DBs, WAL status, free disk space, AGM state, active session count. No ballot data.

Request/response shapes:

```jsonc
// POST /api/v1/resolutions/R4/vote
{ "client_submission_id": "8d1d6a9c-...", "resolution_version": 2,
  "resolution_hash": "sha256:...", "allocation": {"FOR": 2, "AGAINST": 1, "ABSTAIN": 0},
  "confirmed": true }

// 200 OK
{ "status": "RECORDED", "resolution": "R4", "entitlements_recorded": 3,
  "remaining_entitlements": 0, "receipt_id": "VOTE-7B31E2" }
```

Cross-cutting: parameterized SQL only; CSRF tokens on every state-changing operator form; Jinja
autoescaping on for all user-supplied text (resolution wording, owner names, disposition notes);
`HttpOnly; SameSite=Strict` session cookies; no ballot choices and no raw credential codes in any log line.

---

## 8. The five web surfaces

**1. Voter — `/`** (the only one seniors touch; get this right)
Join → waiting → vote → confirm → recorded. Screens, near-verbatim:

```
SGOA AGM VOTING                     RESOLUTION R4
Enter your private voting code      Approve the lift replacement proposal as presented?
      [ K T R 7 ]                   You have 3 votes.
      [ JOIN AGM ]                  [ FOR ] [ AGAINST ] [ ABSTAIN ]
Need help? Please visit the         [ Vote individually instead ]
voting assistance desk.
                                    CONFIRM YOUR VOTE
You are connected.                  You are casting 3 votes FOR.
You have 3 voting entitlements.     [ CONFIRM 3 VOTES FOR ]  [ GO BACK ]
Please wait for the next
resolution.                         VOTE RECORDED
Current item: R4 — discussion       3 voting entitlements were recorded for Resolution R4.
in progress                         You cannot change this vote after confirmation.
```

Mixed allocation ("Vote individually instead") shows ± steppers per choice with a live
`Allocated: 3 of 3` and a CONTINUE that is disabled until the total is valid, then the same
two-step confirmation. **A choice is never committed on the first tap.** Polls
`/api/v1/voter/state` every 2 s and moves itself between screens as the MC opens and closes voting.

Accessibility, non-negotiable: body text ≥18px, resolution text 18–22px, button labels ≥22px, touch
targets ≥48px (aim 56–64px) including the ± steppers, WCAG AA contrast, choices always spelled out as
text (never color alone), no auto-submitting timers, no swipe or hidden gestures, no scrolling needed
for an ordinary resolution on a common phone. The confirmation screen repeats the resolution number,
the choice and the vote count. Every error message ends with a concrete next step — default:
"Please visit the voting assistance desk."

**2. MC console — `/mc`** — one linear strip: `EDIT → FINALIZE → OPEN VOTING → CLOSE VOTING → SHOW RESULT
→ NEXT RESOLUTION`. While open it shows *only* participation:

```
R4 — VOTING OPEN
Eligible entitlements  36     Votes received  29     Not yet cast  7
[███████████████░░░]
Interim FOR / AGAINST / ABSTAIN totals are HIDDEN.
[ CLOSE VOTING ]
```

Result and tie screens per §6.3, with `[ RECORD DISPOSITION NOTE ]` on a tie and the line
"No automatic outcome has been applied."

**3. Registration — `/registration`** — apartment list with eligibility and check-in state; assign OWN or
PROXY; conflict override with mandatory reason; issue credential → printable card view; reset credential.

**4. Scrutineer — `/scrutineer`** — read-only. Per-resolution reconciliation table (the Appendix-C layout:
eligible / consumed / ballot rows / not cast / FOR / AGAINST / ABSTAIN with explicit PASS-FAIL checks),
a **Verify audit chain** button that re-hashes the entire chain and names the first broken sequence
number if any, checkpoint history, and final results.

**5. Projector — `/projector`** — full-screen, no controls: current resolution wording in large type,
then the result table once the MC shows it.

Operator login guards surfaces 2–4 with role enforcement and a 15-minute inactivity lock.

---

## 9. Tests — these are release gates

**Unit:** pass/fail/tie for all three rules · abstain and not-cast kept separate · allocation arithmetic
(reject negatives, zero total, over-remaining) · credential uniqueness and excluded characters
(`I O 0 1` never appear across 10,000 generations) · every legal and illegal state transition ·
hash-chain compute and verify · canonical text hashing stable across whitespace and unicode variants.

**Integration:** join → open → preview → confirm → exactly N ballot rows + ledger consumption ·
3-entitlement holder all FOR · 3-entitlement holder 2 FOR + 1 AGAINST · same `client_submission_id`
retried after simulated drop → still N ballots, original receipt returned · vote after full consumption
→ clean refusal · vote with stale version/hash → refusal with re-fetch · close while a client sits on an
open page · restart the app against the same DB files mid-open-resolution and confirm state survives ·
credential reset after partial voting · revoke affects future resolutions only · amend a finalized
resolution before opening.

**Privacy and security gates:**
- Introspect `ballot.db` schema *and* dump every row; fail if any apartment, attendee, credential or
  session identifier appears anywhere (AC-04). Do the same over every `audit_log.payload_json`.
- `GET /api/v1/admin/results/{id}` as ADMIN while VOTING_OPEN → 403 (AC-07).
- `UPDATE ballots ...` and `DELETE FROM ballots ...` both raise (AC-06).
- 10 rapid bad join attempts → rate limited, audit event written.
- Mutate one `audit_log` payload → chain verification fails and names that sequence.
- SQL-injection and XSS payloads in resolution text, owner names and disposition notes are stored
  literally and rendered inert.
- Admin POST without a CSRF token → rejected.

**Acceptance:** one scripted end-to-end mock AGM — ≥10 credentials including a 3-entitlement holder, one
credential reset, one abstention, one deliberate non-vote, one exact tie, one withdrawn resolution, one
amended resolution, one simulated retry — reconciled against a hardcoded answer key, ending in a
generated certification bundle whose checksums verify. Every criterion below needs at least one
asserting test named `test_ac01_…` through `test_ac15_…`.

| ID | Acceptance criterion |
|---|---|
| AC-01 | Exactly one active representation per apartment. |
| AC-02 | A credential cannot consume more entitlements than assigned for any resolution. |
| AC-03 | A successful submission creates exactly N ballots and consumes exactly N entitlements, atomically. |
| AC-04 | No persistent table or log maps a ballot choice back to an apartment, attendee or credential. |
| AC-05 | Repeated submission with the same idempotency key creates no extra ballots. |
| AC-06 | Ballot rows cannot be updated or deleted through the application. |
| AC-07 | The MC cannot view FOR/AGAINST/ABSTAIN totals until voting closes. |
| AC-08 | Results satisfy `eligible = cast + not_cast` and `cast = FOR + AGAINST + ABSTAIN`. |
| AC-09 | Default rule is FOR > AGAINST ⇒ PASSED; equality ⇒ TIED. |
| AC-10 | The finalized text/version/hash voters saw matches the final report. |
| AC-11 | WITHDRAWN and NOT_PUT_TO_VOTE resolutions stay visible and keep their numbering and history. |
| AC-12 | Core voting works with the network interface disconnected. |
| AC-13 | A credential reset never restores already-consumed votes. |
| AC-14 | The final report validates against database hashes and the audit-chain head. |
| AC-15 | A first-time user can join and cast a confirmed vote unaided after basic verbal instruction. *(manual, scripted in README)* |

---

## 10. Final report and certification bundle

`POST /api/v1/admin/reports/final` — permitted only when registration is closed and no resolution is
VOTING_OPEN; requires operator re-authentication. Produces:

```
export/SGOA-AGM-<date>-FINAL/
  final_report.pdf   final_report.html
  eligibility.db  ballot.db  audit.db
  manifest.json   checksums.sha256   software_version.txt
  configuration_export.json   audit_chain_verification.txt   README_ARCHIVE.txt
```

Report contents: **summary** (AGM title/date/location, eligible apartments, apartments represented,
in-person vs proxy representations, total active entitlements, configuration hash, software build
checksum); **per resolution** (number, title, exact finalized wording, version, SHA-256, voting rule,
open/close timestamps, eligible, FOR, AGAINST, ABSTAIN, NOT CAST, outcome, any disposition note);
**certification footer** verbatim:

```
We certify that the above aggregate results reconcile with the
immutable electronic ballot records and eligibility ledger generated
during the AGM. The system does not retain a voter-to-ballot-choice link.

Scrutineer 1: __________________  Signature: __________  Date: ______
Scrutineer 2: __________________  Signature: __________  Date: ______
Final audit-chain head: SHA256: ______________________________
Ballot database hash:   SHA256: ______________________________
Eligibility DB hash:    SHA256: ______________________________
Software build hash:    SHA256: ______________________________
```

Generating it appends `AGM_FINALIZED` and `FINAL_REPORT_GENERATED` and writes the closing checkpoint.
`POST /api/v1/admin/backup` checkpoints WAL (`TRUNCATE`), copies all three DBs to `backups/<timestamp>/`,
and logs `BACKUP_CREATED`; expose it as a button on the admin surface.

---

## 11. Seed data

`python -m sgoa_vote seed` builds a demo AGM: 51 apartments (`A1–A17`, `B1–B17`, `C1–C17`), operator
accounts `admin` / `registration` / `mc` / `scrutineer` (password `sgoa-demo`), and five draft
resolutions — lift replacement, painting contract, annual budget, appointment of auditor, and a by-law
amendment preset to `TWO_THIRDS_OF_CAST` so the special-rule path is exercised. Twelve attendees are
pre-checked-in with codes printed to the console, including one holding 3 entitlements (own + 2 proxies)
and one holding 2. Every page carries a `DEMO DATA` watermark while the demo AGM is active. Seeding
aborts if any ballot row exists.

---

## 12. Build order

Follow this phase order, keeping the app runnable and tested at the end of each:

1. **P1 core loop** — server, schema, join, one resolution, single entitlement, close, result.
2. **P2 proxy + privacy** — multi-entitlement allocation, anonymous ballot store, consumption ledger, idempotency, reconciliation.
3. **P3 MC + registration** — full eligibility/proxy UI, credential issue and reset, complete resolution lifecycle.
4. **P4 audit + security** — hash chain, immutability triggers, role controls, backups, final report and bundle.
5. **P5 hardening** — senior UX pass, projector view, seed/demo, README and operator checklist.

---

## 13. README must contain

Quick start (`pip install -r requirements.txt` → `python -m sgoa_vote seed` → `python -m sgoa_vote` →
open `http://localhost:8000`); the five surface URLs and demo logins with a prominent warning to change
them; the one-page MC operating sequence (*confirm wording → FINALIZE → read final wording → OPEN VOTING
→ wait → ask if anyone needs help → CLOSE VOTING → verify reconciliation → SHOW RESULT → NEXT*, with
"never edit a resolution once voting has opened; never expose interim counts"); backup and restore
procedure; the paper-ballot fallback note (procedural, not software — pre-numbered sheets and a sealed
box, and once fallback starts for a resolution, electronic and paper ballots are not mixed); the AC-15
senior usability script; and an **AGM-day deployment** appendix covering the move from localhost to a
dedicated router — SSID `SGOA-AGM`, WPA3-Personal (WPA2-AES fallback), client isolation on, WAN
disconnected, router `192.168.50.1`, server static `192.168.50.10`, DHCP `.50–.200`, WPS/UPnP/remote-admin
disabled, router password changed, laptop on Ethernet and on a UPS, QR code to the server IP displayed in
the hall.

---

## 14. Definition of done

- All tests green, including AC-01…AC-14 and every privacy gate.
- `python -m sgoa_vote` serves all five surfaces on localhost with seed data.
- A complete mock AGM can be driven end-to-end through a browser, including a mixed allocation from a
  3-entitlement holder and a mid-vote page reload.
- The certification bundle generates, its checksums verify, and the audit chain verifies independently
  via `python -m sgoa_vote verify-audit`.
- Zero outbound network requests at runtime; `requirements.txt` pinned with `==`.
- Database inspection confirms no voter-to-choice linkage exists in durable storage.

## 15. Explicitly out of scope — do not build

Remote or internet voting · Aadhaar/OTP/email/biometric identity verification · blockchain or
distributed ledger · automated interpretation of SGOA bylaws or proxy validity · automatic chair
casting-vote resolution (disposition note only) · any persistent voter-to-choice mapping, in any form,
for any reason.
