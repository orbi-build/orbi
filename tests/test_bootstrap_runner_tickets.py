"""Ticket-kind scene: content-only, ops, release dispatch (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: the ticket-kind routing in
`process_issue`, the explicit `ai-content-only`/`ai-ops` labels, the
content-only agent session with its provider materialization, the PR-ready
label on a scene-comment failure, and the ops ticket both with and without
a commit.
"""

from orbi import config as config_domain
from orbi import language
import json

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.pi_session as pi_session

import orbi.release_run as release_run
from tests.fakes.bootstrap_preflight import GROQ_PROVIDERS, _user_agent_dir
from seam import seam
from orbi.delivery_scene import RunContext

from conftest import rendered_prompt


# --- Issue #98: ticket kinds — content-only, ops, release dispatch --------


def test_process_issue_routes_release_to_process_release(monkeypatch):
    issue = {"number": 99, "title": "Release v0.3.0", "body": "",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    calls = []
    # Issue #708: the release dispatch now direct-reads the live label
    # state before dispatching (`gh issue view`) — the fresh ticket here
    # answers with no labels, and no slot config means no yield path.
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"labels": []}),
    )
    monkeypatch.setattr(release_run, "process_release",
                        lambda i, c, r: calls.append("release") or "rel-url")
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("run_pi must not run for a release task")))
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc")
    result = runner.process_issue(issue, config_domain.RunnerConfig(base_branch="main"), "o/r")
    assert result == runner.IssueResult("release", "rel-url")
    assert calls == ["release"]


def test_is_content_only_requires_the_explicit_label():
    """Issue #537: the content（纯内容）path dispatches on
    `ai-content-only`; the old `ai-ops-only` name now means ops."""
    assert runner.is_content_only(
        {"labels": [{"name": "ai-content-only"}]},
    ) is True
    assert runner.is_content_only(
        {"labels": [{"name": "ai-ops-only"}]},
    ) is False
    assert runner.is_content_only({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_content_only({"labels": "ai-content-only"}) is False


def test_is_ops_requires_the_explicit_label():
    """Issue #537: `ai-ops-only` is the full-execution ops marker — it
    must NOT dispatch the content agent anymore."""
    assert runner.is_ops({"labels": [{"name": "ai-ops-only"}]}) is True
    assert runner.is_ops({"labels": [{"name": "ai-content-only"}]}) is False
    assert runner.is_ops({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_ops({"labels": "ai-ops-only"}) is False


def test_run_ticket_agent_uses_a_temporary_session_without_git(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        # The prompt FILE lives in the session's TemporaryDirectory
        # (Issue #1634), so it is read while the session is running.
        lambda command, **kwargs: calls.append(
            (command, kwargs, rendered_prompt(command))) or "copy",
    )
    result = pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider=None, pi_model=None, pi_thinking=None),
        "o/r",
    )
    assert result == "copy"
    command, kwargs, prompt = calls[0]
    assert command[0] == "pi"
    # `pi --no-tools` is the enforcement boundary: a prompt instruction
    # alone cannot ensure the Agent never invokes git/gh or writes files.
    assert "--no-tools" in command
    assert "git/gh tools" in prompt
    assert kwargs["cwd"] != tmp_path
    assert kwargs["cwd"].name.startswith("orbi-ticket-")
    assert kwargs["ctx"].branch == "-"
    assert kwargs["role"] == runner.ROLE_TICKET
    assert str(tmp_path) not in command[command.index("--session-dir") + 1]
    assert Path(command[command.index("--session-dir") + 1]).parent == kwargs["cwd"]


def test_run_ticket_agent_materializes_provider_dir_and_env(
    monkeypatch, tmp_path,
):
    """Issue #1379: the ONE no-tools helper keeps reaching the configured
    provider — the materialization the clarify gate used to own still
    puts the per-run agent dir on `PI_CODING_AGENT_DIR`.

    The judgment session runs in a repo that configures a provider file
    (the ticket-only content session does too), and the selected model
    rides on `--provider/--model`: without the materialized catalog Pi
    could never resolve it, and the gate would silently fail open.
    """
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    seen: dict = {}

    def fake_stream(command, **kwargs):
        agent_dir = Path(kwargs["pi_env"]["PI_CODING_AGENT_DIR"])
        seen["kwargs"] = kwargs
        seen["models_written"] = (agent_dir / "models.json").is_file()
        return "copy"

    # The subprocess seam fans out to every binding (Issue #785), and
    # the patch ratchet forbids a new per-module `pi_session` patch
    # (Issue #789): the corpus may only shrink.
    monkeypatch.setattr(seam, "stream_pi", fake_stream)
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, run_id="a1b2c3d4", skills=(),
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    assert pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config, "o/r",
    ) == "copy"
    kwargs = seen["kwargs"]
    agent_dir = kwargs["cwd"] / ".orbi" / "pi-agent"
    # The per-run catalog reaches Pi through the env only...
    assert kwargs["pi_env"] == {"PI_CODING_AGENT_DIR": str(agent_dir)}
    assert seen["models_written"] is True
    # ...never through the journaled command.
    log = " ".join(kwargs["log_command"])
    assert str(agent_dir) not in log
    assert "https://api.groq.com/openai/v1" not in log
    # Transient OS state: the materialized dir goes with the session.
    assert not agent_dir.exists()


def test_run_ticket_agent_logs_provider_config_loaded(monkeypatch, tmp_path, caplog):
    """Issue #176: the ticket-only session logs the provider config
    line too (role=ticket) — every Pi session has the same startup
    sequence."""
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "copy")
    with caplog.at_level("INFO"):
        pi_session.run_ticket_agent(
            {"number": 99, "title": "Launch thread", "body": "Write copy"},
            config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b", pi_thinking=None),
            "o/r",
        )
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=ticket" in lines[0]
    assert "issue=o/r#99" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_progress_state_passes_startup_sub_phase_to_comment(tmp_path):
    """Issue #176: the progress comment's `phase` carries the watcher's
    startup sub-phase (session_pending / request_pending) instead of
    the generic `starting` while the first response is outstanding."""
    for sub_phase in ("session_pending", "request_pending"):
        state = runner.progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": sub_phase, "last_activity": None,
                      "action": None, "session_id": None}, language_code="en")
        assert state["phase"] == sub_phase
    # The tool-based phase passes through unchanged once the first
    # response arrived.
    state = runner.progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": "test", "last_activity": None,
                  "action": "bash pytest tests/", "session_id": "s1"}, language_code="en")
    assert state["phase"] == "test"


# Issue #1826: the operator's one optional project status page.
TASK_STATUS_URL = "https://tasks.example.org/project-status?repo=example%2Fwidget"


def test_process_ticket_only_posts_agent_output_without_git_delivery(monkeypatch):
    """A labeled content task is delivered in its Issue, never through Git."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}]}
    edits = []
    comments = []
    commands = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    monkeypatch.setattr(seam, "run_command",
                        lambda command, **kwargs: commands.append(command) or "")

    result = runner.process_issue(
        issue,
        config_domain.RunnerConfig(
            repo_dir=Path("/repo"), status_url=TASK_STATUS_URL,
        ),
        "o/r",
    )

    assert result == runner.IssueResult("ticket-only", None)
    assert edits == [
        (99, {"repo": "o/r", "add": "ai-in-progress"}),
        (99, {"repo": "o/r", "remove": "ai-in-progress"}),
    ]
    assert comments and "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50" in comments[0][1]["body"]
    # Issue #1826: the delivery record carries the readable summary with
    # the operator's fixed status link, before the agent's output.
    delivered = comments[0][1]["body"]
    assert "**#99 Launch thread**" in delivered
    assert (
        f"**Project status:** [Open project status]({TASK_STATUS_URL})"
        in delivered
    )
    assert delivered.index("**Project status:**") < delivered.index(
        "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03"
    )
    assert "<!-- orbi:run=a1b2c3d4 -->" in comments[0][1]["body"]
    assert "run_id=a1b2c3d4" in comments[0][1]["body"]
    assert commands == [["gh", "issue", "close", "99", "--repo", "o/r"]]


def test_process_ticket_only_rejects_empty_agent_content(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(pi_session, "run_ticket_agent", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="returned no content"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")
    assert edits[-2][1]["add"] == "ai-blocked"
    # ai-blocked ALONE: the queue label is cleared too (Issue #1775).
    assert edits[-1] == (99, {"repo": "o/r", "remove": "ai-ready"})


@pytest.mark.parametrize("language_code", ["en", "zh"])
@pytest.mark.parametrize(
    ("dispatch_label", "queue_label"),
    [(None, "ai-ready"), ("ship", "ship")],
    ids=["default_queue_label", "custom_dispatch_label"],
)
def test_process_ticket_only_failure_marks_blocked_without_git_delivery(
        monkeypatch, language_code, dispatch_label, queue_label):
    """A failed ticket-only run blocks with the community help line from the
    ONE blocked exit, in the run's bound language (Issue #1730).

    Issue #1775: the blocked terminal is `ai-blocked` ALONE — it clears
    the queue label the claim scanned, the repository's own
    `dispatch_label` when it declares one."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    comments = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(
            issue, config_domain.RunnerConfig(
                repo_dir=Path("/repo"), message_language=language_code,
                dispatch_label=dispatch_label, status_url=TASK_STATUS_URL,
            ), "o/r",
        )
    assert edits[-2] == (99, {"repo": "o/r", "add": "ai-blocked",
                               "remove": "ai-in-progress"})
    # The terminal state is ai-blocked ALONE: the queue label the claim
    # kept is cleared too (Issue #1775).
    assert edits[-1] == (99, {"repo": "o/r", "remove": queue_label})
    body = comments[-1][1]["body"]
    assert language.text(
        language_code, "runner.ticket_only_failed",
        detail="Pi failed",
        run_info="run_id=a1b2c3d4 priority=normal task_type=ticket-only",
        run_id="a1b2c3d4",
    ) in body
    assert "run_id=a1b2c3d4" in body
    # Issue #1826: the blocked task message carries the readable summary
    # (identity + the operator's fixed link) above the one help line.
    assert "**#99 Launch thread**" in body
    link = language.text(
        language_code, "summary.project_status", url=TASK_STATUS_URL,
    )
    assert link in body
    help_line = language.text(language_code, "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1


def test_process_ticket_only_keeps_original_error_when_failure_reporting_fails(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("comment failed")))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")


def test_process_issue_keeps_pr_ready_label_when_scene_comment_5xx_exhausts(
    monkeypatch, tmp_path, caplog,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "stable_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
                        lambda *args, **kwargs: edits.append((args, kwargs)))
    monkeypatch.setattr(seam, "set_active_run", lambda *args, **k: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    def comment(number, *, repo, body):
        # The scene comment (`Orbi opened PR:`) is the only one that
        # reaches this fake (the progress publisher is mocked), and it
        # fails with the GitHub 5xx the exhaustion path must survive.
        raise subprocess.CalledProcessError(
            1, ["gh", "issue", "comment"],
            stderr="GraphQL: Something went wrong while executing your query",
        )
    monkeypatch.setattr(seam, "comment_issue", comment)
    monkeypatch.setattr(seam, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "run_pi", lambda *a, **k: "done")
    monkeypatch.setattr(seam, "deliver_pr", lambda *a, **k: "https://github.com/o/r/pull/1")
    def run(command, **kwargs):
        if command[0:3] == ["gh", "pr", "list"]:
            return "[]"
        return "abc123"
    monkeypatch.setattr(seam, "run_command", run)
    monkeypatch.setattr(seam, "delivery_step", Mock())
    monkeypatch.setattr(seam, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(seam, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(seam, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(seam, "_finish_progress", Mock())
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    with caplog.at_level("WARNING"):
        result = runner.process_issue(
            issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
        )
    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/1")
    assert any(kwargs.get("add") == runner.PR_OPENED_LABEL for _, kwargs in edits)
    assert "progress_comment_failed" in caplog.text


def test_process_issue_keeps_normal_flow_without_release_label(
    monkeypatch, tmp_path,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    # The worktree exists: `create_worktree` always returns a real
    # directory (the run state file is written into it, Issue #219).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(pi_session, "run_pi",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(runner, "deliver_pr",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    monkeypatch.setattr(runner, "delivery_step", Mock())
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "LOGGER", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(runner, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(runner, "_finish_progress", Mock())
    runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
    )


def _ops_issue_mocks(monkeypatch, tmp_path, *, head_sha: str, dirty: str):
    """Shared mock set for the `process_issue` ops-dispatch tests.

    `head_sha` answers `git rev-parse HEAD` (the frozen base means the
    session delivered no commit); `dirty` answers `git status
    --porcelain`. Every `gh`/`git`/pi collaborator is captured.
    """
    issue = {
        "number": 99, "title": "Merge the beta PR and verify deploy",
        "body": "Merge PR #7 into main, then verify the deployment.",
        "labels": [{"name": "ai-ready"}, {"name": "ai-ops-only"}],
    }
    commands: list[list[str]] = []
    configs: list[dict] = []
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda issue_arg, ctx, config, **k: configs.append(config),
    )
    monkeypatch.setattr(runner.runner_health, "record_run_attempt", Mock())
    monkeypatch.setattr(runner.runner_health, "record_pickup", Mock())

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head_sha
        if command[:2] == ["git", "status"]:
            return dirty
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    return issue, commands, worktree, configs


def test_process_issue_ops_without_commit_closes_with_evidence(
    monkeypatch, tmp_path,
):
    """Issue #537: a pure-ops session (real shell/gh work, evidence
    comments, no commit) is COMPLETE — the Runner closes the Issue,
    removes the claim label, and never enters the PR ceremony."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    ops_prompt = prompts / "prompt_ops.md"
    ops_prompt.write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pure ops delivery must not open a PR")
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)
    # The full-execution session ran with the OPS playbook, not the dev
    # one — and with the same worktree machinery (no --no-tools content
    # agent).
    assert len(configs) == 1
    assert configs[0].prompt == ops_prompt
    # Terminal transition mirrors the content path: close + claim label
    # removed. No PR, no push.
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    # The claim added the label at pickup; the delivery removes it.
    runner.edit_issue.assert_any_call(
        99, repo="o/r", remove=runner.IN_PROGRESS_LABEL,
    )


def test_process_issue_ops_health_record_failure_is_a_bypass(
    monkeypatch, tmp_path,
):
    """Issue #537/#266: the health state write is pure bypass — a state
    failure never turns a delivered pure-ops run into a failed one."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner.runner_health, "record_run_attempt",
        Mock(side_effect=OSError("state file unwritable")),
    )
    monkeypatch.setattr(runner, "deliver_pr", Mock())
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)


def test_process_issue_ops_with_commit_takes_the_pr_ceremony(
    monkeypatch, tmp_path,
):
    """Issue #537: an ops session that commits code (e.g. a
    wrangler.toml change) is delivered through the SAME deterministic
    closeout as a dev ticket — push + PR, `ai-pr-opened`."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="def456", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    deliver_calls: list[tuple] = []
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: deliver_calls.append(a) or (
            "https://github.com/o/r/pull/7"
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/7")
    assert len(deliver_calls) == 1
    assert any(c[:2] == ["git", "rev-parse"] for c in commands)


def test_process_issue_ops_with_uncommitted_leftovers_fails_fast(
    monkeypatch, tmp_path,
):
    """Issue #537: no commit but uncommitted leftovers is neither a pure
    ops delivery nor a code delivery — the deterministic closeout's
    fail-fast (never commit uncommitted changes) applies."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty=" M a.txt\n",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")

    def failing_deliver_pr(*a, **k):
        raise runner.RecoverablePiFailure(
            "the agent left uncommitted changes in the worktree"
        )

    cleanup = Mock()
    original_cleanup = runner.cleanup_task_worktree
    runner.cleanup_task_worktree = cleanup
    monkeypatch.setattr(runner, "deliver_pr", failing_deliver_pr)

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    # Recoverable delivery failures preserve the scene for the next
    # same-run implement session instead of entering ai-blocked.
    assert result == runner.IssueResult("failed", None)
    cleanup.assert_not_called()
    runner.cleanup_task_worktree = original_cleanup
    runner.edit_issue.assert_called_once_with(
        99, repo="o/r", add=runner.IN_PROGRESS_LABEL,
    )


def test_bot_git_identity_overrides_git_config(tmp_path):
    """The startup identity owns a commit even when `-c user.*` is given.

    Git's `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables take precedence over
    `user.*` config, including `-c user.*`, so a Pi session can no longer
    credit its commits to a stranger's account (Issue #1416).
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    runner.set_bot_git_identity(env)
    work = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=x",
         "-c", "user.email=x@example.com", "commit", "--allow-empty",
         "-m", "t"],
        check=True, capture_output=True, env=env,
    )
    identity = subprocess.run(
        ["git", "-C", str(work), "log", "-1",
         "--format=%an <%ae>|%cn <%ce>"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert identity == (
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>|"
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>"
    )


def test_runner_main_pins_bot_git_identity(monkeypatch, tmp_path):
    """A run pins Orbi's identity before any git call or Pi session."""
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    missing = tmp_path / "missing.toml"
    assert runner.main(["--config", str(missing)]) == 1
    assert os.environ["GIT_AUTHOR_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert os.environ["GIT_COMMITTER_NAME"] == "orbi-build[bot]"
