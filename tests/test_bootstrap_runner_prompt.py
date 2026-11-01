"""Prompt scene: the task context and system prompt Pi receives (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: the rendered prompt content
(`{{...}}` placeholders, `{{ISSUE_COMMENTS}}`, base branch/run id, resume
context, the file-form task context), the provider-config log lines, the
role-specific `--skill` lists and the Pi child environment.

Prompt helpers that other scenes need stay in their single owning module:
`LANGUAGE_SUFFIX` here, `_write_prompts` in
`tests/test_bootstrap_runner_main.py`.
"""

from orbi import config as config_domain
from orbi import language

# The session-boundary message-language instruction every implementer/
# review prompt carries (Issue #1660). Prompt-content assertions compare
# the rendered template PLUS this instruction, so a custom template
# cannot silently lose it.
LANGUAGE_SUFFIX = language.model_instruction("en")
import logging
import os
import sys
from pathlib import Path
import pytest
import orbi.runner as runner
import orbi.pi_session as pi_session
from orbi import pi_command
from seam import seam
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from conftest import rendered_prompt, task_context


def test_render_prompt_replaces_context_values():
    rendered = pi_session.render_prompt(
        "{{SOURCE_REPO}} #{{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{CONTEXT_FILES}}",
        {
            "SOURCE_REPO": "owner/repo",
            "ISSUE_NUMBER": "3",
            "ISSUE_TITLE": "Fix",
            "CONTEXT_FILES": "context.md",
        },
    )
    assert rendered == "owner/repo #3 Fix context.md"


def test_prompt_requires_verifying_external_behavior_against_docs():
    """Issue #73: the implementer prompt must make it a hard rule to
    verify external behavior (APIs, CLIs, config, HTTP paths) against
    official docs or `--help` before writing it — no assembling from
    memory — and test assertions must assert the real contract, not
    the implementation's guessed shape. It must also state that
    bypass failures (progress, notifications) only log and never
    decide the delivery outcome."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "--help" in template
    assert "docs" in template.lower()
    # The bypass rule is explicit.
    assert "bypass" in template.lower()


def test_review_prompt_flags_unverified_external_behavior():
    """Issue #73: the review prompt must check that external behavior
    (paths, parameters, status codes) was verified against docs or a
    real call, and that a bypass failure cannot break the main
    delivery — the #57 guessed-PATCH-route class of bug must be
    caught by review, not shipped."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt_review.md"
    ).read_text(encoding="utf-8")
    assert "verify" in template.lower()
    assert "bypass" in template.lower()


def test_prompt_does_not_require_review_fix_loop_before_pr():
    """Issue #78: the implementer only does plan -> implement -> test
    -> push PR. The independent review happens AFTER the PR is open
    (the Runner runs it); the prompt must not demand a complete
    review-fix loop before opening the PR — the old wording made the
    local Pi self-review for hours (#18/#34)."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "complete review-fix loop" not in template
    # The review is explicitly positioned AFTER the PR is open, and the
    # implementer never reviews/fixes/merges.
    assert "you do not review, fix, or merge" in template


def test_prompt_template_requires_fixes_keyword_for_the_source_issue():
    """Issue #53: the Pi prompt must carry `Fixes #<issue>` (the PR body
    contract the Runner fulfils when it opens the PR, Issue #186) so
    GitHub closes the source Issue natively on merge; the requirement
    renders with the real issue number."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "Fixes #{{ISSUE_NUMBER}}" in template
    rendered = pi_session.render_prompt(template, {
        "SOURCE_REPO": "xqliu/orbi",
        "SOURCE_REPOS": "xqliu/orbi",
        "ISSUE_NUMBER": "53",
        "ISSUE_TITLE": "t",
        "ISSUE_BODY": "b",
        # Issue #745: the shipped template injects the trusted-comment
        # timeline; the render must fill it to prove no placeholder is
        # left unresolved.
        "ISSUE_COMMENTS": "(no trusted comments)",
        "WORKSPACE_ROOT": "/tmp",
        "CONTEXT_FILES": "",
        "SKILLS": "",
        "BASE_BRANCH": "main",
        "BASE_SHA": "abc123",
        "RUN_ID": "a2241189",
        "BASE_SYNC_LOCK": "/checkout/.orbi/base-sync.lock",
    })
    assert "Fixes #53" in rendered
    # Issue #186: the implementer prompt no longer carries the base-sync
    # lock path — the base fetch is the Runner's operation.
    assert "/checkout/.orbi/base-sync.lock" not in rendered
    assert "{{" not in rendered


def test_prompt_templates_carry_the_issue_comments_placeholder():
    """Issue #745: the shipped implementer and review templates inject
    the trusted-comment timeline by default — decisions written in
    Issue comments reach the agent without a human copying them into
    the body."""
    prompts = Path(__file__).resolve().parent.parent / "prompts"
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt_review.md"
    ).read_text(encoding="utf-8")


def test_issue_context_uses_owner_repo_number_form():
    assert runner.issue_context("xqliu/orbi", 40) == (
        "xqliu/orbi#40"
    )


def test_run_pi_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the implementer prompt carries the absolute path of
    # the shared base-sync lock so Pi's base freshness fetch can run
    # under it (flock <lock> git fetch origin <base>).
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert rendered_prompt(command) == (
        "SYSTEM " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_logs_provider_config_loaded_with_selection(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: run_pi logs `provider_config_loaded` with the
    configured provider/model identifiers (the same non-sensitive
    values as the redacted command line) before Pi is spawned."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", pi_provider="local-qwen", pi_model="qwen3.8:27b")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "issue=owner/repo#4" in line
    assert "role=implement" in line
    assert "provider=local-qwen" in line
    assert "model=qwen3.8:27b" in line
    assert "elapsed=" in line


def test_run_pi_logs_provider_config_loaded_unconfigured(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: without a configured provider/model the line still
    fires (Pi keeps its own agent dir) with `-` placeholders."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "provider=-" in lines[0]
    assert "model=-" in lines[0]
    assert "role=implement" in lines[0]


def test_run_review_logs_provider_config_loaded_with_review_role(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: the review session logs the same line with
    role=review (one run_id, the roles are steps of the same run)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "ok")
    with caplog.at_level("INFO"):
        pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b").run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b"), 1)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=review" in lines[0]
    assert "issue=owner/repo#4" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_run_review_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the review prompt carries the SAME lock path (the
    # review session's base merge fetch must not race the shared ref).
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text(
        "REVIEW {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "ok")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


# --- Issue #745: {{ISSUE_COMMENTS}} — the trusted-comment timeline -----------


def test_run_pi_injects_trusted_issue_comments_into_the_prompt(
    monkeypatch, tmp_path,
):
    """Issue #745: a template carrying {{ISSUE_COMMENTS}} receives the
    Issue's trusted-comment timeline — fetched from GitHub, maintainer
    comments only, capped by the configured `issue_comments_limit`
    with a visible omission note."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_COMMENTS}}", encoding="utf-8")
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "oldest decision"},
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "middle decision"},
        {"author": {"login": "dave"}, "authorAssociation": "MAINTAINER",
         "createdAt": "2026-09-12T04:00:00Z", "body": "newest decision"},
    ]
    fetches = []

    def fake_issue_comments(number, *, repo):
        fetches.append((number, repo))
        return comments

    monkeypatch.setattr(seam, "issue_comments", fake_issue_comments)
    # Issue #1806: the writers are the repository's maintainers (alice,
    # carol, dave), mallory is not — the comment's association decides
    # nothing. The read is answered here so the test never depends on the
    # host's real gh login state (CI runs unauthenticated).
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: "none" if login == "mallory" else "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]"
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", issue_comments_limit=2)
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    assert fetches == [(4, "owner/repo")]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "(1 older trusted comment omitted; showing the 2 most recent)"
        in system_prompt
    )
    assert "newest decision" in system_prompt
    assert "middle decision" in system_prompt
    assert "oldest decision" not in system_prompt
    assert "public drive-by" not in system_prompt
    # The context keeps its exact pre-#745 shape — the comments reach
    # the agent through the template placeholder, nowhere else — and it
    # travels as Pi's file argument (Issue #1997).
    assert task_context(command) == (
        "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a template without {{ISSUE_COMMENTS}} keeps the exact
    pre-#745 behavior — no comments GitHub read, no new failure mode
    (backward compatibility for custom templates)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == "SYSTEM b" + LANGUAGE_SUFFIX


def test_run_review_refreshes_issue_body_each_round(monkeypatch, tmp_path):
    """Issue #1273: each review round receives the Issue body as it reads
    now, so deleting an acceptance criterion takes effect on the next round."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    bodies = iter(["old criterion", "new criterion {{BASE_SYNC_LOCK}}"])
    issue_reads = []
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: issue_reads.append((number, fields, repo))
        or {"body": next(bodies)},
    )
    prompts = []
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        # The prompt is read through the FILE the argv names (Issue
        # #1634) at call time: each round overwrites the same prompt
        # file in the shared session dir, so the read has to happen
        # while the round is running.
        lambda command, **kwargs: prompts.append(rendered_prompt(command)) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    pi_session.run_review(ctx, pr, config, 1)
    pi_session.run_review(ctx, pr, config, 2)
    assert issue_reads == [(4, "body", "owner/repo"), (4, "body", "owner/repo")]
    assert prompts == [
        "BODY=old criterion" + LANGUAGE_SUFFIX,
        "BODY=new criterion {{BASE_SYNC_LOCK}}" + LANGUAGE_SUFFIX,
    ]
    assert "old criterion" not in prompts[1]


def test_run_review_rejects_a_non_string_issue_body(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: {"body": 42},
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    with pytest.raises(ValueError, match="issue body must be a string"):
        pi_session.run_review(ctx, pr, config, 1)


def test_run_review_combines_current_pr_feedback_with_issue_comments(
    monkeypatch, tmp_path,
):
    """The current PR's normalized feedback shares one bounded timeline."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda number, *, repo: [
        {"author": {"login": "alice"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "old issue decision"},
    ])
    pr_fetches = []
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "conversation")) or [])
    monkeypatch.setattr(seam, "pr_reviews", lambda number, *, repo:
                        pr_fetches.append((number, repo, "reviews")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "submitted_at": "2026-09-12T02:00:00Z",
         "state": "CHANGES_REQUESTED", "body": "review body"},
    ])
    monkeypatch.setattr(seam, "pr_review_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "inline")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "created_at": "2026-09-12T03:00:00Z", "path": "src/x.py",
         "line": 7, "body": "inline fix"},
    ])
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]"
    )
    # Issue #1806: the human comments of these timelines are writers;
    # the permission read is answered here so no test reaches GitHub.
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(), issue_comments_limit=2,
    )
    pi_session.run_review(
        RunContext(run_id=config.run_id, issue=4, branch="branch",
                   worktree=tmp_path, source_repo="owner/repo"),
        {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1,
    )
    assert pr_fetches == [
        (9, "owner/repo", "conversation"),
        (9, "owner/repo", "reviews"),
        (9, "owner/repo", "inline"),
    ]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "1 older trusted comment omitted; showing the 2 most recent"
        in system_prompt
    )
    assert "old issue decision" not in system_prompt
    assert "review body" in system_prompt
    assert "[CHANGES_REQUESTED]" in system_prompt
    assert "inline fix" in system_prompt
    assert "src/x.py:7" in system_prompt


def test_run_review_pr_feedback_failure_logs_and_keeps_issue_comments(
    monkeypatch, tmp_path, caplog,
):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda *args, **kwargs: [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "issue decision"},
    ])
    monkeypatch.setattr(seam, "pr_comments", lambda *args, **kwargs: [])
    # Issue #1806: the trust read is answered here so the timeline never
    # asks GitHub (and never needs the host's identity).
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]",
    )

    def fail_reviews(*args, **kwargs):
        raise RuntimeError("reviews unavailable")

    monkeypatch.setattr(seam, "pr_reviews", fail_reviews)
    calls = []
    monkeypatch.setattr(seam, "stream_pi",
                        lambda command, **kwargs: calls.append(command) or "ok")
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    with caplog.at_level(logging.ERROR):
        pi_session.run_review(
            RunContext(run_id=config.run_id, issue=4, branch="branch",
                       worktree=tmp_path, source_repo="owner/repo"),
            {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config, 1,
        )
    system_prompt = rendered_prompt(calls[0])
    assert "issue decision" in system_prompt
    assert "pr_review_feedback_read_failed repo=owner/repo pr=9" in caplog.text


def test_run_review_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a review template without {{ISSUE_COMMENTS}} keeps
    the exact pre-#745 behavior (no comments GitHub read)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{BASE_SYNC_LOCK}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_injects_base_branch_sha_and_run_id_into_prompt(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{SOURCE_REPO}} {{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{ISSUE_BODY}} "
        "{{WORKSPACE_ROOT}} {{CONTEXT_FILES}} {{SKILLS}} {{BASE_BRANCH}} "
        "{{BASE_SHA}} {{RUN_ID}} {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=("context.md",), skills=("skill.md",), base_branch="main", base_sha="abc123def456", run_id="run1")
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    command, kwargs = calls[0]
    assert command[:5] == ["pi", "--no-extensions", "--skill", "skill.md", "--print"]
    system_prompt = rendered_prompt(command)
    assert "owner/repo" in system_prompt
    assert " 4 " in system_prompt
    assert "Fix title" in system_prompt
    assert "Fix body" in system_prompt
    assert "context.md" in system_prompt
    assert "skill.md" in system_prompt
    assert system_prompt.endswith(LANGUAGE_SUFFIX)
    assert (
        "main abc123def456 run1 "
        + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
    ) in system_prompt
    assert task_context(command) == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] is None
    assert kwargs["ctx"].run_id == "run1"
    assert kwargs["ctx"].issue == 4
    assert kwargs["ctx"].source_repo == "owner/repo"
    assert kwargs["ctx"].branch == "orbi/owner-repo-issue-4"
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_task_branch_to_stream_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(kwargs) or "done")
    issue = {"number": 5, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, timeout=7)
    assert calls[0]["ctx"].branch == "orbi/owner-repo-issue-5"
    assert calls[0]["timeout"] == 7


def test_run_pi_redacts_prompt_and_issue_from_command_log(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    pi_session.run_pi({"number": 5, "title": "secret", "body": "token"}, RunContext(run_id=config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1").run_id, issue={"number": 5, "title": "secret", "body": "token"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1"))
    command, kwargs = calls[0]
    system_prompt = rendered_prompt(command)
    assert "PRIVATE SYSTEM" in system_prompt
    assert "token" in system_prompt
    assert kwargs["log_command"] == [
        "pi", "--no-extensions", "--print", "--session-dir",
        str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_run_pi_keeps_the_fresh_context_without_a_resume_context(
    monkeypatch, tmp_path,
):
    """No resume context: the context argument is byte-identical to the
    pre-#219 shape (a fresh claim is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert task_context(command) == (
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_appends_the_resume_context_to_the_context_argument(
    monkeypatch, tmp_path,
):
    """Issue #219: the continued run's new session starts from the
    existing work — the resume context is appended to the context
    argument (the prompt template itself is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    resume = (
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.\nPrevious session progress: session=sess-1 "
        "events=7 phase=test last_action=bash pytest last_result=ok\n"
        "Uncommitted changed files (2):\n- src/a.py\n- src/b.py"
    )
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, resume_context=resume)
    command = calls[0]
    context = task_context(command)
    assert context.startswith(
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: " + str(tmp_path),
    )
    assert context.endswith(resume)


# A fake Pi that consumes the task context the way Pi itself does (Issue
# #1997): the command's `@<path>` argument is read from disk and its FULL
# content is what the session receives. It exits non-zero when the context
# does not arrive as a file argument, so the scene cannot pass on a
# command line that still carries the text.
FAKE_PI_READS_THE_CONTEXT_FILE = """#!/usr/bin/env python3
import os, sys
argument = sys.argv[-1]
if not argument.startswith("@"):
    sys.stderr.write("the task context is not a file argument")
    sys.exit(9)
context = open(argument[1:], encoding="utf-8").read()
with open(os.environ["ORBI_PI_CONTEXT_DUMP"], "w", encoding="utf-8") as handle:
    handle.write(context)
sys.stdout.write("done")
"""


def test_a_150k_issue_body_starts_pi_and_reaches_it_whole(tmp_path, monkeypatch):
    """Issue #1997 at the real entry point: the shipped regression scene —
    a short spec plus ~45,600 CJK characters of background (a
    150,000-byte Issue body) — starts Pi like any other ticket.

    Before the fix the whole context was ONE argv string and the kernel
    refused the spawn (`OSError: [Errno 7] Argument list too long: 'pi'`),
    which ended the run as an unclassifiable Pi start failure. Here the
    session is spawned for real from `run_pi`'s command, the fake pi reads
    the context the way Pi does, and the context it received holds the
    whole body — its LAST line included (nothing is truncated).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(FAKE_PI_READS_THE_CONTEXT_FILE, encoding="utf-8")
    fake_pi.chmod(0o755)
    dump = tmp_path / "pi-context.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ORBI_PI_CONTEXT_DUMP", str(dump))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    body = "背景" * 25_000 + "\nLAST LINE OF THE BODY"
    assert len(body.encode("utf-8")) > 150_000
    issue = {"number": 5, "title": "t", "body": body}
    output = pi_session.run_pi(
        issue,
        RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"),
        config,
    )
    assert output == "done"
    context = dump.read_text(encoding="utf-8")
    assert body in context
    assert "LAST LINE OF THE BODY" in context
    # The context file lives in the session dir, outside every commit.
    written = (tmp_path / ".pi-session" / "task-context.md").read_text(
        encoding="utf-8",
    )
    assert written == context


# --- role-specific --skill lists (Issue #83) ---------------------------------


def _skill_config(tmp_path, *names):
    """Build a config whose skills point at <name>/SKILL.md files."""
    skills = []
    for name in names:
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        skills.append(skill_dir / "SKILL.md")
    return config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=skills, base_branch="main", base_sha="abc123def456", run_id="a1b2c3d4")


def _command_skills(command):
    """Return the --skill values of an assembled pi command."""
    return [
        value for index, item in enumerate(command)
        if item == "--skill" and (value := command[index + 1])
    ]


def test_run_pi_keeps_tdd_dev_and_code_review_drops_review_fix_loop(
    monkeypatch, tmp_path,
):
    """Issue #83: the implementer/fixer keeps the delivery skills
    (tdd-dev, code-review) but not review-fix-loop — the Runner runs
    the independent review/fix loop itself after the PR is open."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    skills = _command_skills(calls[0])
    assert any("tdd-dev" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)


def test_run_review_keeps_only_code_review(monkeypatch, tmp_path):
    """Issue #83: the read-only review session must not load the
    delivery-oriented skills (tdd-dev would steer it into the
    implement/test/PR flow, review-fix-loop would open another
    fix/review round); code-review stays."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert not any("tdd-dev" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)


def test_run_pi_and_run_review_skill_lists_differ(monkeypatch, tmp_path):
    """Issue #83 acceptance: the two assembled command lines carry
    different --skill lists."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    implement_skills = _command_skills(calls[0])
    review_skills = _command_skills(calls[1])
    assert implement_skills != review_skills


def test_skill_name_of_skill_md_inside_skill_directory(tmp_path):
    path = tmp_path / "skills" / "tdd-dev" / "SKILL.md"
    path.parent.mkdir(parents=True)
    assert pi_command._skill_name(path) == "tdd-dev"


def test_skill_name_of_bare_markdown_entry(tmp_path):
    path = tmp_path / "my-skill.md"
    assert pi_command._skill_name(path) == "my-skill"


def test_run_review_keeps_non_delivery_skill_names(monkeypatch, tmp_path):
    """Only tdd-dev/review-fix-loop are excluded from the review; any
    other configured skill (including code-review) passes through.
    """
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(tmp_path, "code-review", "platform-qa")
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert any("code-review" in skill for skill in skills)
    assert any("platform-qa" in skill for skill in skills)


# --- Pi child process environment (Issues #157, #1673) ---


def test_stream_pi_sets_pi_env_on_the_process(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('PI_CODING_AGENT_DIR', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, pi_env={"PI_CODING_AGENT_DIR": "/agent-dir"})
    assert result == "/agent-dir"


def test_stream_pi_without_pi_env_keeps_inherited_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "inherited"


def test_stream_pi_strips_the_telegram_token_from_the_child_env(
    tmp_path, monkeypatch,
):
    """Issue #1673: the bot token stays in the Runner, never in Pi.

    Notifications are a bypass the child never needs, so the token is
    removed from the child environment while everything else stays
    inherited (the pre-#157 shape)."""
    monkeypatch.setenv("ORBI_TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', '') + '|' "
        "+ os.environ.get('ORBI_TELEGRAM_BOT_TOKEN', 'ABSENT'))\n"
    )
    result = pi_session.stream_pi(
        [sys.executable, "-c", script],
        ctx=RunContext(run_id="deadbeef", issue=24, branch="b",
                       worktree=tmp_path, source_repo="xqliu/orbi"),
        watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
    )
    assert result == "inherited|ABSENT"
