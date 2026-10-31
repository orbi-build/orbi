"""The shared in-ticket command layer (Issue #1294, Issue #1922).

`/milestone` was the first in-ticket command; Issue #1294 lifted the
generic machinery (matching, permission gate, receipts) into
`orbi.ticket_command` so a second command is a registration, not a copy.
Issue #1922 gave the layer the `/orbi` prefix and the per-spec arity, so a
command supplies its `subcommand` and declares any legacy alias. These
tests drive that layer through a FICTIONAL command, `/orbi noop`, so the
guarantees are pinned independently of the milestone's business rules:

- a command supplies spellings, never a regex — the shared layer builds the
  anchored pattern and owns the prefix;
- registration is the validation (duplicate name, duplicate subcommand or
  alias, unknown permission, bad arity fail at import time) and registers
  the command's journal events;
- the dispatcher is the only path to a handler;
- the rejections are one readable receipt per occurrence, idempotent;
- `TICKET_COMMANDS` matches the command tables in the EN and ZH docs
  (`docs/operations.mdx`, `docs/zh/operations.mdx`).
"""
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orbi import journal, milestone_command
from orbi import ticket_command, ticket_command_registry

from tests.seam import seam

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _repository_permissions(monkeypatch):
    """Issue #1806: this layer's trusted actors are repository writers.

    One place for the whole module: the authors these tests write as can
    write to the repository, everyone else (`outsider`) cannot — the
    permission gate never drowns the behavior under test. A test that
    needs another answer patches the read itself (a later
    `monkeypatch` wins).
    """
    grants = {"alice": "admin", "bob": "write", "orbi-build": "admin"}
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: grants.get(login, "none"),
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "orbi-bot[bot]",
    )


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


@contextlib.contextmanager
def _registered(spec):
    """Register `spec` in the REAL registry and journal for one test.

    No registry is patched: the point of the acceptance test below is that
    `register_command` alone wires everything (Issue #1922), so the test
    runs against the real `TICKET_COMMANDS` / `JOURNAL_EVENTS` and undoes
    only what it added.
    """
    ticket_command.register_command(spec)
    try:
        yield spec
    finally:
        ticket_command.TICKET_COMMANDS.pop(spec.name, None)
        for suffix in ("applied", "failed"):
            journal.JOURNAL_EVENTS.pop(f"{spec.name}_command_{suffix}", None)


def _noop_spec():
    return ticket_command.CommandSpec(
        name="noop",
        subcommand="noop",
        arity=1,
        permission="write",
        validate=_noop_validate,
        apply=_noop_apply,
        usage="/orbi noop <target>",
        description="no-op probe for the shared command layer",
        # A message-catalog key: the receipt renders the label in the
        # ticket's language (Issue #1704).
        argument_label="command.argument_label.argument",
    )


# --- matching is shared, never a command-supplied regex ------------------


def test_the_shared_layer_owns_the_prefix_and_compiles_the_pattern():
    spec = _noop_spec()
    assert ticket_command.parse_commands(
        "please run this:\n/orbi noop alpha", spec,
    ) == ["alpha"]
    # Case-insensitive, like every spelling before it.
    assert ticket_command.parse_commands("/ORBI NOOP alpha", spec) == ["alpha"]
    # A quoted reply and a fenced example never trigger.
    assert ticket_command.parse_commands("> /orbi noop alpha", spec) == []
    assert ticket_command.parse_commands(
        "how:\n```\n/orbi noop alpha\n```\n", spec,
    ) == []
    assert ticket_command.parse_commands("use `/orbi noop alpha`", spec) == []
    # A malformed line is readable as a command attempt, so it gets a reason.
    assert ticket_command.parse_commands("/orbi noop", spec) == [None]
    assert ticket_command.parse_commands("/orbi noop a b", spec) == [None]
    # An indented line is never a command, and a longer word is not this
    # subcommand.
    assert ticket_command.parse_commands("  /orbi noop alpha", spec) == []
    assert ticket_command.parse_commands("/orbi noops alpha", spec) == []
    # The bare subcommand without the prefix is not a command any more
    # (a spec only matches `/orbi <subcommand>` or a declared alias).
    assert ticket_command.parse_commands("/noop alpha", spec) == []
    assert ticket_command.parse_commands("/noop", spec) == []


def test_an_unknown_orbi_word_is_not_a_command():
    """Acceptance: `/orbi unknown` produces no action and no receipt."""
    spec = _noop_spec()
    assert ticket_command.parse_commands("/orbi unknown", spec) == []
    assert ticket_command.parse_commands("/orbi unknown alpha", spec) == []
    assert ticket_command.parse_commands("/orbi", spec) == []


def test_an_arity_zero_command_takes_no_argument():
    spec = ticket_command.CommandSpec(
        name="zero", subcommand="zero", arity=0, permission="write",
        validate=lambda *a, **k: None, apply=lambda *a, **k: None,
        usage="/orbi zero", description="x",
    )
    assert ticket_command.parse_commands("/orbi zero", spec) == [""]
    assert ticket_command.parse_commands("/orbi zero  ", spec) == [""]
    # An argument this command cannot take is still a command attempt.
    assert ticket_command.parse_commands("/orbi zero please", spec) == [None]


def test_an_arity_zero_malformed_receipt_does_not_ask_for_an_argument():
    """The rejection a no-argument command owes its author must match the
    usage it prints beside it (Issue #1922): the shared "write exactly one
    {label}" wording would tell the author to add an argument the command
    cannot take."""
    spec = ticket_command.CommandSpec(
        name="zero", subcommand="zero", arity=0, permission="write",
        validate=lambda *a, **k: None, apply=lambda *a, **k: None,
        usage="/orbi zero", description="x",
    )
    target, rejections = ticket_command.select_command(
        [_comment("/orbi zero please")], spec, repo="owner/repo",
    )
    assert target is None
    reason = rejections[0][2]
    assert "`/orbi zero`" in reason
    assert "takes no argument" in reason
    assert "exactly one" not in reason


def test_a_command_spec_carries_no_raw_regex():
    import dataclasses
    fields = {field.name for field in dataclasses.fields(ticket_command.CommandSpec)}
    assert "regex" not in fields and "pattern" not in fields


def test_registration_rejects_a_spec_without_name_or_subcommand():
    for name, subcommand in (("", "noop"), ("noop", "")):
        with pytest.raises(ticket_command.TicketCommandRegistryError):
            ticket_command.register_command(ticket_command.CommandSpec(
                name=name, subcommand=subcommand, arity=1, permission="write",
                validate=lambda *a, **k: None, apply=lambda *a, **k: None,
                usage="/orbi noop", description="x",
            ))


def test_registration_rejects_an_arity_other_than_zero_or_one():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="arity"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", subcommand="other", arity=2, permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/orbi other", description="x",
        ))


# --- registration is the validation --------------------------------------


def test_registration_rejects_a_duplicate_name():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="duplicate"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="milestone", subcommand="milestone2", arity=1,
            permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/orbi milestone2", description="x",
        ))


def test_registration_rejects_a_duplicate_subcommand():
    with pytest.raises(
        ticket_command.TicketCommandRegistryError, match="already registered",
    ):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", subcommand="milestone", arity=1, permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/orbi other", description="x",
        ))


def test_registration_rejects_a_duplicate_legacy_alias():
    with pytest.raises(
        ticket_command.TicketCommandRegistryError, match="alias 'milestone'",
    ):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", subcommand="other", arity=1, permission="write",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/orbi other", description="x", aliases=("milestone",),
        ))


def test_registration_rejects_an_unknown_permission_level():
    with pytest.raises(ticket_command.TicketCommandRegistryError, match="permission"):
        ticket_command.register_command(ticket_command.CommandSpec(
            name="other", subcommand="other", arity=1, permission="admin",
            validate=lambda *a, **k: None, apply=lambda *a, **k: None,
            usage="/orbi other", description="x",
        ))


def test_registration_accepts_a_valid_spec_and_then_shuts_the_door():
    spec = _noop_spec()
    with _registered(spec):
        assert ticket_command.TICKET_COMMANDS["noop"] is spec
        # Registration wired the journal events too (Issue #1922): a new
        # command needs no second edit.
        assert journal.JOURNAL_EVENTS["noop_command_applied"].endswith(
            "`/orbi noop` command ran"
        )
        assert journal.JOURNAL_EVENTS["noop_command_failed"].endswith(
            "`/orbi noop` command failed at one step"
        )
        with pytest.raises(
            ticket_command.TicketCommandRegistryError, match="duplicate",
        ):
            ticket_command.register_command(spec)


# --- selection: permission, the command's own rule, last word wins -------


def test_selection_enforces_the_declared_permission():
    comments = [_comment(
        "/orbi noop alpha", login="outsider", association="NONE",
    )]
    target, rejections = ticket_command.select_command(
        comments, _noop_spec(), repo="owner/repo", allowed_targets=["alpha"],
    )
    assert target is None
    assert len(rejections) == 1
    assert "no write permission" in rejections[0][2]
    # The receipt names the repository permission it read, never the
    # comment's authorAssociation (Issue #1806).
    assert "repository permission='none'" in rejections[0][2]
    assert "authorAssociation" not in rejections[0][2]
    assert rejections[0][1] == "alpha"


def test_selection_reports_the_commands_own_validation_reason():
    target, rejections = ticket_command.select_command(
        [_comment("/orbi noop beta")], _noop_spec(), repo="owner/repo",
        allowed_targets=["alpha"],
    )
    assert target is None
    assert "not an allowed target" in rejections[0][2]
    assert "alpha" in rejections[0][2]


def test_selection_skips_comments_carrying_the_runner_marker():
    comments = [
        _comment(
            "<!-- orbi:run=deadbeef -->\n/orbi noop alpha",
            login="orbi-build[bot]", association="NONE", cid=1,
        ),
        _comment("/orbi noop alpha", cid=2),
        _comment("/orbi noop beta", cid=3),
    ]
    target, rejections = ticket_command.select_command(
        comments, _noop_spec(), repo="owner/repo",
        allowed_targets=["alpha", "beta"],
    )
    assert rejections == []
    assert target is not None and target[1] == "beta"
    assert target[0]["id"] == 3


def test_selection_ignores_unreadable_comments_and_unreadable_authors():
    target, rejections = ticket_command.select_command(
        ["not-a-dict", None, {"body": "/orbi noop alpha"},
         _comment("/orbi noop beta"), _comment("thanks")],
        _noop_spec(), repo="owner/repo", allowed_targets=["alpha", "beta"],
    )
    # An entry that is not a comment is ignored; a comment whose author
    # cannot be read has no permission to check, so its command is
    # rejected with a receipt instead of being applied.
    assert target is not None and target[1] == "beta"
    assert len(rejections) == 1
    assert "no write permission" in rejections[0][2]


# --- identity helpers -----------------------------------------------------


def test_identity_helpers_tolerate_unreadable_input():
    assert ticket_command.comment_author_login(None) is None
    assert ticket_command.comment_author_login({"author": "alice"}) is None
    assert ticket_command.comment_author_login({"author": {"login": ""}}) is None
    assert ticket_command.comment_author_login(
        {"author": {"login": "alice"}},
    ) == "alice"


def test_the_login_key_normalizes_every_payload_shape():
    """One authorship rule (Issue #1992): the three shapes GitHub reports
    for the same App account compare equal."""
    assert ticket_command.login_key("app/orbi-build") == "orbi-build"
    assert ticket_command.login_key("orbi-build[bot]") == "orbi-build"
    assert ticket_command.login_key("orbi-build") == "orbi-build"


def test_runner_comment_recognition_tolerates_unreadable_input():
    assert ticket_command.is_runner_comment(None) is False
    assert ticket_command.is_runner_comment("not-a-dict") is False
    assert ticket_command.is_runner_comment({"body": None}) is False
    assert ticket_command.is_runner_comment({"body": "thanks"}) is False
    assert ticket_command.is_runner_comment(
        {"body": "<!-- orbi:run=deadbeef -->\nhi"},
    ) is True
    assert ticket_command.is_runner_comment(
        {"body": "<!-- orbi-milestone-command-rejected comment=7 -->"},
    ) is True


# --- dispatch: receipts, idempotency, events -----------------------------


def _patch_dispatch(monkeypatch, comments, posts, events):
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
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
        _comment("/orbi noop alpha", login="outsider", association="NONE", cid=11),
        _comment("/orbi noop beta", cid=12),
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
    comments = [_comment("/orbi noop alpha", cid=31)]
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
    comments = [_comment("/orbi noop fail", cid=41)]
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, comments, posts, events)
    ticket_command.process_commands(
        "owner/repo", 436, commands=[_noop_spec()],
        allowed_targets=["fail"], applied=[],
    )
    body = posts[0][posts[0].index("--body") + 1]
    assert "`/orbi noop fail` command failed" in body
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


def test_a_third_command_needs_only_a_spec_and_a_registration(
    monkeypatch, caplog,
):
    """Acceptance: a new subcommand is one `CommandSpec` plus
    `register_command` — no edit to the matcher, the dispatcher or the
    journal registry — and `/orbi <name> arg` reaches its handler through
    the REAL `event()` (only the GitHub reads are faked)."""
    applied: list = []
    spec = ticket_command.CommandSpec(
        name="third", subcommand="third", arity=1, permission="write",
        validate=lambda argument, **_context: None,
        apply=lambda argument, *, applied, **_context: applied.append(argument),
        usage="/orbi third <target>", description="a third command",
    )
    comments = [_comment("/orbi third alpha", cid=51)]
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    with _registered(spec), caplog.at_level(logging.INFO):
        ticket_command.process_commands(
            "owner/repo", 436, commands=[spec], applied=applied,
        )
    assert applied == ["alpha"]
    assert "third_command_applied" in caplog.text
    assert "third=alpha" in caplog.text


def test_a_receipt_names_the_comment_without_an_id_or_url(monkeypatch):
    comments = [{
        "body": "/orbi noop beta",
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
        "/orbi noop alpha", login="outsider", association="NONE", cid=11,
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


# --- the context a command cannot run without (Issue #1939) --------------
#
# A command whose `validate`/`apply` read caller-supplied context (the
# milestone command's candidate set comes from its confirmation flow) must
# never be handed a missing keyword: the dispatcher rejects the occurrence
# with a readable receipt, so one ticket's command cannot abort the scan
# that is also serving another ticket's command.

def _needs_context_spec(applied: list) -> ticket_command.CommandSpec:
    return ticket_command.CommandSpec(
        name="needsctx",
        subcommand="needsctx",
        arity=1,
        permission="write",
        validate=lambda target, *, candidate_titles, **_context: (
            None if target in candidate_titles else "not a candidate"
        ),
        apply=lambda target, *, candidate_titles, **_context: applied.append(
            target,
        ),
        usage="/orbi needsctx <target>",
        description="a probe whose caller must supply its context",
        required_context=("candidate_titles",),
    )


def test_a_command_without_the_context_it_needs_is_rejected_not_crashed(
    monkeypatch,
):
    applied: list = []
    spec = _needs_context_spec(applied)
    posts: list = []
    events: list = []
    _patch_dispatch(
        monkeypatch, [_comment("/orbi needsctx alpha", cid=71)], posts, events,
    )

    ticket_command.process_commands("owner/repo", 436, commands=[spec])

    body = posts[0][posts[0].index("--body") + 1]
    assert "comment=71" in body
    assert "`/orbi needsctx` command not applied" in body
    assert "needs context this ticket does not carry" in body
    assert "candidate_titles" in body
    assert applied == []
    assert events == []


def test_the_context_a_command_declares_it_needs_still_runs_with_it(
    monkeypatch,
):
    applied: list = []
    spec = _needs_context_spec(applied)
    posts: list = []
    events: list = []
    _patch_dispatch(
        monkeypatch, [_comment("/orbi needsctx alpha", cid=72)], posts, events,
    )
    with _registered(spec):
        ticket_command.process_commands(
            "owner/repo", 436, commands=[spec],
            candidate_titles=["alpha"],
        )

    assert applied == ["alpha"]
    assert posts == []
    assert events == [(
        "needsctx_command_applied",
        {"repo": "owner/repo", "needsctx": "alpha", "issue": "#436"},
    )]


# --- the terminal spelling is the same execution path (Issue #1930) -----


def test_the_command_supplies_the_context_its_caller_cannot(monkeypatch):
    """A CLI caller has no ticket scan to inherit context from: the spec
    reads it from the ticket itself, so the CLI never passes a
    placeholder."""
    seen: list[object] = []
    reads: list[int] = []

    def context(*, comments, record=None, **_context):
        if record is not None:
            return {}
        reads.append(len(comments))
        return {"record": "from the ticket"}

    spec = ticket_command.CommandSpec(
        name="ctx", subcommand="ctx", arity=0, permission="write",
        validate=lambda argument, *, record=None, **_c: None,
        apply=lambda argument, *, record=None, **_c: seen.append(record),
        usage="/orbi ctx", description="a command with its own context",
        context=context,
    )
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, [], posts, events)

    outcome = ticket_command.execute_command(
        "owner/repo", 436, spec,
        caller=ticket_command.cli_caller(
            "alice", now=datetime(2026, 10, 27, 11, 45, tzinfo=timezone.utc),
        ),
    )
    assert outcome.applied is True
    assert outcome.receipt is None
    assert seen == ["from the ticket"]
    assert reads == [0]

    # A caller that already located the value pays no ticket read.
    ticket_command.execute_command(
        "owner/repo", 436, spec, comments=[_comment("/orbi ctx", cid=61)],
        record="from the scan",
    )
    assert seen == ["from the ticket", "from the scan"]
    assert reads == [0]


def test_a_cli_occurrence_is_named_in_its_receipt_and_printed(monkeypatch):
    """The rejection names where the command came from, and the caller
    that SHOWS the receipt (the CLI) gets the text the ticket carries."""
    posts: list = []
    events: list = []
    _patch_dispatch(monkeypatch, [], posts, events)
    # `outsider` is the module fixture's read-only login: the CLI caller is
    # judged by the same repository-permission gate as a comment author.
    outcome = ticket_command.execute_command(
        "owner/repo", 436, _noop_spec(),
        caller=ticket_command.cli_caller(
            "outsider",
            now=datetime(2026, 10, 27, 11, 45, tzinfo=timezone.utc),
        ),
        allowed_targets=["alpha"],
    )
    body = posts[0][posts[0].index("--body") + 1]
    assert "comment=cli@2026-10-27T11:45:00Z" in body
    assert (
        "- command comment: the `orbi` CLI on the host (login @outsider)"
        in body
    )
    assert outcome.applied is False
    assert outcome.receipt == body


def test_the_cli_specs_are_the_arity_zero_commands():
    """``/orbi milestone <version>`` needs an argument, so it keeps its own
    ``orbi milestone set`` instead of a generated entry."""
    specs = ticket_command_registry.cli_specs()
    assert [spec.name for spec in specs] == [
        "fix_base", "amend", "split",
    ]
    assert all(spec.arity == 0 for spec in specs)


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
    assert spec.subcommand == "milestone"
    assert spec.arity == 1
    # The pre-#1922 spelling stays a declared alias, not a second command.
    assert spec.aliases == ("milestone",)
    assert spec.canonical == "/orbi milestone"
    assert spec.permission == "write"


def test_the_machine_id_stays_separate_from_the_typed_subcommand():
    """Receipts already posted and journal consumers key on the machine id
    (Issue #1922): it must keep matching what shipped before the prefix."""
    fix_base = ticket_command.TICKET_COMMANDS["fix_base"]
    assert fix_base.name == "fix_base"
    assert fix_base.subcommand == "fix-base"
    assert ticket_command.command_receipt_marker(fix_base) == (
        "orbi-fix_base-command"
    )
    assert "fix_base_command_applied" in journal.JOURNAL_EVENTS
    assert "fix_base_command_failed" in journal.JOURNAL_EVENTS


def test_a_declared_alias_matches_but_is_not_the_canonical_form():
    spec = ticket_command.CommandSpec(
        name="legacy", subcommand="legacy", arity=1, permission="write",
        validate=lambda *a, **k: None, apply=lambda *a, **k: None,
        usage="/orbi legacy <target>", description="x", aliases=("old",),
    )
    assert ticket_command.parse_commands("/orbi legacy alpha", spec) == ["alpha"]
    assert ticket_command.parse_commands("/old alpha", spec) == ["alpha"]
    assert ticket_command.parse_commands("/oldish alpha", spec) == []
    assert spec.command_text("alpha") == "/orbi legacy alpha"
    assert spec.command_text() == "/orbi legacy"


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
