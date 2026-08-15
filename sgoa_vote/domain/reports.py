"""Final report, certification bundle and backups (work order §10).

The bundle is what survives the meeting. It has to let a scrutineer who was not
in the room check three things without trusting the server: that the results add
up, that the databases are the ones the report was produced from, and that the
audit chain has not been rewritten since.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..config import APP_VERSION, canonical_json
from ..util import display_time, new_id, now_iso
from . import agm, audit, entitlements, resolutions, results

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

CERTIFICATION_TEXT = (
    "We certify that the above aggregate results reconcile with the\n"
    "immutable electronic ballot records and eligibility ledger generated\n"
    "during the AGM. The system does not retain a voter-to-ballot-choice link."
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_hash() -> str:
    """Deterministic hash of the running source tree, for the report footer."""
    digest = hashlib.sha256()
    files = sorted(
        [p for p in PACKAGE_ROOT.rglob("*")
         if p.is_file() and p.suffix in (".py", ".sql", ".html", ".css", ".js")
         and "__pycache__" not in p.parts],
        key=lambda p: p.relative_to(PACKAGE_ROOT).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------
# report data
# --------------------------------------------------------------------------

def gather_report(conn, cfg) -> dict:
    agm_row = agm.require(conn)
    summary = entitlements.representation_summary(conn)
    chain = audit.verify_chain(conn)

    sections = []
    for row in resolutions.list_all(conn, include_superseded=False):
        snapshot = results.result_for(conn, row["resolution_id"])
        rec = results.reconciliation(conn, row["resolution_id"])
        history = resolutions.versions_of(conn, row["number"])
        sections.append({
            "number": row["number"],
            "title": row["title"],
            "full_text": row["full_text"],
            "version": row["version"],
            "text_hash": row["text_hash"] or "not finalized",
            "voting_rule": row["voting_rule"],
            "rule_label": resolutions.VOTING_RULES.get(row["voting_rule"], row["voting_rule"]),
            "status": row["status"],
            # A resolution that never opened has no counts to report. Printing a
            # row of zeros for it would read as "nobody voted for this", which is
            # a different and untrue statement.
            "voted": row["opened_at"] is not None,
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "opened_display": display_time(row["opened_at"], cfg.display_timezone),
            "closed_display": display_time(row["closed_at"], cfg.display_timezone),
            "disposition_note": row["disposition_note"],
            "eligible": snapshot["eligible_count"] if snapshot else rec["eligible"],
            "for": snapshot["for_count"] if snapshot else rec["for"],
            "against": snapshot["against_count"] if snapshot else rec["against"],
            "abstain": snapshot["abstain_count"] if snapshot else rec["abstain"],
            "cast": snapshot["cast_count"] if snapshot else rec["consumed"],
            "not_cast": snapshot["not_cast_count"] if snapshot else rec["not_cast"],
            "outcome": snapshot["outcome"] if snapshot else row["status"],
            "snapshot_hash": snapshot["snapshot_hash"] if snapshot else None,
            "reconciliation": rec,
            "versions": [{"version": h["version"], "text_hash": h["text_hash"],
                          "superseded": bool(h["superseded_by"])} for h in history],
        })

    return {
        "agm": dict(agm_row),
        "generated_at": now_iso(),
        "generated_display": display_time(now_iso(), cfg.display_timezone),
        "summary": summary,
        "governance": cfg.governance(),
        "config_hash": agm_row["config_hash"],
        "software_version": APP_VERSION,
        "build_hash": build_hash(),
        "audit": chain,
        "checkpoints": audit.checkpoints(conn),
        "resolutions": sections,
        "certification_text": CERTIFICATION_TEXT,
        "association_name": cfg.association_name,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_html(data: dict, hashes: dict) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(PACKAGE_ROOT / "web" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html")
    return template.render(r=data, hashes=hashes)


def render_pdf(data: dict, hashes: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceBefore=12)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    mono = ParagraphStyle("mono", parent=styles["BodyText"], fontName="Courier",
                          fontSize=7.5, leading=10)

    story = [
        Paragraph(data["agm"]["title"], h1),
        Paragraph(f"{data['association_name']}<br/>"
                  f"{data['agm']['agm_date']} &nbsp;&nbsp; {data['agm']['location']}", body),
        Paragraph("Final voting report and certification", h2),
    ]

    s = data["summary"]
    summary_rows = [
        ["Eligible apartments", s["eligible_apartments"]],
        ["Apartments represented", s["represented"]],
        ["In-person representations", s["own"]],
        ["Proxy representations", s["proxy"]],
        ["Total voting entitlements active", s["active_entitlements"]],
        ["Voting codes issued", s["active_credentials"]],
        ["AGM configuration hash", data["config_hash"][:32] + "..."],
        ["Software version", f"{data['software_version']} (build {data['build_hash'][:16]}...)"],
        ["Maximum proxies per attendee", data["governance"]["max_proxies_per_attendee"]],
        ["Chair casting vote", "enabled" if data["governance"]["chair_casting_vote_enabled"]
         else "disabled"],
        ["Report generated", data["generated_display"]],
    ]
    table = Table([[Paragraph(str(a), body), Paragraph(str(b), body)] for a, b in summary_rows],
                  colWidths=[70 * mm, 95 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
    ]))
    story += [Spacer(1, 6), table, PageBreak()]

    for section in data["resolutions"]:
        story.append(Paragraph(f"{section['number']} &nbsp; {section['title']}", h2))
        story.append(Paragraph(f"<b>Outcome: {section['outcome'].replace('_', ' ')}</b>", body))
        story.append(Spacer(1, 4))
        story.append(Paragraph("Exact wording put to the vote:", body))
        story.append(Paragraph(section["full_text"].replace("\n", "<br/>"), body))
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"Version {section['version']} &nbsp; "
                               f"Rule: {section['rule_label']}", body))
        story.append(Paragraph(section["text_hash"], mono))

        if section["voted"]:
            counts = [
                ["FOR", section["for"]],
                ["AGAINST", section["against"]],
                ["ABSTAIN", section["abstain"]],
                ["NOT CAST", section["not_cast"]],
                ["CAST", section["cast"]],
                ["ELIGIBLE", section["eligible"]],
            ]
            count_table = Table([[Paragraph(f"<b>{a}</b>", body), Paragraph(str(b), body)]
                                 for a, b in counts], colWidths=[40 * mm, 25 * mm])
            count_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("LINEABOVE", (0, 4), (-1, 4), 1.0, colors.black),
            ]))
            story += [Spacer(1, 4), count_table, Spacer(1, 4)]
            story.append(Paragraph(
                f"Opened {section['opened_display']} &nbsp; closed {section['closed_display']}",
                body))
            rec = section["reconciliation"]
            story.append(Paragraph(
                f"Reconciliation: {rec['labels']['check_entitlements']} &nbsp;|&nbsp; "
                f"{rec['labels']['check_ballots']} &nbsp;|&nbsp; "
                f"{'PASS' if rec['ok'] else 'FAIL'}", body))
        else:
            story.append(Paragraph(
                "<b>This resolution was not put to a vote. No ballots were cast on it "
                "and no counts are reported.</b>", body))
        if section["disposition_note"]:
            story.append(Paragraph(f"<b>Disposition note:</b> {section['disposition_note']}", body))
        story.append(Spacer(1, 10))

    story.append(PageBreak())
    story.append(Paragraph("Certification", h2))
    story.append(Paragraph(data["certification_text"].replace("\n", "<br/>"), body))
    story.append(Spacer(1, 14))
    for line in ["Scrutineer 1: __________________  Signature: __________  Date: ______",
                 "Scrutineer 2: __________________  Signature: __________  Date: ______"]:
        story.append(Paragraph(line, mono))
        story.append(Spacer(1, 8))
    story.append(Spacer(1, 6))
    for label, value in [
        ("Final audit-chain head", hashes["audit_chain_head"]),
        ("Ballot database hash", hashes["ballot.db"]),
        ("Eligibility DB hash", hashes["eligibility.db"]),
        ("Audit DB hash", hashes["audit.db"]),
        ("Software build hash", hashes["software_build"]),
    ]:
        story.append(Paragraph(f"{label + ':':<24}SHA256: {value}", mono))

    SimpleDocTemplate(str(path), pagesize=A4,
                      leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=16 * mm,
                      title=data["agm"]["title"]).build(story)


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------

def create_backup(svc) -> dict:
    stamp = now_iso().replace(":", "").replace("-", "").replace(".", "")[:15]
    target = svc.backup_path / stamp
    files = svc.db.backup_to(target)
    with svc.db.writer() as conn:
        audit.append(conn, "BACKUP_CREATED", {"target": target.name},
                     actor_role="ADMIN")
    return {"status": "BACKED_UP", "path": str(target),
            "files": [f.name for f in files]}


def generate_certification_bundle(svc, operator_id: str | None = None) -> dict:
    cfg = svc.config

    # Write the closing audit events first, so the head hash the report quotes is
    # the head hash present in the copied audit.db.
    with svc.db.writer() as conn:
        agm.finalize(conn, operator_id)
        audit.append(conn, "FINAL_REPORT_GENERATED", {"software_version": APP_VERSION},
                     actor_role="ADMIN", actor_id=operator_id)
        audit.create_checkpoint(conn, "final report generated")

    with svc.db.reader() as conn:
        data = gather_report(conn, cfg)
        verification = audit.verify_chain(conn)

    agm_date = str(data["agm"]["agm_date"]).replace("/", "-")
    bundle_name = f"SGOA-AGM-{agm_date}-FINAL"
    bundle = svc.export_path / bundle_name
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)

    svc.db.backup_to(bundle)

    hashes = {
        "eligibility.db": sha256_file(bundle / "eligibility.db"),
        "ballot.db": sha256_file(bundle / "ballot.db"),
        "audit.db": sha256_file(bundle / "audit.db"),
        "audit_chain_head": verification.get("head_hash", "0" * 64),
        "software_build": data["build_hash"],
    }

    (bundle / "final_report.html").write_text(render_html(data, hashes), encoding="utf-8")
    render_pdf(data, hashes, bundle / "final_report.pdf")

    (bundle / "software_version.txt").write_text(
        f"sgoa-vote {APP_VERSION}\nbuild_sha256={data['build_hash']}\n"
        f"generated_at={data['generated_at']}\n", encoding="utf-8")

    (bundle / "configuration_export.json").write_text(
        json.dumps({"config_hash": data["config_hash"],
                    "governance": data["governance"],
                    "association_name": cfg.association_name,
                    "agm": data["agm"]}, indent=2, default=str), encoding="utf-8")

    (bundle / "audit_chain_verification.txt").write_text(
        "AUDIT CHAIN VERIFICATION\n"
        "=======================\n"
        f"verified_at : {data['generated_at']}\n"
        f"events      : {verification['events']}\n"
        f"result      : {'INTACT' if verification['ok'] else 'BROKEN'}\n"
        f"detail      : {verification['reason']}\n"
        f"head hash   : {hashes['audit_chain_head']}\n\n"
        "Checkpoints\n-----------\n" +
        "\n".join(f"{c['head_sequence']:>6}  {c['head_hash']}  {c['label']}"
                  for c in data["checkpoints"]) + "\n",
        encoding="utf-8")

    (bundle / "manifest.json").write_text(json.dumps({
        "bundle": bundle_name,
        "generated_at": data["generated_at"],
        "software_version": APP_VERSION,
        "software_build_sha256": data["build_hash"],
        "agm": data["agm"],
        "config_hash": data["config_hash"],
        "database_hashes": {k: v for k, v in hashes.items() if k.endswith(".db")},
        "audit_chain_head": hashes["audit_chain_head"],
        "audit_chain_ok": verification["ok"],
        "resolutions": [
            {"number": s["number"], "version": s["version"], "text_hash": s["text_hash"],
             "outcome": s["outcome"], "for": s["for"], "against": s["against"],
             "abstain": s["abstain"], "not_cast": s["not_cast"],
             "eligible": s["eligible"], "snapshot_hash": s["snapshot_hash"]}
            for s in data["resolutions"]],
    }, indent=2, default=str), encoding="utf-8")

    (bundle / "README_ARCHIVE.txt").write_text(ARCHIVE_README, encoding="utf-8")

    # checksums.sha256 covers every other file in the bundle.
    lines = []
    for path in sorted(bundle.iterdir()):
        if path.name == "checksums.sha256" or not path.is_file():
            continue
        lines.append(f"{sha256_file(path)}  {path.name}")
    (bundle / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "status": "GENERATED",
        "bundle": bundle_name,
        "path": str(bundle),
        "files": sorted(p.name for p in bundle.iterdir()),
        "hashes": hashes,
        "audit_chain_ok": verification["ok"],
    }


ARCHIVE_README = """SGOA AGM VOTING - CERTIFICATION BUNDLE
======================================

This directory is the permanent record of one Annual General Meeting.

Contents
--------
final_report.pdf / .html    The signed report: wording, hashes, counts, outcomes.
eligibility.db              Apartments, representations, credentials, consumption ledger.
ballot.db                   Resolutions, anonymous ballots, frozen result snapshots.
audit.db                    Hash-chained event log and checkpoints.
manifest.json               Machine-readable summary including every hash below.
checksums.sha256            SHA-256 of every other file in this directory.
configuration_export.json   The governance settings the meeting actually ran under.
audit_chain_verification.txt Result of re-hashing the entire audit chain.
software_version.txt        Version and source-tree hash of the build that ran.

How to verify this bundle
-------------------------
1. Checksums:      sha256sum -c checksums.sha256
                   (PowerShell: Get-FileHash -Algorithm SHA256 <file>)
2. Audit chain:    python -m sgoa_vote verify-audit --data-dir .
3. Reconciliation: every resolution in the report shows
                   cast + not cast = eligible, and FOR + AGAINST + ABSTAIN = cast.
                   Both must hold. A resolution that failed either was never
                   given an outcome.

What this bundle deliberately cannot tell you
---------------------------------------------
Which apartment, attendee or voting code cast any particular ballot. The ballot
table holds only a random identifier, the resolution, the choice and a timestamp.
The link between a voting entitlement and the ballots it produced was never
written to durable storage, so it cannot be recovered from these files by anyone,
including the administrator.
"""
