"""Runner tick scene: main() routing, idle diagnostics and slots (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: one tick of `runner.main()` from
configuration checks through the claim scan, the idle outcome
(`no_ready_issue`, outside-milestone, query failure), the unscoped retry
when the active milestone closed, the routing of a process result into the
PR delivery wait, slot acquisition/occupancy, the blocked-error finish
comment, and the `run_failed` single-emission pin.

`_write_prompts` (the shared prompt-file scaffold) lives here; other suites
import it from this module.
"""

import ast

import logging
import os
from pathlib import Path
import pytest

import orbi.claim as claim
import orbi.run_state as run_state
from orbi import scene as scene_mod

import orbi.runner as runner
import orbi.resume_verification as resume_verification
import orbi.milestone as milestone
from orbi import pi_stream_log, progress
from tests.fakes.github import FakeGh
from seam import seam
from tests.fakes.bootstrap_delivery import PR_URL


def _recovered_scene(pr_url: str) -> run_state.ResumeScene:
    """The scene the resumable scan hands the dispatch (Issue #2053: one
    parsed `Scene` value, plus the comment stamp beside it)."""
    return run_state.ResumeScene(parsed=scene_mod.Scene(
        run_id="a1b2c3d4", base_branch="main",
        base_sha="abc123def456", pr_url=pr_url,
    ))


def _write_prompts(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")


def test_idle_diagnostic_logs_ready_outside_active_milestone(
    monkeypatch, caplog,
):
    issues = [
        {"number": 834, "milestone": None},
        {"number": 828, "milestone": {"title": "v0.5.37"}},
        {"number": 827, "milestone": {"title": "v0.5.38"}},
    ]
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: issues)
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is True
    assert "ready_outside_milestone" in caplog.text
    assert "repo=owner/repo" in caplog.text
    assert "active_milestone=v0.5.38" in caplog.text
    assert "count=2" in caplog.text
    assert "no_ready_issue" not in caplog.text


def test_idle_diagnostic_keeps_no_ready_issue_when_queue_is_empty(
    monkeypatch, caplog,
):
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: [])
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone" not in caplog.text


def test_idle_diagnostic_query_failure_falls_back_to_no_ready_issue(
    monkeypatch, caplog,
):
    def fail_query(*args, **kwargs):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "list_issues", fail_query)
    with caplog.at_level(logging.ERROR):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "GitHub unavailable" in caplog.text


def test_main_uses_outside_milestone_outcome_instead_of_no_ready(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(runner.__dict__, "log_ready_outside_milestone", lambda *args, **kwargs: True)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "no_ready_issue" not in caplog.text


def test_main_query_failure_keeps_idle_exit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("GitHub unavailable")
    ))
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "no_ready_issue" in caplog.text


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    assert runner.main(["--config", str(config)]) == 0


def test_main_passes_configured_active_milestone_to_the_claim_scan(
    monkeypatch, tmp_path,
):
    """Issue #139: the configured `active_milestone` reaches the claim
    scan (the fresh-claim scope), and an unconfigured one passes None
    (the compat behavior — no milestone filter)."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] == "v0.2.0"


def test_main_advances_milestone_only_after_no_ready_issue(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.3.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls == [
        (("owner/repo", "v0.3.0", config.resolve(), tmp_path.resolve()),
         {
             "auto_next_milestone": True,
             "release_confirmation": False,
             "parse_version_title": runner._parse_version_title,
             "policy": None,
             "policy_path": ".github/orbi.toml",
             "base_branch": "main",
             "dispatch_label": runner.READY_LABEL,
             "version_file": None,
             "language_code": "en",
         }),
    ]


def test_main_passes_disabled_auto_next_milestone_to_idle_advance(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        'active_milestone = "v0.3.0"\n'
        'auto_next_milestone = false\n',
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen == {
        "auto_next_milestone": False,
        "release_confirmation": False,
        "parse_version_title": runner._parse_version_title,
        "policy": None,
        "policy_path": ".github/orbi.toml",
        "base_branch": "main",
        "dispatch_label": runner.READY_LABEL,
        "version_file": None,
        "language_code": "en",
    }


def test_main_idle_uses_the_single_milestone_reconcile_entry_point(
    monkeypatch, tmp_path,
):
    """Issue #856: the idle path makes ONE Milestone bookkeeping call, so a
    new step never becomes a second, separately-failing call."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket",
        lambda *args, **kwargs: pytest.fail("the runner must not arm directly"),
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ("owner/repo", "v0.4.0")
    assert calls[0][1]["release_confirmation"] is False
    assert not hasattr(runner, "arm_release_ticket")
    # The acceptance is structural: the idle path reaches Milestone
    # bookkeeping through reconcile alone, never by calling the arm or
    # the advance directly (Issue #856).
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "arm_release_ticket" not in source
    assert "advance_active_milestone_on_idle" not in source


def test_main_idle_passes_release_confirmation_from_the_repo_policy(
    monkeypatch, tmp_path,
):
    """Issue #856: the opted-in repository policy reaches the reconcile call.

    Issue #1342: the same per-key precedence applies to
    `auto_next_milestone`, so the repository value reaches the idle path.
    """
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config(
        'dispatch_label = "dev-queue"\nrelease_confirmation = true\n'
        "auto_next_milestone = false\n"
    )
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs) or ("open", None),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["release_confirmation"] is True
    assert seen["dispatch_label"] == "dev-queue"
    # Issue #1342: `auto_next_milestone` is a repository policy key too --
    # the repo value (false) must reach the idle path even though the host
    # default is true, so a managed tenant can stop the auto-advance.
    assert seen["auto_next_milestone"] is False


def test_main_passes_none_active_milestone_when_unconfigured(
    monkeypatch, tmp_path,
):
    """Issue #139 compat: without the config field the claim scan
    receives None and keeps the pre-#139 scans."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] is None


def _stub_idle_milestone_path(monkeypatch, outcome):
    """Stub the idle Milestone path down to one resolved outcome."""
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "sync_active_milestone_variable",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: outcome,
    )
    monkeypatch.setitem(
        runner.__dict__, "log_ready_outside_milestone",
        lambda *args, **kwargs: False,
    )


def test_main_retries_the_claim_unscoped_when_active_milestone_closed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 user journey: the configured Milestone is closed with no
    newer open one, so the idle tick's fresh ready scan runs again WITHOUT
    the Milestone filter and the open `ai-ready` Issue is claimed.

    Counter-test: remove the unscoped retry and the second scan never runs,
    `claimed` stays empty and this assertion fails."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    issue = {"number": 2, "title": "after the first release", "body": "b"}
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    def fake_ready_scan(repo, active_milestone=None, **_kwargs):
        scopes.append(active_milestone)
        return issue

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(claim.__dict__, "pick_issue", fake_ready_scan)
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    # The scoped scan found nothing, the unscoped retry claimed the Issue.
    assert scopes == ["v0.1.0", None]
    assert claimed == [2]


def test_main_closed_unscoped_scan_that_finds_nothing_stays_idle(
    monkeypatch, tmp_path,
):
    """Issue #1391: the unscoped re-scan is not a claim by itself. When the
    closed-Milestone repo has no ready Issue either, the tick re-scans with
    no Milestone filter (still the repo's dispatch label) and ends idle
    instead of starting a delivery."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    rescans: list[tuple] = []
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: rescans.append(args) or None,
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert rescans == [("owner/repo", None)]


def test_main_closed_active_milestone_unscopes_the_repo_policy_milestone(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 through the real claim path: when the REPOSITORY policy
    declares `active_milestone` (`.github/orbi.toml`, the managed-sandbox
    default — a managed sandbox renders no host line), a closed one with no
    newer open Milestone must leave the unscoped re-scan unscoped too. The
    host fallback is only one of the two places the value can come from,
    and the policy value wins the scoped scan either way.

    Counter-test: drop the unscoped re-scan and the retry never runs, so
    the fake's milestone-scoped scan finds nothing and `claimed` stays
    empty."""
    _write_prompts(tmp_path)
    # The fake does not model `gh issue list --milestone` (the preflight's
    # closed-milestone report); the claim path under test stays the real one.
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.1.0", state="closed")
    fake.add_issue(2, title="after the first release", labels=("ai-ready",))
    fake.set_repo_config('active_milestone = "v0.1.0"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert (
        "active_milestone_closed_unscoped repo=owner/repo closed=v0.1.0"
        in caplog.text
    )
    assert claimed == [2]


@pytest.mark.parametrize("outcome", [("open", None), ("closed", "v0.2.0")])
def test_main_keeps_the_scope_without_the_unscoped_outcome(
    monkeypatch, tmp_path, outcome,
):
    """Acceptance: while the active Milestone is open (or the engine can
    advance to a newer open one), the tick never drops the Milestone filter
    and an Issue outside it is not claimed."""
    _stub_idle_milestone_path(monkeypatch, outcome)
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: pytest.fail("the scope must be kept"),
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert scopes == ["v0.1.0"]


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    steps = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "xqliu/orbi", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or runner.IssueResult("pr", "https://github.com/x/y/pull/12"))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue
    # Issue #788: the PR is open and the scene is written — the tick ends
    # WITHOUT running the review step in the same tick. The review is the
    # next tick's RESUME_REVIEW step, so the slot is released here and is
    # never held waiting for CI or mergeability.
    assert steps == []


def test_main_uses_process_result_kind_without_rechecking_task_type(
    monkeypatch, tmp_path,
):
    issue = {"number": 281, "title": "task", "body": "body",
             "labels": [{"name": "ai-ready"}]}
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args: runner.IssueResult("pr", "https://x/y/pull/281"),
    )
    monkeypatch.setattr(
        runner, "is_content_only",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        runner, "is_ops",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        claim, "is_release",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    steps = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append(args),
    )

    assert runner.main(["--config", str(config)]) == 0
    # Issue #788: a fresh claim ends its tick when the PR opens; the
    # review step runs on a later tick through the resume scan.
    assert steps == []


def test_main_ends_tick_when_process_issue_delivers_nothing(
    monkeypatch, tmp_path,
):
    """Issue #239: when `process_issue` returns `None` (a terminal
    delivery failure it already handled — the Issue is `ai-blocked` and
    the failure comment is posted), `main` ends the tick cleanly: it
    returns 0 and NEVER calls `delivery_step` (there is no PR to
    wait for). The service must not crash on the handled failure."""
    issue = {"number": 239, "title": "task", "body": "body"}
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: runner.IssueResult("failed", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_ticket_only_finishes_without_entering_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Ticket-only work has no PR, so the normal review/merge wait is invalid."""
    issue = {
        "number": 12, "title": "Launch copy", "body": "Write copy",
        "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}],
    }
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("ticket-only", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ticket-only work must not enter PR delivery wait")
        ),
    )

    assert runner.main(["--config", str(config)]) == 0


def test_main_release_success_ends_tick_without_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Issue #269: a release delivery returns a Release URL, not a PR URL.

    `process_release` already closed the delivery (tag pushed, GitHub
    Release published, Issue `ai-merged` and closed), so `main` must end
    the tick with exit code 0 and NEVER enter the PR delivery wait —
    `pr_number` on `.../releases/tag/v0.3.0` raises `ValueError` and
    crashed the Runner (run_id=37216af5)."""
    issue = {
        "number": 254, "title": "Release v0.3.0", "body": "Release v0.3.0",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
    }
    release_url = "https://github.com/orbi-build/orbi/releases/tag/v0.3.0"
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("release", release_url))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append(args) or (
            _ for _ in ()
        ).throw(AssertionError(
            "release delivery must not enter PR delivery wait"
        )),
    )

    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_routes_fix_needed_resume_to_delivery_wait(
    monkeypatch, tmp_path, bind_run_id,
):
    """A resumed `ai-fix-needed` delivery goes straight to the delivery
    wait (Issue #82: the review session fixes findings in the same
    session, so there is no cold-start fixer to run). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    bind_run_id(None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = _recovered_scene("https://github.com/owner/repo/pull/12")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        resume_verification, "verify_resumed_pr",
        lambda *args, **kwargs: scene.parsed.pr_url,
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene.parsed.pr_url, issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_routes_awaiting_review_resume_to_delivery_wait(
    monkeypatch, tmp_path, bind_run_id,
):
    """A resumed `ai-pr-opened` delivery (stranded by a dead runner or
    the Issue #70 progress 404) goes straight to the delivery wait:
    the independent review of the same PR runs (Issue #45 round-5
    contract: a clean PR is never sent to a fixer). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    bind_run_id(None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = _recovered_scene("https://github.com/owner/repo/pull/12")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        resume_verification, "verify_resumed_pr",
        lambda *args, **kwargs: scene.parsed.pr_url,
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer for a clean PR: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene.parsed.pr_url, issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_rejects_repeated_source_repo_before_execution(
    monkeypatch, tmp_path,
):
    _write_prompts(tmp_path)
    seen = []
    config = tmp_path / "orbi.toml"
    config.write_text(
        "source_repos = [\"xqliu/orbi\", \"xqliu/orbi-backlog\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda *args, **kwargs: seen.append(args) or None,
    )
    assert runner.main(["--config", str(config)]) == 1
    assert seen == []


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.main(["--config", str(config)])


# --- main() slot acquisition (Issue #39) --------------------------------------


def test_main_capacity_full_does_not_pick_issue_or_call_pi(
    monkeypatch, tmp_path, caplog,
):
    """A full slot stops the runner before any claim or Pi invocation."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 1\n',
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    # The single slot is already HELD (lock) by this live process: a
    # file on disk alone is not a held slot (flock is the token).
    slot_dir = tmp_path / ".orbi" / "slots"
    held = pilot_slots.acquire_slot(slot_dir, 1, os.getpid())
    assert held is not None

    def fail_if_called(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        raise AssertionError("pick_next_delivery must not run when capacity is full")

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    monkeypatch.setattr(runner, "process_issue", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="must not run when capacity is full"):
        fail_if_called(["owner/repo"], Path("slots"), 1)
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "capacity_full" in caplog.text
    assert "max_concurrency=1" in caplog.text
    assert "slot_dir=" in caplog.text
    # The pre-existing holder keeps its slot (no claim, no label change,
    # no Pi).
    assert (
        pilot_slots.slot_occupancy(slot_dir, 1) == [(1, os.getpid())]
    )
    held.release()


def test_main_holds_slot_while_processing_issue(monkeypatch, tmp_path):
    """The slot is acquired before the pick and held for the whole task.

    Issue #1319: the claim lock is held for the SAME pick and is free
    again while the Pi session runs — the hold spans the scan -> identity
    write, never a Pi session.
    """
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}
    slot_dir = tmp_path / ".orbi" / "slots"

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["occupancy"] = pilot_slots.slot_occupancy(
            tmp_path / ".orbi" / "slots", 1,
        )
        seen["claim_window"] = pilot_slots.is_claim_lock_held(slot_dir)
        return ("owner/repo", issue, None)

    def fake_process(*args, **kwargs):
        # The first Pi session starts only after `mark_slot_delivery`.
        seen["pi_session"] = pilot_slots.is_claim_lock_held(slot_dir)
        return runner.IssueResult("pr", "https://x/y/pull/12")

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setattr(runner, "process_issue", fake_process)
    monkeypatch.setattr(runner, "delivery_step", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["occupancy"] == [(1, os.getpid())]
    assert seen["claim_window"] is True
    assert seen["pi_session"] is False


def test_main_reacquires_slot_after_previous_release(monkeypatch, tmp_path):
    """After the holder releases (process exit), the next run takes the slot."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )

    assert runner.main(["--config", str(config)]) == 0
    slot_dir = tmp_path / ".orbi" / "slots"
    # The slot file exists and was released when main() returned ...
    assert (slot_dir / "slot-1").exists()
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    # ... so the next run takes the slot again.
    assert runner.main(["--config", str(config)]) == 0
    assert (slot_dir / "slot-1").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_terminal_failure_parts_render_action_before_reason_and_diagnosis():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Approve the PR manually.",
        reason="Branch protection requires one human approval.",
        diagnosis="rounds=3; mergeable=CONFLICTING",
        detail="ignored", next_step="", pr_url=None, number=39,
        source_repo="owner/repo", language_code="en",
    )
    assert body.index("Approve the PR manually.") < body.index(
        "Branch protection requires one human approval."
    )
    assert body.index("Branch protection requires one human approval.") < body.index(
        "<details><summary>Raw error</summary>"
    )
    assert "rounds=3; mergeable=CONFLICTING" in body


def test_terminal_failure_parts_omit_empty_action_and_show_disposition():
    body = progress._finish_outcome_body(
        outcome="blocked", action="", reason="No human action is required.",
        diagnosis="engine retry state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What you need to do:" not in body
    assert "waiting on a human decision" in body
    assert "engine retry state" in body


def test_terminal_failure_parts_render_missing_reason_explicitly():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Inspect the PR.", reason="",
        diagnosis="raw state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What happened: No reason was provided." in body


def test_finish_progress_body_uses_fixed_sections_for_blocked_error(caplog):
    body = progress.finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None,
        pr_url="https://github.com/owner/repo/pull/46", review_round=0,
        priority="normal", detail="Command '['gh']' failed: raw stderr",
        next_step="", outcome="blocked", source_repo="owner/repo", language_code="en",
    )

    assert "What happened: Orbi could not complete the delivery" in body
    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next:" in body
    assert "https://github.com/owner/repo/pull/46" in body
    assert "<details><summary>Raw error</summary>" in body
    assert "Command '['gh']' failed: raw stderr" in body
    assert "next step: " not in body
    assert "progress_finish_missing_next_step" in caplog.text
    assert "call_point=_finish_progress_body" in caplog.text


def test_finish_progress_body_keeps_next_step_in_user_section():
    body = progress.finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="relabel the Issue ai-ready", outcome="blocked",
        source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: relabel the Issue ai-ready" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body


def test_finish_progress_body_defaults_orbi_action_when_fix_step_missing(caplog):
    body = progress.finish_progress_body(
        number=39, title="Fix task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="", outcome="fix needed", source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body
    assert "progress_finish_missing_next_step" in caplog.text


def test_main_releases_slot_after_opening_the_pr(monkeypatch, tmp_path):
    """Issue #788: a fresh claim ends its tick when the PR is open — the
    slot is released instead of being held through review -> merge. The
    review is the next tick's RESUME_REVIEW step, which takes the slot
    again for exactly the Pi session's duration. A concurrent runner can
    therefore take the slot the moment this tick ends."""
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *a, **k: runner.IssueResult("pr", PR_URL))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a fresh claim must not run the review step"),
        ),
    )

    assert runner.main(["--config", str(config)]) == 0
    # The tick ended after opening the PR: the slot is free again.
    slot_dir = tmp_path / ".orbi" / "slots"
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    assert pilot_slots.acquire_slot(slot_dir, 1, os.getpid()) is not None


def _run_failed_log_sites(source: str) -> list[int]:
    """Scan one Python source; return the line numbers of the
    `run_failed` emission sites — the Issue #791 single-emission form
    `event("run_failed ...")` and the legacy `LOGGER.error("run_failed
    ...")` form alike."""
    sites: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and (first.value == "run_failed"
                     or first.value.startswith("run_failed "))):
            continue
        func = node.func
        is_event = isinstance(func, ast.Name) and func.id == "event"
        is_error = isinstance(func, ast.Attribute) and func.attr == "error"
        if is_event or is_error:
            sites.append(node.lineno)
    return sites


def test_run_failed_pin_detects_every_error_site():
    """The detector really detects: two `run_failed` ERROR sites produce
    two line numbers and a non-ERROR sibling is ignored."""
    source = (
        "event(\n"
        "    'run_failed',\n"
        "    format_run_scene(activity),\n"
        "    level=logging.ERROR, reason='a',\n"
        ")\n"
        "LOGGER.info('run_failed (historic)', s)\n"
        "LOGGER.error('run_failed %s reason=b', s)\n"
    )
    assert _run_failed_log_sites(source) == [1, 7]


def test_run_failed_scene_logged_in_one_place():
    """Issue #292: the `run_failed` skeleton (the six-argument
    `format_run_scene` + reason) lives in exactly ONE place —
    `pi_stream_log._log_run_failed` (extracted from `pi_process`, Issue
    #1860); each terminal branch computes its reason and exception and
    calls it once. A second literal `LOGGER.error("run_failed ...` site is
    the duplicated debt this pin rejects (a new log field would have to be
    edited per branch again)."""
    sites = _run_failed_log_sites(
        Path(pi_stream_log.__file__).read_text(encoding="utf-8")
    )
    assert len(sites) == 1, (
        "the run_failed scene must be logged in exactly one place (the "
        f"_fail_run helper, Issue #292); sites at lines: {sites}"
    )
