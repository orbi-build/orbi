"""Fake-based adapter-seam tests for the delivery PR create (Issue #1324).

The delivery closeout's ``gh pr create`` runs through the bounded write
retry. These tests live in a ``*_fakes.py`` module because at the adapter
seam the ``gh`` argv IS the contract (Article 5.2): the fake answers argv
and the tests assert the observable setup/closeout behavior — a transient
GraphQL hiccup is retried, a lost response that really created the PR is
not duplicated, and a non-transient failure is not retried. Only the ONE
subprocess seam is patched.
"""
import json
import subprocess

import pytest

import orbi.github as github
import orbi.runner as runner
from orbi.delivery_scene import RunContext
from seam import seam

from tests.test_bootstrap_runner import (
    DELIVER_BRANCH,
    FAKE_HEAD_SHA,
    FAKE_PR_URL,
    FAKE_RUN_ID,
    _seed_deliver_run_state,
    fake_deliver_run,
)

GRAPHQL_TRANSIENT = (
    "GraphQL: Something went wrong while executing your query on "
    "2026-09-23T14:13:38Z."
)


def _ctx(tmp_path):
    return RunContext(
        run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH,
        worktree=tmp_path, source_repo="o/r",
    )


def _pr_create_retry_run(*, fail_times, applied_after_failure=False):
    """Stateful fake for the PR-create retry scene (Issue #1324).

    ``gh pr list`` reflects the real GitHub state: empty until a create
    succeeds, or — when a lost response really created the PR — once the
    injected ``applied_after_failure`` flag flips.
    """
    calls = []
    create_attempts = []
    created = []
    attempted = {"n": 0}
    applied = {"done": False, "body": ""}

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "pr", "list"]:
            prs = []
            if created:
                prs.append(created[0])
            elif applied_after_failure and applied["done"]:
                prs.append({
                    "url": FAKE_PR_URL,
                    "baseRefName": "main",
                    "headRefName": DELIVER_BRANCH,
                    "headRefOid": FAKE_HEAD_SHA,
                    "body": applied["body"],
                })
            return json.dumps(prs)
        if command[:3] == ["gh", "pr", "create"]:
            attempted["n"] += 1
            create_attempts.append(command)
            applied["body"] = command[command.index("--body") + 1]
            if attempted["n"] <= fail_times:
                applied["done"] = True
                raise subprocess.CalledProcessError(
                    1, command, stderr=GRAPHQL_TRANSIENT,
                )
            created.append({
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefName": DELIVER_BRANCH,
                "headRefOid": FAKE_HEAD_SHA,
                "body": applied["body"],
            })
            return FAKE_PR_URL
        return fake_deliver_run(command, **kwargs)

    return fake_run, create_attempts


def test_deliver_pr_retries_transient_pr_create(monkeypatch, tmp_path):
    """Issue #1324: a transient GraphQL hiccup on the PR create is retried
    by the bounded write path (two attempts) and the delivery proceeds to
    `verify_pr` — a finished delivery is not thrown away."""
    _seed_deliver_run_state(tmp_path)
    fake_run, create_attempts = _pr_create_retry_run(fail_times=1)
    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    monkeypatch.setattr(seam, "run_command", fake_run)

    assert runner.deliver_pr(
        _ctx(tmp_path), "main", "9" * 40, issue_title="t",
        repo_dir=tmp_path,
    ) == FAKE_PR_URL
    assert len(create_attempts) == 2


def test_deliver_pr_lost_pr_create_response_does_not_duplicate(
    monkeypatch, tmp_path,
):
    """Issue #1324: when the response is lost AFTER GitHub created the PR,
    the idempotency hook sees it and no second create is issued."""
    _seed_deliver_run_state(tmp_path)
    fake_run, create_attempts = _pr_create_retry_run(
        fail_times=1, applied_after_failure=True,
    )
    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    monkeypatch.setattr(seam, "run_command", fake_run)

    assert runner.deliver_pr(
        _ctx(tmp_path), "main", "9" * 40, issue_title="t",
        repo_dir=tmp_path,
    ) == FAKE_PR_URL
    assert len(create_attempts) == 1


def test_deliver_pr_pr_create_non_transient_fails_without_retry(
    monkeypatch, tmp_path,
):
    """Issue #1324: a validation/permission failure (HTTP 422) is raised on
    the first attempt, with no retry and no sleep."""
    _seed_deliver_run_state(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "pr", "create"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 422: Validation Failed",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        runner.deliver_pr(
            _ctx(tmp_path), "main", "9" * 40, issue_title="t",
            repo_dir=tmp_path,
        )
    assert failure.value.stderr == "HTTP 422: Validation Failed"
    assert len([
        command for command in calls
        if command[:3] == ["gh", "pr", "create"]
    ]) == 1
