"""In-ticket `/milestone <version>` command (Issue #1290).

With `auto_next_milestone = false` the engine posts a confirmation ticket and
waits. The ticket used to tell a human to run `orbi milestone set` — which a
hosted sandbox tenant cannot do (no host access, no CLI). The ticket now
carries the command itself: a comment line `/milestone vX.Y.Z` runs the same
three deterministic steps a maintainer would.

The tests below drive the real entry points:
`process_milestone_commands` (parsing, authorization, receipts) and
`apply_milestone_command` (create milestone, open the release ticket from the
repository template, land `active_milestone`) against a stateful fake `gh`,
plus the idle-path wiring in `advance_active_milestone_on_idle`.
"""
import base64
import json
import logging
from pathlib import Path

import pytest

import orbi.runner as runner
from orbi import milestone
from orbi import milestone_command
from orbi.delivery_labels import READY_LABEL, RELEASE_LABEL
from orbi.release import parse_release_declaration
from orbi.repo_config import RepoPolicy

from tests.fakes.milestone_gh import FakeMilestoneGh
from tests.seam import seam

ROOT = Path(__file__).resolve().parent.parent
REAL_TEMPLATE = (ROOT / ".github" / "release-ticket-template.md").read_text(
    encoding="utf-8",
)


def _comment(body, *, login="alice", association="MEMBER", cid=1, url=None):
    comment = {
        "id": cid,
        "body": body,
        "author": {"login": login},
        "authorAssociation": association,
        "createdAt": "2026-09-22T00:00:00Z",
    }
    if url is not None:
        comment["url"] = url
    return comment


# --- matching rules -------------------------------------------------------


def test_command_matches_any_line_not_only_the_comment_start():
    assert milestone_command.parse_milestone_commands(
        "first line\n\nplease run this:\n/milestone v0.5.41\nthanks"
    ) == ["v0.5.41"]


def test_command_allows_trailing_arguments_and_whitespace():
    assert milestone_command.parse_milestone_commands("/milestone\tv0.5.41  ") == [
        "v0.5.41"
    ]


def test_quoted_reply_does_not_trigger():
    # GitHub's "Quote reply" prefixes every quoted line with `>`, which
    # pushes `/` off the line start.
    assert milestone_command.parse_milestone_commands("> /milestone v0.5.41") == []
    assert milestone_command.parse_milestone_commands(">\t/milestone v0.5.41") == []


def test_fenced_code_blocks_do_not_trigger():
    assert milestone_command.parse_milestone_commands(
        "how to advance:\n```\n/milestone v0.5.41\n```\n"
    ) == []
    assert milestone_command.parse_milestone_commands(
        "~~~\n/milestone v0.5.41\n~~~"
    ) == []
    assert milestone_command.parse_milestone_commands(
        "```bash\n/milestone v0.5.41\n```"
    ) == []


def test_a_real_command_after_a_fenced_example_still_triggers():
    assert milestone_command.parse_milestone_commands(
        "example:\n```\n/milestone v0.5.40\n```\n/milestone v0.5.41"
    ) == ["v0.5.41"]


def test_inline_code_span_does_not_trigger():
    assert milestone_command.parse_milestone_commands("use `/milestone v0.5.41`") == []


def test_a_malformed_command_line_is_reported_not_ignored():
    # A `/milestone` line that carries no single version is still a command
    # attempt: it is parsed as `None` so its author gets a readable reason.
    assert milestone_command.parse_milestone_commands("/milestone") == [None]
    # An indented line is never a command (it may be an indented code block).
    assert milestone_command.parse_milestone_commands("  /milestone  ") == []
    assert milestone_command.parse_milestone_commands(
        "/milestone v0.5.41 v0.6.0"
    ) == [None]
    # A different word after the slash is not this command.
    assert milestone_command.parse_milestone_commands("/milestones v0.5.41") == []


def test_select_rejects_a_malformed_command_with_a_readable_reason():
    comments = [_comment("/milestone")]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41"], runner_login="orbi-build",
    )
    assert target is None
    assert len(rejections) == 1
    assert rejections[0][1] is None
    assert "malformed" in rejections[0][2]
    assert "`/milestone <version>`" in rejections[0][2]


def test_non_string_or_empty_bodies_are_ignored():
    assert milestone_command.parse_milestone_commands(None) == []
    assert milestone_command.parse_milestone_commands("") == []
    assert milestone_command.parse_milestone_commands(123) == []


# --- selection ------------------------------------------------------------


def test_select_requires_write_permission_and_reports_the_reason():
    comments = [_comment(
        "/milestone v0.5.41", login="outsider", association="NONE",
    )]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41"], runner_login="orbi-build",
    )
    assert target is None
    assert len(rejections) == 1
    assert "no write permission" in rejections[0][2]
    assert rejections[0][1] == "v0.5.41"


def test_select_rejects_a_version_outside_the_candidate_set():
    comments = [_comment("/milestone v9.9.9")]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41", "v0.6.0"], runner_login="orbi-build",
    )
    assert target is None
    assert "not an open milestone above the current one" in rejections[0][2]
    assert "v0.5.41, v0.6.0" in rejections[0][2]


def test_select_reports_an_empty_candidate_set_readably():
    _target, rejections = milestone_command.select_milestone_command(
        [_comment("/milestone v0.5.41")], [], runner_login="orbi-build",
    )
    assert "(none)" in rejections[0][2]


def test_select_skips_the_runners_own_comments_silently():
    # The bot must never trigger itself off its own receipt text; a silent
    # skip is what keeps that from becoming a rejection loop.
    comments = [_comment(
        "/milestone v0.5.41", login="orbi-build[bot]", association="NONE",
    )]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41"], runner_login="orbi-build",
    )
    assert target is None
    assert rejections == []


def test_select_accepts_a_trusted_author():
    comments = [_comment("/milestone v0.5.41")]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41"], runner_login="orbi-build",
    )
    assert rejections == []
    assert target is not None
    assert target[1] == "v0.5.41"


def test_select_lets_the_last_command_win():
    comments = [
        _comment("/milestone v0.5.41", cid=1),
        _comment("/milestone v0.6.0", cid=2),
    ]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41", "v0.6.0"], runner_login="orbi-build",
    )
    assert rejections == []
    assert target is not None and target[1] == "v0.6.0"
    assert target[0]["id"] == 2


def test_select_lets_a_later_bad_command_shadow_an_earlier_good_one():
    comments = [
        _comment("/milestone v0.5.41", cid=1),
        _comment("/milestone v9.9.9", cid=2),
    ]
    target, rejections = milestone_command.select_milestone_command(
        comments, ["v0.5.41"], runner_login="orbi-build",
    )
    assert target is None
    assert len(rejections) == 1 and rejections[0][1] == "v9.9.9"


def test_select_ignores_non_comment_entries():
    target, rejections = milestone_command.select_milestone_command(
        ["not-a-dict", None], ["v0.5.41"], runner_login="orbi-build",
    )
    assert target is None and rejections == []


# --- rendering the release ticket ----------------------------------------


def test_render_replaces_every_placeholder_and_drops_judgement_sections():
    body = milestone_command.render_release_ticket(
        REAL_TEMPLATE, version="v0.5.41", base_branch="main",
        version_file="pyproject.toml",
    )
    assert "vX.Y.Z" not in body
    assert "## Background" not in body
    assert "## Not included in this release" not in body
    assert "Release section reference" not in body
    assert body.count("```") == 0
    declaration = parse_release_declaration(body)
    assert declaration["version"] == "v0.5.41"
    assert declaration["scope_from_milestone"] == "v0.5.41"
    assert declaration["base_branch"] == "main"
    assert declaration["version_file"] == "pyproject.toml"


def test_render_replaces_an_existing_placeholder_version_file_line():
    template = (
        "## Release\n\n"
        "- version: vX.Y.Z\n"
        "- base_branch: main\n"
        "- scope_from_milestone: vX.Y.Z\n"
        "- version_file: pyproject.toml\n"
    )
    body = milestone_command.render_release_ticket(
        template, version="v0.5.41", base_branch="main",
        version_file="package.json",
    )
    assert "pyproject.toml" not in body
    assert parse_release_declaration(body) == {
        "version": "v0.5.41",
        "base_branch": "main",
        "scope": [],
        "scope_from_milestone": "v0.5.41",
        "version_file": "package.json",
    }


def test_set_release_version_file_appends_when_the_block_is_absent():
    assert milestone_command._set_release_version_file(
        "just prose\n", "Cargo.toml",
    ) == "just prose\n- version_file: Cargo.toml\n"


def test_set_release_version_file_stays_inside_the_release_block():
    # A repository template may carry sections after `## Release`; the
    # field belongs to the machine-readable block, not to the last line.
    template = (
        "## Release\n\n"
        "- version: v0.5.41\n"
        "- base_branch: main\n\n"
        "## Notes\n\n"
        "- for maintainers\n"
    )
    assert milestone_command._set_release_version_file(template, "Cargo.toml") == (
        "## Release\n\n"
        "- version: v0.5.41\n"
        "- base_branch: main\n"
        "- version_file: Cargo.toml\n\n"
        "## Notes\n\n"
        "- for maintainers\n"
    )


def test_render_drops_the_judgement_sections_of_a_stale_template():
    # A repository whose template predates this change still carries the
    # two judgement sections; they are dropped, not merely left empty.
    template = (
        "## Preconditions\n\n- x\n\n"
        "## Background\n\nA hand-written reason.\n\n"
        "## Not included in this release\n\n- #1 deliberately out\n\n"
        "## Release\n\n- version: vX.Y.Z\n- base_branch: main\n"
        "- scope_from_milestone: vX.Y.Z\n"
    )
    body = milestone_command.render_release_ticket(
        template, version="v0.5.41", base_branch="main",
        version_file="pyproject.toml",
    )
    assert "A hand-written reason." not in body
    assert "#1 deliberately out" not in body
    assert "## Background" not in body
    assert "## Not included in this release" not in body
    assert "## Preconditions" in body and "- x" in body
    assert parse_release_declaration(body)["version"] == "v0.5.41"


def test_render_falls_back_to_the_builtin_field_set_without_a_template():
    body = milestone_command.render_release_ticket(
        None, version="v9.9.9", base_branch="release/1.x",
        version_file="package.json",
    )
    assert "no `.github/release-ticket-template.md`" in body
    assert "vX.Y.Z" not in body
    declaration = parse_release_declaration(body)
    assert declaration["version"] == "v9.9.9"
    assert declaration["base_branch"] == "release/1.x"
    assert declaration["scope_from_milestone"] == "v9.9.9"
    assert declaration["version_file"] == "package.json"


def test_read_release_ticket_template_decodes_the_contents_api(monkeypatch):
    payload = base64.b64encode(REAL_TEMPLATE.encode("utf-8")).decode("ascii")

    def fake_read(command, **kwargs):
        assert command == [
            "gh", "api",
            "repos/owner/repo/contents/.github/release-ticket-template.md"
            "?ref=main",
        ]
        return json.dumps({"content": payload})

    monkeypatch.setattr(seam, "run_gh_read_command", fake_read)
    assert milestone_command._read_release_ticket_template("owner/repo", "main") == (
        REAL_TEMPLATE
    )


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps({"content": None}),
    json.dumps({"content": "not base64!"}),
    json.dumps(["no", "object"]),
])
def test_read_release_ticket_template_fails_soft(monkeypatch, payload):
    monkeypatch.setattr(
        seam, "run_gh_read_command", lambda command, **kwargs: payload,
    )
    assert milestone_command._read_release_ticket_template("owner/repo", "main") is None


def test_read_release_ticket_template_fails_soft_on_command_error(monkeypatch):
    def fail(command, **kwargs):
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(seam, "run_gh_read_command", fail)
    assert milestone_command._read_release_ticket_template("owner/repo", "main") is None


def test_detect_version_file_picks_the_first_known_root_file(monkeypatch):
    def fake_read(command, **kwargs):
        assert command == [
            "gh", "api", "repos/owner/repo/git/trees/main",
        ]
        return json.dumps({"tree": [
            {"path": "README.md"},
            {"path": "package.json"},
            {"path": "pyproject.toml"},
        ]})

    monkeypatch.setattr(seam, "run_gh_read_command", fake_read)
    assert milestone_command._detect_version_file("owner/repo", "main") == "pyproject.toml"


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps({"tree": "nope"}),
    json.dumps({"tree": ["not-a-dict", {"path": 3}]}),
    json.dumps({"tree": [{"path": "README.md"}]}),
])
def test_detect_version_file_fails_soft(monkeypatch, payload):
    monkeypatch.setattr(
        seam, "run_gh_read_command", lambda command, **kwargs: payload,
    )
    assert milestone_command._detect_version_file("owner/repo", "main") is None


# --- the three steps ------------------------------------------------------


def test_ensure_command_milestone_creates_a_missing_one(monkeypatch):
    calls = []
    monkeypatch.setattr(
        seam, "list_milestones", lambda repo, **kwargs: [{"title": "v0.5.40"}],
    )
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: calls.append(command) or "",
    )
    milestone_command._ensure_command_milestone("owner/repo", "v0.5.41")
    assert calls == [[
        "gh", "api", "--method", "POST", "repos/owner/repo/milestones",
        "-f", "title=v0.5.41",
    ]]


def test_ensure_command_milestone_leaves_an_existing_one_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(
        seam, "list_milestones",
        lambda repo, **kwargs: [{"title": "v0.5.41", "description": "keep me"}],
    )
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: calls.append(command) or "",
    )
    milestone_command._ensure_command_milestone("owner/repo", "v0.5.41")
    assert calls == []


def test_ensure_release_ticket_creates_one_with_both_labels(monkeypatch):
    created = []
    monkeypatch.setattr(seam, "list_issues", lambda repo, **kwargs: [])
    monkeypatch.setattr(
        seam, "run_gh_read_command",
        lambda command, **kwargs: json.dumps({
            "content": base64.b64encode(REAL_TEMPLATE.encode()).decode(),
        }),
    )
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: created.append(command) or "",
    )
    milestone_command._ensure_command_release_ticket(
        "owner/repo", "v0.5.41", base_branch="main",
        dispatch_label=READY_LABEL, version_file="pyproject.toml",
    )
    assert len(created) == 1
    command = created[0]
    assert command == [
        "gh", "issue", "create", "--repo", "owner/repo",
        "--title", "release v0.5.41",
        "--body", command[command.index("--body") + 1],
        "--label", RELEASE_LABEL,
        "--label", READY_LABEL,
        "--milestone", "v0.5.41",
    ]
    body = command[command.index("--body") + 1]
    assert body.count("```") == 0
    assert "- version: v0.5.41" in body
    assert "- version_file: pyproject.toml" in body
    assert parse_release_declaration(body)["version"] == "v0.5.41"


def test_ensure_release_ticket_skips_when_one_already_covers_the_milestone(
    monkeypatch,
):
    created = []
    monkeypatch.setattr(
        seam, "list_issues", lambda repo, **kwargs: [{"number": 7}],
    )
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: created.append(command) or "",
    )
    milestone_command._ensure_command_release_ticket(
        "owner/repo", "v0.5.41", base_branch="main",
        dispatch_label=READY_LABEL, version_file="pyproject.toml",
    )
    assert created == []


def test_ensure_release_ticket_detects_the_version_file_when_unset(monkeypatch):
    created = []

    def fake_read(command, **kwargs):
        if "/contents/" in command[2]:
            return json.dumps({
                "content": base64.b64encode(REAL_TEMPLATE.encode()).decode(),
            })
        return json.dumps({"tree": [{"path": "package.json"}]})

    monkeypatch.setattr(seam, "list_issues", lambda repo, **kwargs: [])
    monkeypatch.setattr(seam, "run_gh_read_command", fake_read)
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: created.append(command) or "",
    )
    milestone_command._ensure_command_release_ticket(
        "owner/repo", "v0.5.41", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    body = created[0][created[0].index("--body") + 1]
    assert "- version_file: package.json" in body


def test_ensure_release_ticket_uses_the_builtin_set_without_a_template(
    monkeypatch,
):
    created = []

    def fake_read(command, **kwargs):
        raise RuntimeError("404")

    monkeypatch.setattr(seam, "list_issues", lambda repo, **kwargs: [])
    monkeypatch.setattr(seam, "run_gh_read_command", fake_read)
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: created.append(command) or "",
    )
    milestone_command._ensure_command_release_ticket(
        "owner/repo", "v0.5.41", base_branch="main",
        dispatch_label=READY_LABEL, version_file="pyproject.toml",
    )
    body = created[0][created[0].index("--body") + 1]
    assert "no `.github/release-ticket-template.md`" in body
    assert parse_release_declaration(body)["version"] == "v0.5.41"


def test_write_policy_active_milestone_puts_the_rewritten_file(monkeypatch):
    original = 'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.40"\n'
    reads = []
    writes = []

    def fake_read(command, **kwargs):
        reads.append(command)
        return json.dumps({
            "sha": "deadbeef",
            "content": base64.b64encode(original.encode()).decode(),
        })

    monkeypatch.setattr(seam, "run_gh_read_command", fake_read)
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: writes.append(command) or "",
    )
    milestone_command._write_policy_active_milestone(
        "owner/repo", ".github/orbi.toml", "v0.5.41", base_branch="main",
    )
    assert reads == [[
        "gh", "api", "repos/owner/repo/contents/.github/orbi.toml",
    ]]
    command = writes[0]
    message = command[command.index("-f") + 1]
    content = next(item for item in command if item.startswith("content="))
    assert message == "message=chore: set active_milestone to v0.5.41"
    assert command == [
        "gh", "api", "--method", "PUT",
        "repos/owner/repo/contents/.github/orbi.toml",
        "-f", message,
        "-f", content,
        "-f", "sha=deadbeef",
        "-f", "branch=main",
    ]
    assert base64.b64decode(content[len("content="):]).decode() == (
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.41"\n'
    )
    assert "sha=deadbeef" in command
    assert "branch=main" in command


@pytest.mark.parametrize("payload,reason", [
    ("not json", "unreadable JSON"),
    (json.dumps({"content": None}), "no file content"),
    (json.dumps({"content": base64.b64encode(b"x").decode()}), "no blob sha"),
    (json.dumps({"sha": "", "content": base64.b64encode(b"x").decode()}),
     "no blob sha"),
])
def test_write_policy_active_milestone_fails_loudly(monkeypatch, payload, reason):
    writes = []
    monkeypatch.setattr(
        seam, "run_gh_read_command", lambda command, **kwargs: payload,
    )
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: writes.append(command) or "",
    )
    with pytest.raises(RuntimeError, match=reason):
        milestone_command._write_policy_active_milestone(
            "owner/repo", ".github/orbi.toml", "v0.5.41", base_branch="main",
        )
    assert writes == []


def test_land_active_milestone_writes_the_repository_policy(monkeypatch):
    landed = []
    monkeypatch.setattr(
        seam, "_write_policy_active_milestone",
        lambda *args, **kwargs: landed.append((args, kwargs)),
    )
    milestone_command._land_active_milestone(
        "owner/repo", "v0.5.41",
        policy=RepoPolicy(active_milestone="v0.5.40"),
        policy_path=".github/orbi.toml", config_path=Path("/nope/orbi.toml"),
        base_branch="main",
    )
    assert landed == [(("owner/repo", ".github/orbi.toml", "v0.5.41"),
                       {"base_branch": "main"})]


def test_land_active_milestone_writes_the_host_config_without_policy(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    milestone_command._land_active_milestone(
        "owner/repo", "v0.5.41", policy=None,
        policy_path=".github/orbi.toml", config_path=config, base_branch="main",
    )
    assert config.read_text() == 'active_milestone = "v0.5.41"\n'


def test_land_active_milestone_falls_back_to_host_config_for_a_partial_policy(
    tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    milestone_command._land_active_milestone(
        "owner/repo", "v0.5.41", policy=RepoPolicy(dispatch_label="ai-ready"),
        policy_path=".github/orbi.toml", config_path=config, base_branch="main",
    )
    assert config.read_text() == 'active_milestone = "v0.5.41"\n'


def test_apply_milestone_command_names_the_failing_step(monkeypatch):
    completed = []

    def ok(step):
        completed.append(step)

    monkeypatch.setattr(
        seam, "_ensure_command_milestone",
        lambda repo, version: ok("milestone"),
    )

    def fail_ticket(repo, version, **kwargs):
        raise RuntimeError("template unreadable")

    monkeypatch.setattr(
        seam, "_ensure_command_release_ticket", fail_ticket,
    )
    monkeypatch.setattr(
        seam, "_land_active_milestone",
        lambda *args, **kwargs: ok("active_milestone"),
    )
    with pytest.raises(milestone_command.MilestoneCommandError) as raised:
        milestone_command.apply_milestone_command(
            "owner/repo", "v0.5.41", config_path=Path("/nope"),
            policy=None, policy_path=".github/orbi.toml", base_branch="main",
            dispatch_label=READY_LABEL, version_file=None,
        )
    assert raised.value.step == "release ticket"
    assert raised.value.reason == "template unreadable"
    # The successful first step is NOT rolled back, and the third never ran.
    assert completed == ["milestone"]


def test_apply_milestone_command_reports_all_three_steps(monkeypatch):
    completed = []
    for name in (
        "_ensure_command_milestone", "_ensure_command_release_ticket",
        "_land_active_milestone",
    ):
        monkeypatch.setattr(
        seam, name,
            lambda *args, _name=name, **kwargs: completed.append(_name),
        )
    assert milestone_command.apply_milestone_command(
        "owner/repo", "v0.5.41", config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    ) == [milestone_command._STEP_MILESTONE, milestone_command._STEP_RELEASE_TICKET,
          milestone_command._STEP_ACTIVE_MILESTONE]
    assert completed == [
        "_ensure_command_milestone", "_ensure_command_release_ticket",
        "_land_active_milestone",
    ]


# --- receipts -------------------------------------------------------------


def test_identity_helpers_reject_unreadable_input(monkeypatch):
    # The active account comes from `gh auth status`; stub that credential
    # read so the assertion pins this module's delegation and the gh argument
    # list instead of whichever account happens to be logged into the machine
    # running the suite (CI machines have none, and the real call fails there).
    commands = []
    monkeypatch.setattr(
        seam, "run_gh_read_command",
        lambda command, **kwargs: commands.append(command) or (
            "account orbi-build[bot]\nActive account: true\n"),
    )
    assert milestone_command.authenticated_login() == "orbi-build[bot]"
    assert commands == [["gh", "auth", "status", "--hostname", "github.com"]]
    assert milestone_command.comment_author_login(None) is None
    assert milestone_command.comment_author_login("just a body") is None
    assert milestone_command.same_github_identity(None, "orbi-build") is False
    assert milestone_command.same_github_identity("orbi-build", 7) is False
    assert milestone_command.same_github_identity(
        "orbi-build[bot]", "orbi-build",
    ) is True


def _receipt_body(posts, index=0):
    return posts[index][posts[index].index("--body") + 1]


def test_process_posts_one_reason_per_rejected_command(monkeypatch):
    comments = [_comment(
        "/milestone v9.9.9", login="outsider", association="NONE", cid=11,
        url="https://github.com/owner/repo/issues/436#issuecomment-11",
    )]
    posts = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    assert len(posts) == 1
    assert posts[0] == [
        "gh", "issue", "comment", "436", "--repo", "owner/repo",
        "--body", _receipt_body(posts),
    ]
    body = _receipt_body(posts)
    assert "not applied" in body
    assert "no write permission" in body
    assert (
        "- command comment: https://github.com/owner/repo/issues/436#issuecomment-11"
        in body
    )
    assert "<!-- orbi-milestone-command-rejected comment=11 -->" in body
    # A receipt must never re-trigger the parser itself.
    assert milestone_command.parse_milestone_commands(body) == []


def test_rejected_receipts_name_the_comment_without_an_id(monkeypatch):
    # A comment read from the API may carry only a url, or neither id nor
    # url; the receipt must still name the exact comment it answers.
    comments = [
        {
            "body": "/milestone v9.9.9",
            "author": {"login": "outsider"},
            "authorAssociation": "NONE",
            "createdAt": "2026-09-22T01:00:00Z",
        },
        {
            "id": 0,
            "url": "https://github.com/owner/repo/issues/436#issuecomment-7",
            "body": "/milestone v9.9.9",
            "author": {"login": "outsider"},
            "authorAssociation": "NONE",
            "createdAt": "2026-09-22T02:00:00Z",
        },
    ]
    posts = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    assert len(posts) == 2
    bodies = [_receipt_body(posts, index) for index in range(len(posts))]
    assert "comment=outsider@2026-09-22T01:00:00Z" in bodies[0]
    assert "comment by @outsider at 2026-09-22T01:00:00Z" in bodies[0]
    assert (
        "comment=https://github.com/owner/repo/issues/436#issuecomment-7"
        in bodies[1]
    )
    assert "https://github.com/owner/repo/issues/436#issuecomment-7" in bodies[1]


def test_process_answers_a_malformed_command_on_the_ticket(monkeypatch):
    comments = [_comment("/milestone v0.5.41 extra")]
    posts = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: pytest.fail("nothing to apply"),
    )
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    assert len(posts) == 1
    body = _receipt_body(posts)
    assert "not applied" in body
    assert "- command line: `/milestone`" in body
    assert "malformed" in body


def test_process_receipts_are_idempotent_per_command_comment(monkeypatch):
    first = _comment("/milestone v9.9.9", cid=11)
    comments = [first]
    posts = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    assert len(posts) == 1
    comments.append({"id": 99, "body": _receipt_body(posts)})
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )
    assert len(posts) == 1


def test_process_names_the_failed_step_on_the_ticket(monkeypatch, caplog):
    comments = [_comment("/milestone v0.5.41", cid=21)]
    posts = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: posts.append(command) or "",
    )
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            milestone_command.MilestoneCommandError("release ticket", "template missing")
        ),
    )
    with caplog.at_level(logging.ERROR):
        milestone_command.process_milestone_commands(
            "owner/repo", 436, candidate_titles=["v0.5.41"],
            config_path=Path("/nope"), policy=None,
            policy_path=".github/orbi.toml", base_branch="main",
            dispatch_label=READY_LABEL, version_file=None,
        )
    body = _receipt_body(posts)
    assert "`/milestone v0.5.41` command failed" in body
    assert "- reason: template missing" in body
    assert "- failed step: `release ticket`" in body
    assert 'step="release ticket"' in caplog.text


def test_process_applies_the_winning_command_and_logs_it(monkeypatch, caplog):
    comments = [_comment("/milestone v0.5.41", cid=31)]
    applied = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: pytest.fail("no receipt expected"),
    )
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda repo, version, **kwargs: applied.append((repo, version, kwargs)),
    )
    with caplog.at_level(logging.INFO):
        milestone_command.process_milestone_commands(
            "owner/repo", 436, candidate_titles=["v0.5.41"],
            config_path=Path("/cfg"), policy=RepoPolicy(active_milestone="v0.5.40"),
            policy_path=".github/orbi.toml", base_branch="main",
            dispatch_label=READY_LABEL, version_file="package.json",
        )
    assert applied == [(
        "owner/repo", "v0.5.41",
        {
            "config_path": Path("/cfg"),
            "policy": RepoPolicy(active_milestone="v0.5.40"),
            "policy_path": ".github/orbi.toml",
            "base_branch": "main",
            "dispatch_label": READY_LABEL,
            "version_file": "package.json",
        },
    )]
    assert "milestone_command_applied" in caplog.text


def test_process_ignores_a_ticket_without_any_command(monkeypatch):
    monkeypatch.setattr(
        seam, "issue_comments",
        lambda number, repo: [_comment("thanks!")],
    )
    monkeypatch.setattr(seam, "authenticated_login", lambda: "orbi-build")
    monkeypatch.setattr(
        seam, "run_command", lambda command, **kwargs: pytest.fail("nothing to post"),
    )
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: pytest.fail("nothing to apply"),
    )
    milestone_command.process_milestone_commands(
        "owner/repo", 436, candidate_titles=["v0.5.41"], config_path=Path("/nope"),
        policy=None, policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file=None,
    )


# --- the whole deterministic chain ---------------------------------------


@pytest.mark.parametrize("policy_text,template_missing", [
    ('active_milestone = "v0.5.40"\n', False),
    (None, True),
])
def test_full_command_is_idempotent_and_lands_every_step(
    monkeypatch, tmp_path, caplog, policy_text, template_missing,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    fake = FakeMilestoneGh(
        policy_text=policy_text,
        template=None if template_missing else REAL_TEMPLATE,
    )
    monkeypatch.setattr(seam, "run_command", fake.run)
    policy = (
        RepoPolicy(active_milestone="v0.5.40") if policy_text else None
    )
    with caplog.at_level(logging.INFO):
        completed = milestone_command.apply_milestone_command(
            "owner/repo", "v0.5.41", config_path=config, policy=policy,
            policy_path=".github/orbi.toml", base_branch="main",
            dispatch_label=READY_LABEL, version_file="pyproject.toml",
        )
    assert completed == [
        milestone_command._STEP_MILESTONE, milestone_command._STEP_RELEASE_TICKET,
        milestone_command._STEP_ACTIVE_MILESTONE,
    ]
    assert [m["title"] for m in fake.milestones] == ["v0.5.40", "v0.5.41"]
    assert len(fake.release_issues) == 1
    body = fake.release_issues[0]["body"]
    assert parse_release_declaration(body)["version"] == "v0.5.41"
    if policy_text:
        assert 'active_milestone = "v0.5.41"' in fake.policy_text
        assert 'active_milestone = "v0.5.40"' in config.read_text()
    else:
        assert 'active_milestone = "v0.5.41"' in config.read_text()

    # A second identical command changes nothing: three idempotent steps.
    fake.commands.clear()
    milestone_command.apply_milestone_command(
        "owner/repo", "v0.5.41", config_path=config, policy=policy,
        policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file="pyproject.toml",
    )
    assert len(fake.milestones) == 2
    assert len(fake.release_issues) == 1
    assert [
        command for command in fake.commands
        if "issue" in command and "create" in command
    ] == []


def test_a_command_never_infers_the_next_version(monkeypatch, tmp_path):
    # v0.5.42 is the higher open milestone, but the command says v0.5.41 —
    # the literal in the comment is the only version that may be used.
    fake = FakeMilestoneGh(policy_text='active_milestone = "v0.5.40"\n')
    fake.milestones.append({"title": "v0.5.42", "state": "open"})
    monkeypatch.setattr(seam, "run_command", fake.run)
    milestone_command.apply_milestone_command(
        "owner/repo", "v0.5.41", config_path=tmp_path / "orbi.toml",
        policy=RepoPolicy(active_milestone="v0.5.40"),
        policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label=READY_LABEL, version_file="pyproject.toml",
    )
    assert [m["title"] for m in fake.milestones][-1] == "v0.5.41"
    assert 'active_milestone = "v0.5.41"' in fake.policy_text


def test_the_stateful_gh_fake_fails_fast_on_an_unexpected_command():
    # The fake answers only the argv the command really sends; anything else
    # must fail loudly instead of silently returning a plausible payload.
    fake = FakeMilestoneGh()
    with pytest.raises(AssertionError, match="unexpected endpoint"):
        fake.run(["gh", "api", "repos/owner/repo/contents/other.toml"])
    with pytest.raises(RuntimeError, match="404"):
        fake.run(["gh", "api", "repos/owner/repo/contents/.github/orbi.toml"])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake.run(["gh", "pr", "view"])


# --- idle-path wiring -----------------------------------------------------


def _milestone_responses(milestones):
    return json.dumps([milestones])


def test_advance_pending_processes_commands_on_the_confirmation_ticket(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    processed = []
    notes = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: _milestone_responses([
            {"title": "v0.5.40", "state": "closed"},
            {"title": "v0.5.41", "state": "open", "open_issues": 0},
        ]),
    )
    monkeypatch.setattr(
        seam, "list_issues", lambda repo, **kwargs: [{"number": 436}],
    )
    monkeypatch.setattr(
        seam, "_pending_milestone_issue", lambda *args, **kwargs: 436,
    )
    monkeypatch.setattr(
        seam, "process_milestone_commands",
        lambda repo, number, **kwargs: processed.append((repo, number, kwargs)),
    )
    monkeypatch.setattr(
        seam, "rewrite_active_milestone_line",
        lambda *args, **kwargs: notes.append(args),
    )
    milestone.advance_active_milestone_on_idle(
        "owner/repo", "v0.5.40", config,
        auto_next_milestone=False,
        parse_version_title=runner._parse_version_title,
        policy=RepoPolicy(active_milestone="v0.5.40", dispatch_label="ai-ready"),
        policy_path=".github/orbi.toml", base_branch="main",
        dispatch_label="ai-ready", version_file="package.json",
    )
    assert len(processed) == 1
    repo, number, kwargs = processed[0]
    assert repo == "owner/repo" and number == 436
    assert kwargs["candidate_titles"] == ["v0.5.41"]
    assert kwargs["policy_path"] == ".github/orbi.toml"
    assert kwargs["base_branch"] == "main"
    assert kwargs["version_file"] == "package.json"
    # The path never auto-advances: waiting for the human is intentional.
    assert notes == []
    assert config.read_text() == 'active_milestone = "v0.5.40"\n'


def test_advance_pending_skips_commands_when_the_ticket_is_unknown(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: _milestone_responses([
            {"title": "v0.5.40", "state": "closed"},
            {"title": "v0.5.41", "state": "open", "open_issues": 0},
        ]),
    )
    monkeypatch.setattr(
        seam, "_pending_milestone_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        seam, "process_milestone_commands",
        lambda *args, **kwargs: pytest.fail("nothing to process"),
    )
    milestone.advance_active_milestone_on_idle(
        "owner/repo", "v0.5.40", config,
        auto_next_milestone=False,
        parse_version_title=runner._parse_version_title,
    )


def test_advance_pending_command_failure_never_fails_the_idle_tick(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.5.40"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: _milestone_responses([
            {"title": "v0.5.40", "state": "closed"},
            {"title": "v0.5.41", "state": "open", "open_issues": 0},
        ]),
    )
    monkeypatch.setattr(
        seam, "_pending_milestone_issue", lambda *args, **kwargs: 436,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(
        seam, "process_milestone_commands", boom)
    with caplog.at_level(logging.ERROR):
        assert milestone.advance_active_milestone_on_idle(
            "owner/repo", "v0.5.40", config,
            auto_next_milestone=False,
            parse_version_title=runner._parse_version_title,
        ) == ("closed", None)
    assert "milestone_command_processing_failed" in caplog.text


def test_issue_number_only_accepts_a_github_issue_number():
    assert milestone._issue_number({"number": 436}) == 436
    assert milestone._issue_number(None) is None
    assert milestone._issue_number({"number": "436"}) is None
    assert milestone._issue_number({"number": True}) is None


def test_rewrite_active_milestone_text_serializes_and_stops_at_one_line():
    text = 'a = 1\nactive_milestone = "v0.5.40"\nb = 2\n'
    assert milestone_command.rewrite_active_milestone_text(text, "v0.5.41") == (
        'a = 1\nactive_milestone = "v0.5.41"\nb = 2\n'
    )
    with pytest.raises(RuntimeError, match="active_milestone line not found"):
        milestone_command.rewrite_active_milestone_text("a = 1\n", "v0.5.41")


def test_repo_policy_carries_the_active_milestone_the_idle_path_reads():
    # The idle path and the claim scans read ONE policy object, so a
    # repository-file `active_milestone` reaches the command path.
    assert RepoPolicy(active_milestone="v0.5.40").active_milestone == "v0.5.40"
