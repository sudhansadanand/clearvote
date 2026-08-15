"""Command line entry point.

    python -m sgoa_vote run                 start the server
    python -m sgoa_vote seed                create the demo AGM
    python -m sgoa_vote set-password ...    replace a demo operator password
    python -m sgoa_vote verify-audit        re-hash the audit chain
    python -m sgoa_vote backup              copy the three databases
"""

from __future__ import annotations

import argparse
import sys

from .config import APP_VERSION, Config
from .services import Services


def _services(args) -> Services:
    cfg = Config.load(getattr(args, "config", None))
    if args.data_dir:
        cfg.data_dir = args.data_dir
    return Services(cfg)


def cmd_run(args) -> int:
    import uvicorn

    from .app import create_app, create_multi_event_app, discover_events
    from .config import Config

    if getattr(args, "events_dir", None):
        return _run_multi_event(args, uvicorn, create_multi_event_app,
                                discover_events, Config)

    svc = _services(args)
    cfg = svc.config
    host = args.host or cfg.host
    port = args.port or cfg.port

    with svc.db.reader() as conn:
        agm_row = conn.execute("SELECT title, status FROM agms LIMIT 1").fetchone()

    print()
    print(f"  SGOA AGM Voting System {APP_VERSION}")
    if agm_row:
        print(f"  {agm_row['title']}  [{agm_row['status']}]")
    else:
        print("  No AGM yet. Run: python -m sgoa_vote seed")
    print()
    print(f"  Voter        http://localhost:{port}/")
    print(f"  MC console   http://localhost:{port}/mc")
    print(f"  Registration http://localhost:{port}/registration")
    print(f"  Scrutineer   http://localhost:{port}/scrutineer")
    print(f"  Projector    http://localhost:{port}/projector")
    print(f"  Administrator http://localhost:{port}/admin")
    print()

    uvicorn.run(create_app(svc), host=host, port=port, log_level="info", access_log=False)
    return 0


def cmd_seed(args) -> int:
    from .domain.errors import DomainError
    from .seed import print_summary, seed

    svc = _services(args)
    try:
        print_summary(seed(svc, password=args.password))
    except DomainError as exc:
        print(f"\n  Refused: {exc.message}\n", file=sys.stderr)
        return 1
    return 0


def _run_multi_event(args, uvicorn, create_multi_event_app, discover_events, Config):
    """Serve every meeting in an events directory under its own path prefix."""
    from pathlib import Path

    events_dir = Path(args.events_dir)
    cfg = Config.load(getattr(args, "config", None))
    host = args.host or cfg.host
    port = args.port or cfg.port
    names = discover_events(events_dir)

    print()
    print(f"  SGOA AGM Voting System {APP_VERSION}")
    print(f"  Serving {len(names)} meeting(s) from {events_dir.resolve()}")
    print()
    if names:
        for name in names:
            print(f"    http://localhost:{port}/{name}/")
        print()
        print(f"  Index of all meetings:  http://localhost:{port}/")
    else:
        print(f"  No meetings found. Create one with:")
        print(f"    python -m sgoa_vote --data-dir {events_dir}/my-event init \\")
        print(f"        --title \"...\" --date 2026-09-20 --apartments apartments.csv")
        print()
        print("  Then restart. Deleting a folder deletes that meeting.")
    print()

    uvicorn.run(create_multi_event_app(events_dir, cfg), host=host, port=port,
                log_level="info", access_log=False)
    return 0


def _warn_about_unused_columns(ignored: list[str]) -> None:
    if not ignored:
        return
    print()
    print(f"  NOTE: these columns were not used: {', '.join(ignored)}")
    if any(name.strip().lower() == "code" for name in ignored):
        print("        Voting codes cannot be imported. They are generated at the")
        print("        registration desk when an attendee checks in, and only a hash")
        print("        is stored -- a code known before check-in would be a code")
        print("        someone could use without being verified. One code covers all")
        print("        the apartments a person represents, so a per-apartment column")
        print("        would not fit in any case.")


def cmd_init(args) -> int:
    """Create the real meeting, as opposed to the demo one."""
    from .domain.errors import DomainError
    from .seed import (ignored_apartment_columns, init_agm, load_apartments_csv,
                       load_resolutions_csv, parse_blocks, print_init_summary)

    svc = _services(args)
    try:
        if args.apartments:
            apartments = load_apartments_csv(args.apartments)
            _warn_about_unused_columns(ignored_apartment_columns(args.apartments))
        else:
            apartments = parse_blocks(args.blocks)
        agenda = load_resolutions_csv(args.resolutions) if args.resolutions else None
        print_init_summary(init_agm(svc, title=args.title, agm_date=args.date,
                                    location=args.location, apartments=apartments,
                                    resolutions=agenda))
    except DomainError as exc:
        print(f"\n  Refused: {exc.message}\n", file=sys.stderr)
        return 1
    return 0


def cmd_add_operator(args) -> int:
    from .domain import auth
    from .domain.errors import DomainError
    from .seed import strong_password

    svc = _services(args)
    password = args.password or strong_password()
    try:
        with svc.db.writer() as conn:
            auth.create_operator(conn, args.username, args.role.upper(), password)
    except DomainError as exc:
        print(f"  {exc.message}", file=sys.stderr)
        return 1
    print(f"\n  Created {args.username} ({args.role.upper()})")
    print(f"  Password: {password}")
    print("  Write this down now; it is not shown again.\n")
    return 0


def cmd_set_password(args) -> int:
    from .domain import auth
    from .domain.errors import DomainError

    svc = _services(args)
    try:
        with svc.db.writer() as conn:
            auth.set_password(conn, args.username, args.password)
    except DomainError as exc:
        print(f"  {exc.message}", file=sys.stderr)
        return 1
    print(f"  Password updated for {args.username}.")
    return 0


def cmd_verify_audit(args) -> int:
    from .domain import audit

    svc = _services(args)
    with svc.db.reader() as conn:
        result = audit.verify_chain(conn)
        marks = audit.checkpoints(conn)

    print()
    print(f"  Audit chain: {'INTACT' if result['ok'] else 'BROKEN'}")
    print(f"  Events     : {result['events']}")
    print(f"  Detail     : {result['reason']}")
    if result["ok"]:
        print(f"  Head hash  : {result['head_hash']}")
    else:
        print(f"  First bad  : sequence {result['first_bad_sequence']}")
    if marks:
        print("\n  Checkpoints")
        for mark in marks:
            print(f"    {mark['head_sequence']:>5}  {mark['head_hash'][:32]}...  {mark['label']}")
    print()
    return 0 if result["ok"] else 2


def cmd_backup(args) -> int:
    from .domain import reports

    svc = _services(args)
    result = reports.create_backup(svc)
    print(f"  Backed up to {result['path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sgoa_vote",
                                     description="SGOA AGM Voting System")
    parser.add_argument("--data-dir", default=None,
                        help="directory holding the three database files (default: data)")
    parser.add_argument("--config", default=None, help="path to config.json")

    # The same two options are accepted after the subcommand as well, so both
    #     sgoa_vote --data-dir bundle verify-audit
    #     sgoa_vote verify-audit --data-dir bundle
    # work. The second is the form a scrutineer reaches for when checking an
    # archived certification bundle, and it is the form the documentation uses.
    # SUPPRESS keeps the subcommand copy from overwriting a value given earlier.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="directory holding the three database files")
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to config.json")

    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    def add(name, **kwargs):
        return sub.add_parser(name, parents=[common], **kwargs)

    run = add("run", help="start the server")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--events-dir", default=None,
                     help="serve every meeting in this directory at /<folder-name>/ "
                          "instead of one meeting at the root")
    run.set_defaults(func=cmd_run)

    init = add("init", help="create the real AGM and its operator accounts")
    init.add_argument("--title", required=True,
                      help='e.g. "SGOA Annual General Meeting 2026"')
    init.add_argument("--date", required=True, help="YYYY-MM-DD")
    init.add_argument("--location", default="")
    init.add_argument("--apartments", default=None,
                      help="CSV with columns apartment_id, owner_name, eligible")
    init.add_argument("--blocks", default="A:17,B:17,C:17",
                      help="used when no CSV is given, e.g. A:17,B:17,C:17")
    init.add_argument("--resolutions", default=None,
                      help="CSV of the agenda: one resolution per row, exact wording "
                           "in the first column. Optional columns: number, title, "
                           "voting_rule")
    init.set_defaults(func=cmd_init)

    add_op = add("add-operator", help="create an extra operator account")
    add_op.add_argument("username")
    add_op.add_argument("role", choices=["ADMIN", "REGISTRATION", "MC", "SCRUTINEER",
                                         "admin", "registration", "mc", "scrutineer"])
    add_op.add_argument("--password", default=None,
                        help="omit to have a strong one generated and printed once")
    add_op.set_defaults(func=cmd_add_operator)

    seed_cmd = add("seed", help="create the demo AGM")
    seed_cmd.add_argument("--password", default="sgoa-demo",
                          help="password for the seeded operator accounts")
    seed_cmd.set_defaults(func=cmd_seed)

    pw = add("set-password", help="set an operator password")
    pw.add_argument("username")
    pw.add_argument("password")
    pw.set_defaults(func=cmd_set_password)

    add("verify-audit", help="re-hash and verify the audit chain") \
        .set_defaults(func=cmd_verify_audit)

    add("backup", help="copy the three databases into backups/") \
        .set_defaults(func=cmd_backup)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        args.func = cmd_run
        args.host = None
        args.port = None
        args.events_dir = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
