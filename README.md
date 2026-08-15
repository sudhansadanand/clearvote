# SGOA AGM Voting System

[![tests](https://github.com/sudhansadanand/clearvote/actions/workflows/tests.yml/badge.svg)](https://github.com/sudhansadanand/clearvote/actions/workflows/tests.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

Local, private, auditable electronic voting for the Shanti Gulmohar Owners Association
Annual General Meeting. Runs entirely on one laptop, on a network with no internet
connection, and produces a signed certification bundle at the end of the meeting.

The system proves that every counted ballot came from a valid, unused voting
entitlement — and is deliberately unable to show how any apartment voted.

---

> **Running the actual meeting?** Read [`AGM_MANUAL.md`](AGM_MANUAL.md) — the short
> operator manual for setup, the registration desk, the MC's ten steps, and what to do
> when something goes wrong. This README is the technical reference behind it.

## Quick start (rehearsal)

```bash
pip install -r requirements.txt
python -m sgoa_vote seed          # creates the DEMO AGM and prints 12 voting codes
python -m sgoa_vote run           # serves on http://localhost:8000
```

## Setting up the real meeting

`seed` builds a practice meeting, watermarked `DEMO DATA` on every page. The real one is
created with `init`, in its own data directory:

```bash
python -m sgoa_vote --data-dir data-agm2026 init \
  --title "SGOA Annual General Meeting 2026" \
  --date 2026-09-20 \
  --location "Community Hall, Shanti Gulmohar" \
  --apartments apartments.csv \
  --resolutions agenda.csv
```

That creates the AGM record, the apartment register, the agenda as drafts, and five
operator accounts whose strong passwords are printed once. It leaves the meeting in
`SETUP`; the administrator opens registration on the day, so the audit trail carries the
real time the desk opened.

**`apartments.csv`** needs a header row with `apartment_id`, optionally `owner_name` and
`eligible` (blank means eligible). Any other column is reported as unused rather than
silently dropped. Use `--blocks A:17,B:17,C:17` instead if you have no sheet yet.

```csv
apartment_id,owner_name,eligible
A1,Meera Raghavan,yes
B4,Disputed ownership,no
```

**`agenda.csv`** in its simplest form is one resolution per row, exact wording in the
first column, no header:

```csv
That the Association approve the lift replacement as tabled at this meeting.
That the Association adopt the annual budget for 2026-27 as circulated.
```

They are numbered R1, R2, … in file order, and a short title is derived from the wording.
Add a header row to control any of that:

```csv
number,title,full_text,voting_rule
R1,Lift replacement,That the Association approve the lift replacement.,
R2,By-law amendment,That the by-laws be amended per Annexure 2.,two-thirds
R3,Auditor,That the auditor be appointed.,majority of all eligible
```

`voting_rule` accepts blank or `simple` (FOR > AGAINST), `two-thirds`, or
`majority of all eligible`. Anything else is refused rather than guessed at — the
software must never infer a special majority from the wording.

Everything arrives as a **draft**. The MC still finalizes each one on the day, which is
what freezes the wording and produces its hash.

> **Voting codes cannot be imported.** A `code` column in the apartment sheet is ignored
> and reported. Codes are generated at the registration desk when an attendee checks in,
> and only a keyed hash is stored — a code that existed before check-in would be usable
> by whoever found it, without anyone having verified their authority. Codes are also
> per *attendee*, not per apartment: one person representing three flats gets one code
> carrying three entitlements.

Always rehearse in a **separate** `--data-dir`. Seeding refuses to run against a database
that already holds ballots, but keeping the two apart is the real protection.

| Surface | URL | Who uses it |
|---|---|---|
| Voter | `http://localhost:8000/` | residents, on their own phones |
| MC console | `http://localhost:8000/mc` | whoever is running the meeting |
| Registration desk | `http://localhost:8000/registration` | check-in, proxy verification, issuing codes |
| Phone check-in | `http://localhost:8000/checkin` | assigning representations from a tablet in the queue |
| Scrutineer | `http://localhost:8000/scrutineer` | read-only reconciliation and audit |
| Projector | `http://localhost:8000/projector` | the screen in the hall |
| Administrator | `http://localhost:8000/admin` | setup, backups, final report |

Seeded operator accounts are `admin`, `registration`, `mc` and `scrutineer`, all with
the password `sgoa-demo`.

> **Change these before any real meeting.** `python -m sgoa_vote set-password mc <newpassword>`
> Any page served from a demo AGM carries a `DEMO DATA` banner so a rehearsal can never
> be mistaken for the real thing.

Other commands:

```bash
python -m sgoa_vote verify-audit             # re-hash the audit chain, exit 2 if broken
python -m sgoa_vote backup                   # copy the three databases into backups/
python -m sgoa_vote set-password mc <pw>     # change an operator password
python -m sgoa_vote add-operator scrutineer3 SCRUTINEER   # extra account
python -m sgoa_vote run --port 9000          # different port
python -m sgoa_vote --data-dir D:\agm2027 init ...        # a separate meeting
```

---

## How the privacy design works

Three separate SQLite databases, and nothing joins the first to the second:

```
eligibility.db          ballot.db              audit.db
who may vote            what was voted         what happened
apartments              resolutions            hash-chained event log
representations         ballots                checkpoints
credentials             result_snapshots
consumption ledger
```

A ballot row has four columns: a random id, the resolution, the choice, and a timestamp.
There is no apartment, no attendee, no credential, no session, no IP address. When a
voter confirms, the server opens one transaction across all three databases, mints N
one-time authorisation tokens **in memory**, spends each to insert one anonymous ballot,
increments that credential's consumed counter by N, writes an aggregate audit event, and
commits. The token-to-credential mapping is never written anywhere, so it cannot be
recovered afterwards — not by an administrator, not by anyone holding the files.

What the system can prove: *"31 valid entitlements were consumed and exactly 31 ballots
were accepted."* What it cannot prove: *"ballot 8843 came from code KTR7."*

Two invariants are checked before any result is published, and a resolution that fails
either one gets no outcome at all — it goes to `RECONCILIATION_ERROR` and blocks
certification until the scrutineers resolve it:

```
cast + not cast          = eligible
FOR + AGAINST + ABSTAIN  = cast
```

---

## Running the meeting

### Before doors open

1. Change every operator password. Confirm no resolution is open and no ballots exist.
2. `python -m sgoa_vote backup` — the clean pre-AGM snapshot.
3. Open `/admin`, check **Health**: database integrity `ok` on all three files, audit
   chain intact, disk space adequate.
4. Have both scrutineers record the AGM configuration hash and the audit-chain head
   shown on `/scrutineer`. This is what makes later tampering detectable.
5. Put the projector page on the screen and two assisted-voting tablets in place.

### Registration

For each attendee: find the apartment, verify owner or proxy authority against the
governing documents, assign exactly one representation per apartment, then issue **one**
voting code carrying all their verified entitlements. Fold the printed card before
handing it over.

The code is shown exactly once, on the card. Only a keyed hash is stored, so it cannot
be looked up later — a lost card means **Reset code**, which issues a new code bound to
the same ledger. Votes already cast stay cast.

### For each resolution (the one-page sequence)

1. Confirm the wording on screen.
2. **FINALIZE** — the wording freezes and is hashed.
3. Read the final wording aloud and display it.
4. **OPEN VOTING**.
5. Wait. The console shows only how many entitlements have been received.
6. Ask whether anyone still needs assistance.
7. **CLOSE VOTING** — the system recounts, reconciles, and puts the result on the
   projector in the same moment.
8. Read the result out. Check that reconciliation passed.
9. **NEXT**.

> Never edit a resolution once voting has opened. Never expose interim FOR/AGAINST
> counts — the API refuses to serve them while a resolution is open, to every role
> including the administrator.

A tie reports `TIED` and stops. The software does not apply a casting vote; use
**Record disposition note** so any Chair or General Body action is on the record
separately from the anonymous ballots.

### Publishing the result

Closing a resolution publishes its result to the projector immediately, in the same
transaction that produces it. There is deliberately no gap in which the MC alone knows
the outcome — the count reaches the hall at the moment it is made.

This is a change from the specification's operating sequence (§20.3), which has the MC
press a separate **SHOW RESULT**. The manual step never protected the result itself:
ballots and result snapshots are immutable and the close is timestamped in the audit
chain, so nobody could have altered anything during that gap. What it did create was a
window worth being suspicious about, and removing it costs nothing.

A resolution that fails reconciliation is never published. The projector says the count
is being verified by the scrutineers, which is the truth and is better broadcast
immediately than discovered later.

If SGOA's governing documents require the Chair to formally declare each result, set
`"auto_publish_results": false` in `config.json`. The **SHOW RESULT** button then
reappears on the MC console and the projector holds on the current resolution, showing no
numbers, until it is pressed. Whichever mode was used is recorded in the AGM
configuration hash printed in the final report.

### End of the meeting

1. Confirm no resolution is still open.
2. `/admin` → **Generate final report** (re-enter your password to confirm).
3. Copy `export/SGOA-AGM-<date>-FINAL/` to two USB drives held by different scrutineers.
4. Both scrutineers review and sign the report.

---

## The certification bundle

```
export/SGOA-AGM-2026-09-20-FINAL/
  final_report.pdf  final_report.html
  eligibility.db  ballot.db  audit.db
  manifest.json  checksums.sha256  software_version.txt
  configuration_export.json  audit_chain_verification.txt  README_ARCHIVE.txt
```

To verify it months later, without trusting the server it came from:

```bash
# 1. every file is the file the report was produced from
sha256sum -c checksums.sha256
#    PowerShell: Get-FileHash -Algorithm SHA256 ballot.db

# 2. the audit chain has not been rewritten
python -m sgoa_vote verify-audit --data-dir path/to/bundle
#    (--data-dir works before or after the subcommand)

# 3. the arithmetic holds — every resolution in the report shows both
#    reconciliation equalities, and any that failed was never given an outcome
```

---

## Backup and recovery

`python -m sgoa_vote backup` (or the button on `/admin`) copies all three databases to
`backups/<timestamp>/` while holding the writer lock, so the copy is always taken
between transactions.

Take a backup after registration closes and after each resolution closes. To restore,
stop the server, move the three files from a backup directory into `data/`, restart, and
run the reconciliation view on `/scrutineer` before resuming. Any ballots cast after the
backup checkpoint are gone and require an explicit governance decision — a re-vote or
the paper fallback.

**If the server crashes**, just restart it. The open resolution, the ledger and every
committed ballot survive, because each vote is a single committed transaction.

**Paper fallback** is procedural, not software. Prepare pre-numbered ballot sheets and a
sealed box before the AGM. Once fallback begins for a resolution, do not mix electronic
and paper ballots for that resolution unless a reconciliation method was approved in
advance. Record the transition time in the minutes.

---

## Tests

```bash
python -m pytest tests -q          # 148 tests
```

Every push runs the same suite on Python 3.12 and 3.13 via GitHub Actions, with the
privacy gates and the acceptance criteria as their own steps so a failure in either is
visible without opening the log. A second job seeds a meeting, verifies the audit chain,
creates a real register, checks that seeding refuses to overwrite an existing meeting,
starts the server, and greps the served assets for any external reference.

The privacy tests are release gates, not niceties. They read the raw database rather
than asking the application politely: they dump every ballot row and assert that no
credential, attendee, apartment or session identifier appears in any of them; they drop
the immutability triggers and edit the audit log to prove the hash chain still catches
it; and they call the results endpoint with an admin session while voting is open and
require a 403.

Acceptance criteria AC-01 to AC-14 each have at least one asserting test named
`test_ac01_…` through `test_ac14_…`. `tests/acceptance/test_mock_agm.py` runs a complete
simulated meeting — a three-entitlement proxy holder, a mixed allocation, a retry after
a dropped connection, a credential reset mid-meeting, an abstention, deliberate
non-voters, an exact tie, an amended resolution, a withdrawn resolution and a two-thirds
majority — and checks the result against a hardcoded answer key before verifying the
certification bundle's checksums.

### AC-15: the senior usability check (must be done by a person)

Before production use, ask two residents who did not help build this to each do the
following unaided, after one verbal explanation and with no administrator intervention:
join with a printed code, read the resolution, choose FOR, confirm, and describe what
the screen now says. Both must succeed. If either hesitates at a step, fix the step.

---

## Deliberate deviations from the specification

Three, each flagged here because the specification says something slightly different.

**1. Rollback journal instead of WAL.** SQLite provides atomic commit across `ATTACH`ed
databases only in rollback-journal modes; in WAL there is no multi-database master
journal. A vote must write the ledger, the ballots and the audit event as one indivisible
unit, so WAL would have meant giving up AC-03. This build uses `journal_mode=DELETE` with
`synchronous=FULL`. At this scale the cost is a few milliseconds per transaction against
a two-second p95 budget. (`DELETE` rather than `TRUNCATE`: `TRUNCATE` leaves a
zero-length journal file that the running server keeps open on Windows, which makes a
second connection — a scrutineer inspecting the file, a backup tool — fail with a disk
I/O error.)

**2. No htmx.** The specification suggests server-rendered HTML plus htmx. The live parts
of this application are a small JSON state machine driven by polling, which htmx does not
simplify, so the pages use about 200 lines of plain JavaScript instead. This removes a
vendored dependency and keeps the guarantee that nothing is fetched from the internet.

**3. Localhost, plain HTTP, no TLS.** Deliberate for this build. The router, WPA3,
static IP, local DNS and certificate provisioning are AGM-day physical deployment, not
software; see below. The session cookie's `Secure` flag is driven by `cookie_secure` in
config, so enabling HTTPS later is a one-line change.

FastAPI's `/docs` and `/redoc` are disabled, because they load Swagger assets from a CDN.

---

## AGM-day deployment: moving off localhost

The same build, pointed at a dedicated network:

```
SSID              SGOA-AGM
Security          WPA3-Personal (WPA2-AES acceptable fallback; never WPA/WEP)
Client isolation  ON
WAN               disconnected or disabled
Router            192.168.50.1     — admin password changed, WPS/UPnP/remote admin off
Server            192.168.50.10    — static/reserved, connected by Ethernet
DHCP pool         192.168.50.50 – 192.168.50.200
Voting URL        http://192.168.50.10:8000/   — QR code displayed in the hall
```

Put the laptop and the router on a UPS or a power bank. Use a dedicated router, never
the building guest Wi-Fi. If a publicly trusted certificate for a domain SGOA controls
can be obtained in advance and resolved locally to the server IP, use it and set
`cookie_secure` — that avoids browser warnings on residents' phones. Otherwise document
the isolated-LAN-plus-HTTP fallback as an accepted risk.

---

## Configuration

`config.json` beside the project, all keys optional:

```json
{
  "max_proxies_per_attendee": 5,
  "auto_publish_results": true,
  "sessions_per_credential": 1,
  "voter_session_hours": 12,
  "operator_inactivity_minutes": 15,
  "join_rate_limit_attempts": 5,
  "cookie_secure": false,
  "display_timezone": "Asia/Kolkata"
}
```

Settings that affect outcomes are hashed into the AGM configuration hash printed in the
final report, so scrutineers can confirm the meeting ran under the rules they recorded at
the start.

### Governance decisions to settle before production use

The software can be built and rehearsed now, but these are SGOA Committee decisions and
should be checked against the governing documents and applicable law first:

| Decision | Current default |
|---|---|
| Maximum proxies per attendee | 5 — **a placeholder**, set it from the bylaws |
| Owner vs proxy priority when both appear | operator override with a recorded reason |
| Chair casting vote | disabled; ties report `TIED` and stop |
| Who declares the result | published automatically on close; set `auto_publish_results` to `false` if the Chair must declare it |
| Special resolution thresholds | per resolution, chosen explicitly, never inferred |
| Challenge and retention period | not set; decide before destroying credential material |
| Scrutineers | two independent people recommended |

---

## Licence

MIT — see [LICENSE](LICENSE). Any association is free to use, modify and run this for
their own AGM.

The copyright line names the author rather than SGOA. If the committee should hold it
instead, change that one line. If you would rather that anyone running a modified version
had to publish their changes — an argument with some force for voting software — the
AGPL-3.0 is the licence to swap in, at the cost of making casual reuse harder.
