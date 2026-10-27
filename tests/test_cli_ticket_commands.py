"""`orbi <command>` — a registered ticket command from the terminal (Issue #1930).

Registering `/orbi fix-base` gives both spellings: a comment on the ticket
and `orbi fix-base --repo … --issue …` on the host. Both run the ONE
execution path (`ticket_command.execute_command`): the same permission
gate, the same `validate`, the same `apply` and exactly one receipt on
the ticket — the parity assertions below compare the two worlds call for
call and receipt for receipt.

Here, through the real CLI entry (`orbi.cli.main`) over the in-memory
GitHub fake:

- a waiting base-red ticket: the CLI opens the same triage Issue and
  leaves the same reply a `/orbi fix-base` comment leaves, exactly once;
- a login without write permission: a non-zero exit, one rejection
  receipt with the same reason the comment path writes, and the printed
  text IS that receipt;
- the generated argparse tree: one subcommand per registered arity-0
  command and no hand edit, while `milestone` keeps only
  `orbi milestone set`.
"""
import re

import pytest

import orbi.base_ci_red as base_ci_red
from orbi import cli, ticket_command
from orbi.cli_parser import build_parser
from tests.fakes.github import FakeGh
from tests.test_base_ci_red import (
    BASE_SHA,
    CHECK,
    REPO,
    _blocks_issue,
    base_run,
)
from tests.test_cli_milestone import make_world
from tests.test_ticket_command import _registered
from seam import seam

ISSUE = 1825
MILESTONE = "v0.7.15"
OPERATOR = "alice"
MARKER_LINE = re.compile(r"^<!-- orbi-fix_base-command[^>]*-->\n?", re.MULTILINE)
ORIGIN_LINE = re.compile(r"^- command comment: .*$", re.MULTILINE)


def world(monkeypatch, *, permission: str = "write") -> FakeGh:
    """The paused base-red ticket, answered by the in-memory GitHub fake.

    The fake's own login is the operator's, so `authenticated_github_login`
    answers it the way a real `gh` credential store would: the CLI caller
    IS that login, and its repository permission decides the gate.
    """
    gh = FakeGh(REPO, login=OPERATOR)
    gh.add_issue(ISSUE, title="paused delivery", labels=("ai-blocked",))
    gh.add_collaborator(OPERATOR, permission)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def paused(gh: FakeGh, *, comments: tuple[tuple[str, str], ...] = ()) -> None:
    """The scene both spellings must act on: a waiting base-CI-red block."""
    _blocks_issue(gh, ISSUE, login=OPERATOR)
    gh.add_check_runs(BASE_SHA, [base_run()])
    gh.add_check_runs("head", [])
    gh.add_milestone(7, title=MILESTONE)
    gh.issues[ISSUE]["milestone"] = 7
    for body, login in comments:
        gh.comment(ISSUE, body, login=login)


def triage_issues(gh: FakeGh) -> dict[int, dict]:
    return {
        number: issue for number, issue in gh.issues.items()
        if number != ISSUE
    }


def replies(gh: FakeGh, marker: str = "orbi-fix_base-command") -> list[str]:
    """The receipts the command left on the paused ticket, in order."""
    return [
        comment["body"] for comment in gh.issues[ISSUE]["comments"]
        if marker in comment["body"]
    ]


def without_marker(body: str) -> str:
    """The receipt TEXT: the hidden occurrence marker is not user-visible."""
    return MARKER_LINE.sub("", body).strip()


def without_origin(body: str) -> str:
    """The receipt text minus its origin line (a comment vs the CLI)."""
    return ORIGIN_LINE.sub("", without_marker(body)).strip()


def reason_of(body: str) -> str:
    return next(
        line for line in body.splitlines() if line.startswith("- reason: ")
    )


# --- acceptance 1: the CLI does what the comment does --------------------


def test_the_cli_leaves_the_same_result_and_receipt_as_a_comment(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path, milestone=MILESTONE, repo=REPO)

    commented = world(monkeypatch)
    paused(commented, comments=(("/orbi fix-base", OPERATOR),))
    base_ci_red.recover(
        REPO, language_code="en", milestone=MILESTONE,
        dispatch_label="ai-ready",
    )

    terminal = world(monkeypatch)
    paused(terminal)
    exit_code = cli.main([
        "fix-base", "--repo", REPO, "--issue", str(ISSUE),
        "--config", str(config_path),
    ])
    assert exit_code == 0
    assert capsys.readouterr().err == ""

    # The same ticket result: one triage Issue with the same title, body,
    # labels and Milestone.
    assert len(triage_issues(commented)) == 1
    assert len(triage_issues(terminal)) == 1
    comment_created = next(iter(triage_issues(commented).values()))
    cli_created = next(iter(triage_issues(terminal).values()))
    assert cli_created["title"] == comment_created["title"] == (
        base_ci_red.triage_title(CHECK, "release/7")
    )
    assert cli_created["body"] == comment_created["body"]
    assert cli_created["labels"] == comment_created["labels"]
    assert cli_created["milestone"] == comment_created["milestone"] == 7

    # …and exactly one receipt, with the same text.
    assert len(replies(commented)) == 1
    assert len(replies(terminal)) == 1
    assert without_marker(replies(terminal)[0]) == (
        without_marker(replies(commented)[0])
    )


def test_a_second_cli_run_reuses_the_triage_issue_instead_of_duplicating_it(
    tmp_path, monkeypatch,
):
    """The command's own idempotency record is the receipt: a second
    invocation (like a second comment) never opens a second fix Issue for
    the same check — it reuses the open one."""
    config_path = make_world(tmp_path, milestone=MILESTONE, repo=REPO)
    gh = world(monkeypatch)
    paused(gh)
    argv = [
        "fix-base", "--repo", REPO, "--issue", str(ISSUE),
        "--config", str(config_path),
    ]
    assert cli.main(argv) == 0
    assert len(replies(gh)) == 1
    assert cli.main(argv) == 0
    assert len(triage_issues(gh)) == 1


# --- acceptance 2: the same write-permission rule -----------------------


def test_the_cli_rejects_a_login_without_write_permission(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path, milestone=MILESTONE, repo=REPO)

    commented = world(monkeypatch, permission="read")
    paused(commented, comments=(("/orbi fix-base", "stranger"),))
    commented.add_collaborator("stranger", "read")
    base_ci_red.recover(
        REPO, language_code="en", milestone=MILESTONE,
        dispatch_label="ai-ready",
    )
    comment_receipt = replies(
        commented, marker="orbi-fix_base-command-rejected",
    )
    assert len(comment_receipt) == 1

    terminal = world(monkeypatch, permission="read")
    paused(terminal)
    exit_code = cli.main([
        "fix-base", "--repo", REPO, "--issue", str(ISSUE),
        "--config", str(config_path),
    ])
    printed = capsys.readouterr().err
    assert exit_code != 0

    cli_receipt = replies(terminal, marker="orbi-fix_base-command-rejected")
    assert len(cli_receipt) == 1
    # The receipt the operator sees is the receipt the ticket carries.
    assert printed.strip() == cli_receipt[0].strip()
    # The CLI caller is named, and the reason is the comment path's reason.
    assert "- command comment: the `orbi` CLI on the host" in cli_receipt[0]
    assert f"(login @{OPERATOR})" in cli_receipt[0]
    assert reason_of(cli_receipt[0]) == reason_of(comment_receipt[0])
    assert without_origin(cli_receipt[0]) == without_origin(
        comment_receipt[0]
    )
    # Nothing was created by the rejected invocation.
    assert triage_issues(terminal) == {}
    assert triage_issues(commented) == {}


def test_the_cli_rejection_needs_no_comment_on_the_ticket(
    tmp_path, monkeypatch, capsys,
):
    """A command that cannot run on THIS ticket is rejected with its own
    reason — the record comes from the ticket, not from a placeholder the
    CLI would have to invent."""
    config_path = make_world(tmp_path, milestone=MILESTONE, repo=REPO)
    gh = world(monkeypatch)
    gh.add_issue(ISSUE, title="a plain ticket")
    exit_code = cli.main([
        "fix-base", "--repo", REPO, "--issue", str(ISSUE),
        "--config", str(config_path),
    ])
    printed = capsys.readouterr().err
    assert exit_code != 0
    rejected = replies(gh, marker="orbi-fix_base-command-rejected")
    assert len(rejected) == 1
    assert "no `orbi:base-ci-red:v1` block" in rejected[0]
    assert printed.strip() == rejected[0].strip()


# --- acceptance 3: the generated argparse tree --------------------------


def test_help_lists_the_generated_command_and_milestone_once(capsys):
    """`orbi --help`: one entry per generated command, and `milestone`
    still names its own `set` — never a generated duplicate."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    usage = capsys.readouterr().out
    choices = re.search(r"\{([^}]*)\}", usage)
    assert choices, f"no subcommand choices in --help usage: {usage}"
    names = choices.group(1).split(",")
    assert names.count("fix-base") == 1
    assert names.count("milestone") == 1
    assert "set" not in names


def test_the_generated_subcommand_takes_the_repo_and_the_issue():
    parser = build_parser("test")
    args = parser.parse_args([
        "fix-base", "--repo", REPO, "--issue", str(ISSUE),
    ])
    assert args.command == "fix-base"
    assert args.ticket_command == "fix_base"
    assert args.repo == REPO
    assert args.issue == ISSUE


def test_only_arity_zero_commands_are_generated():
    """`/orbi milestone <version>` keeps its own `orbi milestone set`:
    the generated entry has no argument slot, so it is arity 0 only."""
    assert [spec.name for spec in ticket_command.cli_specs()] == ["fix_base"]
    assert ticket_command.TICKET_COMMANDS["milestone"].arity == 1


def test_a_new_registration_gets_a_subcommand_without_a_cli_edit():
    """Acceptance: registering a command gives both spellings — the tree
    is generated from the registry, never edited per command."""
    spec = ticket_command.CommandSpec(
        name="probe", subcommand="probe", arity=0, permission="write",
        validate=lambda argument, **_context: None,
        apply=lambda argument, **_context: None,
        usage="/orbi probe", description="probe command",
    )
    with _registered(spec):
        parser = build_parser("test")
        args = parser.parse_args([
            "probe", "--repo", REPO, "--issue", str(ISSUE),
        ])
    assert args.command == "probe"
    assert args.ticket_command == "probe"
