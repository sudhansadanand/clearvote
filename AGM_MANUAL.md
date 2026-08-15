# SGOA AGM Voting — Operator Manual

Short version, for the people actually running the meeting. Print pages 1 and 2 and
keep them with the laptop. Full technical detail is in `README.md`.

---

## 1. The kit

| Item | Notes |
|---|---|
| Laptop | 8 GB RAM, working battery, Ethernet port or adapter |
| Router / access point | Dedicated. **Not** the building guest Wi-Fi |
| UPS or power bank | For the laptop charger and the router |
| 2 association tablets | Assisted voting, and `/checkin` for the registration queue |
| Printer + card stock | For the voting-code cards |
| 2 USB drives | One for each scrutineer, for the final bundle |
| Paper ballot pack | Pre-numbered sheets and a sealed box, in case everything fails |

---

## 2. Two weeks before — set up the meeting

Install once, **while you still have internet** — the AGM network has none, and nothing
downloads on the day:

```bash
python --version          # must be 3.12 or newer
pip install -r requirements.txt
python -m pytest tests -q # confirms the install on this exact laptop
```

Prepare two spreadsheets, saved as CSV.

**`apartments.csv`** — the register:

```csv
apartment_id,owner_name,eligible
A1,Meera Raghavan,yes
A2,Sunil Menon,yes
B4,Disputed ownership,no
```

`owner_name` is a registration aid and never reaches the ballot store. `eligible` may be
left blank, which means yes. Mark `no` for any flat the committee has determined cannot
vote. A `code` column will **not** be used — see the note at the end of this section.

**`agenda.csv`** — one resolution per row, exact wording in column A, no header needed:

```csv
That the Association approve the lift replacement as tabled at this meeting.
That the Association adopt the annual budget for 2026-27 as circulated.
```

If a resolution needs a two-thirds majority, add a header row and a rule column:

```csv
title,full_text,voting_rule
By-law amendment,That the by-laws be amended per Annexure 2.,two-thirds
```

Create the real meeting:

```bash
python -m sgoa_vote --data-dir data-agm2026 init \
  --title "SGOA Annual General Meeting 2026" \
  --date 2026-09-20 \
  --location "Community Hall, Shanti Gulmohar" \
  --apartments apartments.csv \
  --resolutions agenda.csv
```

> **Voting codes cannot be pre-assigned to apartments.** They are generated at the desk
> when someone checks in, and only a hash is kept. Two reasons: a code that existed
> before check-in could be used by whoever found it, without anyone verifying their
> authority; and a code belongs to a *person*, not a flat — someone representing three
> apartments gets one code carrying three votes, not three codes.

This prints five operator passwords **once**. Write them down before closing the window.
It also prints the **configuration hash** — both scrutineers should record it now, on
paper. That hash is what lets them prove afterwards that the rules did not change.

If you have no CSV yet, `--blocks A:17,B:17,C:17` creates A1–A17, B1–B17 and C1–C17.

Start the server and enter the agenda:

```bash
python -m sgoa_vote --data-dir data-agm2026 run
```

Sign in at `http://localhost:8000/mc` as `mc` and enter every expected resolution as a
**draft**, in order, with its exact wording. Leave them all as drafts. Choose the voting
rule deliberately for each one — the software will never guess a special majority from
the wording.

Extra scrutineer account if you need one:
`python -m sgoa_vote --data-dir data-agm2026 add-operator scrutineer3 SCRUTINEER`

---

## 3. Rehearse — in a separate folder

**Never rehearse in the real meeting's folder.**

```bash
python -m sgoa_vote --data-dir data-rehearsal seed
python -m sgoa_vote --data-dir data-rehearsal run
```

This builds a practice meeting with 12 voters and prints their codes. Every page shows a
yellow **DEMO DATA** banner, so a rehearsal can never be mistaken for the real thing.
Practise the full sequence at least once, including a tie and a lost card.

Before the day, have two residents who did not help build this each join with a printed
code and cast a confirmed vote unaided, after one verbal explanation. If either hesitates
at any step, fix that step.

---

## 4. AGM morning — twenty minutes before doors

1. Plug the router into the UPS. **Disconnect the WAN/internet cable.**
2. Connect the laptop to the router by Ethernet. Start the server.
3. Open `http://localhost:8000/admin` and sign in as `admin`. Check **Health**:
   all three databases `ok`, audit chain intact, disk space fine.
4. Confirm the meeting shows zero ballots and no resolution open.
5. Click **BACK UP NOW**. This is the clean pre-meeting snapshot.
6. Click **OPEN REGISTRATION**.
7. On `/scrutineer`, click **VERIFY AUDIT CHAIN**. Both scrutineers write down the head
   hash and compare it to the configuration hash they recorded earlier.
8. Put `/projector` on the hall screen. Put the QR code or the address on a poster.
9. Set the two tablets to `/`, screen sleep disabled, in a spot where a voter's screen
   is not visible to the room.

---

## 5. The registration desk

Two screens, and you can use either or both:

| | Screen | Good for |
|---|---|---|
| **Desk** | `/registration` | the full register, issuing and resetting codes, revoking |
| **Phone** | `/checkin` | one big form, nothing else — walking the queue on a tablet |

For each attendee:

1. Find the apartment. Verify owner or proxy authority against the governing documents —
   **the software does not judge proxy validity, you do.**
2. **ASSIGN REPRESENTATION**: apartment, name, and whether they are the owner in person
   or holding a proxy. Repeat for each apartment this person represents.
3. **ISSUE CODE** once all their apartments are assigned. One person gets one code
   carrying all their entitlements.
4. Fold the printed card and hand it over privately.

**Working with two people.** One walks the queue with a tablet on `/checkin` capturing
who represents what; the other stays at the desk on `/registration` and issues the codes.
`/checkin` deliberately cannot issue a code — a code has to be printed, folded and handed
over in person, so it stays a desk job. After each assignment the phone shows a running
total ("Meera Raghavan now holds 3 voting entitlements"), and it keeps the name in the
box so adding their next proxy is one field away.

Things that will come up:

- **The flat is already checked in.** The system blocks it and asks for an authorised
  override with a written reason. Both events go on the record. Follow the governing
  documents; do not override casually.
- **Lost card, dead phone, code shared by mistake.** Use **RESET CODE**. A new code is
  issued against the same ledger. Votes already cast stay cast and are not returned.
- **A proxy is withdrawn.** Use **REVOKE**. It removes only unused future entitlement.
  Ballots already cast are anonymous and are never deleted or reassigned.
- **"This code is already in use on another device."** One code, one device, by design.
  Reset it at the desk.

The code appears exactly once, on the card. It is not recoverable afterwards — only a
hash is stored. That is deliberate.

---

## 6. The MC's ten steps — one per resolution

> **Print this section.**

```
1. Display and discuss the current wording.
2. Incorporate any approved changes.
3. FINALIZE          -> the wording freezes and a hash appears.
4. Read the final wording aloud and display it.
5. OPEN VOTING
6. Wait. The console shows only how many entitlements have come in.
7. Ask: "Does anyone still need help voting?"
8. CLOSE VOTING      -> the count appears on the projector at the same moment.
9. Read the result out. Check that reconciliation passed.
10. NEXT RESOLUTION.
```

**The result goes up on its own.** There is no separate "show result" step: closing the
voting puts the count on the hall screen in the same instant it is produced. You will see
it at the same time as the room, which is the point — nobody can wonder what happened in
between.

**Never edit a resolution once voting has opened.**
**Never read out interim FOR/AGAINST counts** — the system will not show them to anyone,
including the administrator, until voting closes.

Wording changed after finalizing but before voting? Use **CREATE AMENDMENT**. It makes a
new version; the old one stays on the record. Open the new version.

Not going to the vote at all? **WITHDRAW** or **NOT PUT TO VOTE**, with a note. The
number is kept and never reused.

**A tie** reports `TIED` and stops there. The software will not apply a casting vote. Use
**RECORD DISPOSITION NOTE** to put any Chair or General Body action on the record,
separately from the anonymous ballots.

---

## 7. If something goes wrong

| What you see | What to do |
|---|---|
| A voter's Wi-Fi drops on Confirm | Tell them to tap Confirm again. A repeat can never double-count. |
| Voter closed the browser | Their vote is safe. They rejoin with the same code. |
| Phone battery died | Reset the code at the desk, vote on a tablet. Cast votes are not returned. |
| Router reboots | Nothing is lost. Devices reconnect. Pause the item if governance allows. |
| Server or laptop restarts | Restart the server. The open resolution and every committed ballot survive. |
| **RECONCILIATION ERROR** after closing | **Stop.** No outcome is produced. Both scrutineers and the administrator investigate before anything is certified. Do not reopen or re-vote without a governance decision. |
| Total technology failure | Switch to paper ballots. Record the time of the switch in the minutes. |

**Paper fallback:** use the pre-numbered sheets and the sealed box, keeping the same
entitlement register and distinguishing FOR / AGAINST / ABSTAIN / not cast. Once paper
starts for a resolution, do not mix paper and electronic ballots for that resolution.

Take a backup (`/admin` → **BACK UP NOW**) after registration closes and after each
resolution, if the pace allows.

---

## 8. End of the meeting

1. Confirm no resolution is still open and none is in reconciliation error.
2. `/admin` → **GENERATE FINAL REPORT**, re-entering the admin password. This finalizes
   the AGM and writes the certification bundle.
3. Copy `export/SGOA-AGM-<date>-FINAL/` to **two USB drives**, one per scrutineer.
4. Both scrutineers read the report, check that every resolution's reconciliation shows
   PASS, and sign the certification page.
5. Keep the laptop until the bundle has been verified from at least one USB drive.

To verify a bundle later, on any machine:

```bash
python -m sgoa_vote verify-audit --data-dir path/to/bundle
```

Plus the file checksums listed in `checksums.sha256`. The bundle's own
`README_ARCHIVE.txt` explains both.

---

## 9. What the system will not tell you, ever

It cannot show how any apartment, attendee or code voted. That linkage is never written
to disk, so it cannot be recovered from the databases by anyone — including the
administrator, and including someone who later takes the files.

What it can prove is that the number of entitlements consumed exactly equals the number
of ballots counted, that no code voted more than it was issued, and that the wording,
the results and the event log have not been altered since the meeting.

If anyone asks for a breakdown by flat, the correct answer is that it does not exist.
