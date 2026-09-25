"""The shared in-ticket command layer (Issue #1294).

`/milestone` was the first in-ticket command; Issue #1294 lifted the
generic machinery (matching, permission gate, receipts) into
`orbi.ticket_command` so a second command is a registration, not a copy.
These tests drive that layer through a FICTIONAL command, `/noop`, so the
guarantees are pinned independently of the milestone's business rules:

- a command supplies verbs, never a regex — the shared layer builds the
  anchored pattern;
- registration is the validation (duplicate name, overlapping verb,
  unknown permission fail at import time);
- the dispatcher is the only path to a handler;
- the rejections are one readable receipt per occurrence, idempotent;
- `TICKET_COMMANDS` matches the command tables in the EN and ZH docs
  (`docs/operations.mdx`, `docs/zh/operations.mdx`).
"""
import logging
from pathlib import Path

import pytest

from orbi import milestone_command
from orbi import ticket_command

from tests.seam import seam

ROOT = Path(__file__).resolve().parent.parent


def _comment(body, *, login="alice", association="MEMBER", cid=1, url=None):
    comment = {
        "id": cid,
        "author": {"login": login},
        "authorAssociation": association,
        "body": body,
    }
    if url is not None:
        comment["url"] = url
    return comment


def _noop_validate(target, *, allowed_targets, **_context):
    if target not in set(allowed_targets):
        return (
            f"`{target}` is not an allowed target; "
            f"candidates: {', '.join(allowed_targets) or '(none)'}"
        )
    return None


def _noop_apply(target, *, repo, issue_number, applied, **_context):
    if target == "fail":
        raise ticket_command.TicketCommandError("noop step", "boom")
    applied.append((repo, issue_number, target))


def _noop_spec():
    return ticket_command.CommandSpec(
        name="noop",
        verbs=("noop",),
        permission="write",
        validate=_noop_validate,
        apply=_noop_apply,
        usage="/noop <target>",
        description="no-op probe for the shared command layer",
        argument_label="target",
    )


# --- matching is shared, never a command-supplied regex ------------------


def test_the_shared_layer_compiles_the_pattern_from_verbs_alone():
    spec = _noop_spec()
    assert ticket_command.parse_commands(
        "please run this:\n/noop alpha", spec,
    ) == ["alpha"]
    # A quoted reply and a fenced example never trigger.
    assert ticket_command.parse_commands("> /noop alpha", spec) == []
    assert ticket_command.parse_commands(
        "how:\n```\n/noop alpha\n```\n", spec,
    ) == []
    assert ticket_command.parse_commands("use `/noop alpha`", spec) == []
    # A malformed line is readable as a command attempt, so it gets a reason.
    assert ticket_command.parse_commands("/noop", spec) == [None]
    assert ticket_command.parse_commands("/noop a b", spec) == [None]
    # An indented line is never a command, and a longer word is not this verb.
    assert ticket_command.parse_commands("  /noop alpha", spec) == []
    assert ticket_command.parse_commands("/noops alpha", spec) == []


def test_a_command_spec_carries_no_raw_regex():
    import dataclasses
    fields = {field.name for field in dataclasses.fields(ticket_command.CommandSpec)}
    assert "regex" not in fields and "pattern" not in fields


def test_registration_rejects_a_spec_without_name_or_verbs():
    for name, verbs in (("", ("noop",)), ("noop", ())):
        with pytest.raises(ticket_command.TicketCommandRegistryError):
            ticket_command.register_command(ticket_command.CommandSpec(
                name=name, verbs=verbs, permission="write",
                validate=lambda *a, **k: None, apply=lambda *a, **k: None,
                usage="/noop", description="x",
            ))


# --- registration is the validation --------------------------------------


def test_registration_rejects_a_duplicate_name():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="duplicate"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="milestone", verbs=("milestone2",), permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/milestone2", description="x",
        ))


def test_registration_rejects_a_verb_another_command_owns():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="already registered"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", verbs=("milestone",), permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/other", description="x",
        ))


def test_registration_rejects_an_unknown_permission_level():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="permission"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", verbs=("other",), permission="admin",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/other", description="x",
        ))


def test_registration_accepts_a_valid_spec_and_then_shuts_the_door(monkeypatch):
    monkeypatch.setattr(seam, "TICKET_COMMANDS", {})
    spec = _noop_spec()
    ticket_command.register_command(spec)
    assert ticket_command.TICKET_COMMANDS == {"noop": spec}
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="duplicate"):
        ticket_command.register_command(spec)


# --- selection: permission, the command's own rule, last word wins -------


def test_selection_enforces_the_declared_permission():
    comments = [_comment(
        "/noop alpha", login="outsider", association="NONE",
    )]
    target, rejections = ticket_command.select_command(
        comments, _noop_spec(), allowed_targets=["alpha"],
        runner_login="orbi-build",
    )
    assert target is None
    assert len(rejections) == 1
    assert "no write permission" in rejections[0][2]
    assert rejections[0][1] == "alpha"


def test_selection_reports_the_commands_own_validation_reason():
    target, rejections = ticket_command.select_command(
        [_comment("/noop beta")], _noop_spec(), allowed_targets=["alpha"],
        runner_login="orbi-build",
    )
    assert target is None
    assert "not an allowed target" in rejections[0][2]
    assert "alpha" in rejections[0][2]


def test_selection_lets_the_last_word_win_and_skips_the_runner():
    comments = [
        _comment("/noop alpha", login="orbi-build[bot]", association="NONE", cid=1),
        _comment("/noop alpha", cid=2),
        _comment("/noop beta", cid=3),
    ]
    target, rejections = ticket_command.select_command(
        comments, _noop_spec(), allowed_targets=["alpha", "beta"],
        runner_login="orbi-build",
    )
    assert rejections == []
    assert target is not None and target[1] == "beta"
    assert target[0]["id"] == 3


def test_selection_ignores_unreadable_comments_and_unreadable_authors():
    target, rejections = ticket_command.select_command(
        ["not-a-dict", None, {"body": "/noop alpha", "authorAssociation": "MEMBER"},
         _comment("thanks")],
        _noop_spec(), allowed_targets=["alpha"], runner_login="orbi-build",
    )
    assert target is not None
    assert target[1] == "alpha"
    assert rejections == []


# --- identity helpers -----------------------------------------------------


def test_identity_helpers_tolerate_unreadable_input():
    assert ticket_command.comment_author_login(None) is None
    assert ticket_command.comment_author_login({"author": "alice"}) is None
    assert ticket_command.comment_author_login({"author": {"login": ""}}) is None
    assert ticket_command.comment_author_login(
        {"author": {"login": "alice"}},
    ) == "alice"
    assert ticket_command.same_github_identity(None, "alice") is False
    assert ticket_command.same_github_identity("alice[bot]", "alice") is True


# --- dispatch: receipts, idempotency, events -----------------------------


def _patch_dispatch(monkeypatch, comments, posts, events):
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    monkeypatch.setattr(
        seam, "event", lambda kind, **fields: events.append((kind, fields)),
    )


def test_dispatch_posts_one_idempotent_receipt_per_rejected_occurrence(
    monkeypatch,
):
    comments = [
        _comment("/noop alpha", login="outsider", association="NONE", cid=11),
        _comment("/noop beta", cid=12),
    ]
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["alpha"], applied=[],
    )
    assert len(posts) == 2
    bodies = [command[command.index("--body") + 1] for command in posts]
    assert "<!-- orbi-noop-command-rejected comment=11 -->" in bodies[0]
    assert "<!-- orbi-noop-command-rejected comment=12 -->" in bodies[1]
    # The receipt never re-triggers the parser.
    assert ticket_command.parse_commands(bodies[0], _noop_spec()) == []
    # Re-reading the ticket (now carrying the receipts) posts nothing new.
    comments.extend({"id": 90 + index, "body": body}
                    for index, body in enumerate(bodies))
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["alpha"], applied=[],
    )
    assert len(posts) == 2
    assert events == []


def test_dispatch_runs_the_winning_command_and_emits_the_applied_event(
    monkeypatch,
):
    comments = [_comment("/noop alpha", cid=31)]
    posts: list = []
    events: list = []
    applied: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["alpha"], applied=applied,
    )
    assert applied == [("owner/repo", 436, "alpha")]
    assert posts == []
    assert events == [(
        "noop_command_applied",
        {"repo": "owner/repo", "noop": "alpha", "issue": "#436"},
    )]


def test_dispatch_names_the_failed_step_and_emits_the_failed_event(
    monkeypatch,
):
    comments = [_comment("/noop fail", cid=41)]
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["fail"], applied=[],
    )
    body = posts[0][posts[0].index("--body") + 1]
    assert "`/noop fail` command failed" in body
    assert "- reason: boom" in body
    assert "- failed step: `noop step`" in body
    assert "<!-- orbi-noop-command-failed step=noop step comment=41 -->" in body
    assert events == [(
        "noop_command_failed",
        {
            "level": logging.ERROR, "repo": "owner/repo", "noop": "fail",
            "step": "noop step", "reason": "boom",
        },
    )]


def test_a_receipt_names_the_comment_without_an_id_or_url(monkeypatch):
    comments = [{
        "body": "/noop beta",
        "author": {"login": "outsider"},
        "authorAssociation": "NONE",
        "createdAt": "2026-09-15T01:00:00Z",
    }]
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["alpha"], applied=[],
    )
    body = posts[0][posts[0].index("--body") + 1]
    assert "comment=outsider@2026-09-15T01:00:00Z" in body
    assert "- command comment: comment by @outsider at 2026-09-15T01:00:00Z" in body


def test_a_receipt_names_a_comment_by_its_url_when_it_has_one(monkeypatch):
    comments = [_comment(
        "/noop alpha", login="outsider", association="NONE", cid=11,
        url="https://github.com/owner/repo/issues/436#issuecomment-11",
    )]
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["alpha"], applied=[],
    )
    body = posts[0][posts[0].index("--body") + 1]
    assert "comment=11" in body
    assert (
        "- command comment: https://github.com/owner/repo/issues/436#issuecomment-11"
        in body
    )


# --- the registry is pinned to the docs -----------------------------------


def _docs_command_table(page_text, heading):
    lines = page_text.splitlines()
    assert heading in lines, f"missing heading: {heading}"
    index = lines.index(heading) + 1
    while index < len(lines) and not lines[index].startswith("|"):
        index += 1
    rows = {}
    for line in lines[index:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        name = cells[0].strip("`")
        if not name or name == "Command" or set(name) <= {"-", ":"}:
            continue
        rows[name] = {
            "usage": cells[1].strip("`"),
            "permission": cells[2].strip("`"),
            "description": cells[3],
        }
    return rows


def _expected_rows():
    return {
        spec.name: {
            "usage": spec.usage,
            "permission": spec.permission,
            "description": spec.description,
        }
        for spec in ticket_command.TICKET_COMMANDS.values()
    }


def test_the_docs_table_parser_handles_odd_rows_and_the_table_end():
    page = (
        "### In-ticket commands\n\n"
        "| Command | Usage | Permission | Description |\n"
        "| --- | --- | --- |\n"
        "| `one` | `/one` | `write` | first command |"
    )
    assert _docs_command_table(page, "### In-ticket commands") == {
        "one": {
            "usage": "/one", "permission": "write",
            "description": "first command",
        },
    }


def test_the_registry_matches_the_english_docs_table():
    page = (ROOT / "docs" / "operations.mdx").read_text(encoding="utf-8")
    assert _docs_command_table(page, "### In-ticket commands") == _expected_rows()


def test_the_registry_matches_the_chinese_docs_table():
    page = (ROOT / "docs" / "zh" / "operations.mdx").read_text(encoding="utf-8")
    assert _docs_command_table(page, "### 票内命令") == _expected_rows()


def test_milestone_is_the_first_registrant():
    assert "milestone" in ticket_command.TICKET_COMMANDS
    spec = ticket_command.TICKET_COMMANDS["milestone"]
    assert spec is milestone_command.MILESTONE_COMMAND
    assert spec.verbs == ("milestone",)
    assert spec.permission == "write"


def test_the_generic_layer_left_the_milestone_module():
    source = (ROOT / "src" / "orbi" / "milestone_command.py").read_text(
        encoding="utf-8",
    )
    for name in (
        "parse_milestone_commands", "select_milestone_command",
        "process_milestone_commands", "strip_fenced_code_blocks",
        "_post_command_receipt", "MILESTONE_COMMAND_RE",
        "MilestoneCommandError",
    ):
        assert name not in source, name


def test_the_dispatch_has_exactly_one_call_site():
    source = (ROOT / "src" / "orbi" / "milestone.py").read_text(encoding="utf-8")
    assert source.count("process_commands(") == 1
