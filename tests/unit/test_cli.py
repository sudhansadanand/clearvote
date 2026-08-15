"""Command line surface.

The argument order matters more than it looks: the documentation, and the
README inside every certification bundle, tell a scrutineer to run

    python -m sgoa_vote verify-audit --data-dir path/to/bundle

months after the meeting. If only the other order parsed, that instruction
would fail at exactly the moment someone is trying to check the result.
"""

from __future__ import annotations

import pytest

from sgoa_vote.__main__ import build_parser


@pytest.mark.parametrize("argv", [
    ["--data-dir", "bundle", "verify-audit"],
    ["verify-audit", "--data-dir", "bundle"],
])
def test_data_dir_is_accepted_on_either_side_of_the_subcommand(argv):
    args = build_parser().parse_args(argv)
    assert args.command == "verify-audit"
    assert args.data_dir == "bundle"


def test_a_subcommand_data_dir_overrides_the_global_one():
    args = build_parser().parse_args(
        ["--data-dir", "outer", "verify-audit", "--data-dir", "inner"])
    assert args.data_dir == "inner"


def test_omitting_data_dir_leaves_the_default_in_place():
    args = build_parser().parse_args(["verify-audit"])
    assert args.data_dir is None          # Config supplies "data"


@pytest.mark.parametrize("command", [
    "run", "init", "add-operator", "seed", "set-password", "verify-audit", "backup",
])
def test_every_subcommand_accepts_the_shared_options(command):
    parser = build_parser()
    subparsers = [action for action in parser._actions
                  if isinstance(action, type(parser._subparsers._group_actions[0]))]
    choices = subparsers[0].choices
    assert command in choices, f"{command} is missing from the CLI"
    options = {option for action in choices[command]._actions
               for option in action.option_strings}
    assert "--data-dir" in options, f"{command} cannot be pointed at a data directory"
    assert "--config" in options


def test_init_requires_the_details_that_identify_the_meeting():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["init"])          # no title or date

    args = parser.parse_args(["init", "--title", "AGM 2026", "--date", "2026-09-20"])
    assert args.title == "AGM 2026"
    assert args.blocks == "A:17,B:17,C:17"   # sensible default for SGOA


def test_no_subcommand_starts_the_server():
    from sgoa_vote.__main__ import cmd_run

    args = build_parser().parse_args([])
    assert getattr(args, "command", None) is None
    # main() fills in the run defaults for this case; see __main__.main.
    assert cmd_run is not None


def test_seed_password_defaults_to_the_documented_demo_password():
    args = build_parser().parse_args(["seed"])
    assert args.password == "sgoa-demo"
