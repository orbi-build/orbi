"""The missing check-read permission scene (Issue #1918).

A GitHub App installation that has not approved a permission the check
read needs makes GitHub answer `Resource not accessible by integration`
(HTTP 403). Before this scene the Runner retried the same read on every
tick without telling anyone: the Issue sat `ai-pr-opened` for days and
the health report counted a crash loop.

The acceptance proven here, all through the REAL entry points:

1. `runner.delivery_step` with that 403 posts exactly ONE comment naming
   the `statuses: read` permission and the approval page of THIS
   installation, and the Issue gets `ai-blocked`;
2. a later tick with the same 403 posts no comment and does not raise;
3. once the read works again the scan swaps `ai-blocked` for
   `ai-fix-needed` — the state the existing resume flow picks up and
   merges as usual.
"""
import subprocess

import pytest

import orbi.check_permission as check_permission
import orbi.runner as runner
from orbi import config as config_domain
from orbi import gh_response
from orbi.delivery_labels import FIX_NEEDED_LABEL
from seam import seam
from tests.fakes.github import FakeGh

REPO = "owner/repo"
ISSUE_NUMBER = 7
PR_NUMBER = 8
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def _scene(monkeypatch, *, owner_type="Organization",
           check_read_failure=True, fail_on=None) -> FakeGh:
    """One open-PR delivery whose check read the App refuses.

    `fail_on` is a predicate over the argv for a test that needs one
    particular call to fail server-side (the scan's own failure path).
    """
    gh = FakeGh(REPO)
    gh.add_issue(ISSUE_NUMBER, title="task", labels=("ai-pr-opened",))
    gh.add_pr(PR_NUMBER, head=f"orbi/issue-{ISSUE_NUMBER}", oid=HEAD)
    gh.set_owner_type(owner_type)
    gh.set_check_read_failure(check_read_failure)

    def fake_run(command, **kwargs):
        if fail_on is not None and fail_on(command):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Server Error (HTTP 500)",
            )
        return gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    return gh


def _issue() -> dict:
    return {
        "number": ISSUE_NUMBER, "title": "task", "body": "",
        "labels": [{"name": "ai-pr-opened"}],
    }


def _config() -> config_domain.RunnerConfig:
    return config_domain.RunnerConfig(installation_id=4242)


def _bodies(gh: FakeGh) -> list[str]:
    return [comment["body"] for comment in gh.issues[ISSUE_NUMBER]["comments"]]


def _blocked_issue(gh: FakeGh, *, body: str) -> None:
    """Replace the scene with an `ai-blocked` Issue carrying one block."""
    gh.add_issue(ISSUE_NUMBER, title="task", labels=("ai-blocked",))
    # A comment WITHOUT a block: the scan reads past it.
    gh.comment(ISSUE_NUMBER, "Orbi opened PR: ...", login="orbi-bot",
               association="NONE")
    gh.issues[ISSUE_NUMBER]["comments"].append({
        "id": 99, "url": f"https://github.com/{REPO}/issues/7#issuecomment-99",
        "author": {"login": "orbi-bot"}, "authorAssociation": "NONE",
        "createdAt": "2026-09-13T00:00:00Z", "body": body,
    })


# --------------------------------------------------------------------------
# The classification: GitHub's permission refusal, never a transient read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("error, expected", [
    # A 403 with quota left is a permission failure.
    (gh_response.GitHubRequestFailed(
        1, ["gh"], status=403, ratelimit_remaining=4999), True),
    # A 403 with no quota is GitHub's rate limit: transient, not permission.
    (gh_response.GitHubRequestFailed(
        1, ["gh"], status=403, ratelimit_remaining=0), False),
    (gh_response.GitHubRequestFailed(1, ["gh"], status=401), False),
    (gh_response.GitHubRequestFailed(1, ["gh"], status=500), False),
    # `gh pr view` prints no response status: GitHub's own sentence is the
    # only signal the refused read carries (production's shape).
    (subprocess.CalledProcessError(
        1, ["gh"], stderr="gh: Resource not accessible by integration "
                          "(HTTP 403)"), True),
    (subprocess.CalledProcessError(
        1, ["gh"], stderr="gh: Bad Gateway (HTTP 502)"), False),
])
def test_the_permission_refusal_is_classified_from_its_own_signal(
    error, expected,
):
    assert check_permission.is_missing_permission(error) is expected


# --------------------------------------------------------------------------
# The approval page
# --------------------------------------------------------------------------

@pytest.mark.parametrize("owner_type, installation_id, expected", [
    ("Organization", 4242,
     "https://github.com/organizations/owner/settings/installations/4242"),
    ("User", 4242, "https://github.com/settings/installations/4242"),
    # Without a configured id the account's installation list is the page —
    # never a guessed id.
    ("Organization", None,
     "https://github.com/organizations/owner/settings/installations"),
    ("User", None, "https://github.com/settings/installations"),
])
def test_the_approval_page_follows_the_installation_account(
    monkeypatch, owner_type, installation_id, expected,
):
    _scene(monkeypatch, owner_type=owner_type)
    assert check_permission.approval_url(
        REPO, installation_id=installation_id,
    ) == expected


def test_a_repository_read_without_an_owner_type_fails_fast(monkeypatch):
    """A read that reports no account type is a failed read, never a
    guessed account (a guessed URL sends the reader somewhere else)."""
    _scene(monkeypatch, owner_type="")
    with pytest.raises(ValueError, match="owner type is missing"):
        check_permission.approval_url(REPO, installation_id=4242)


# --------------------------------------------------------------------------
# 1. ONE comment + ai-blocked, from the real delivery step
# --------------------------------------------------------------------------

def test_the_delivery_stops_with_the_permission_and_the_approval_page(
    monkeypatch, caplog, bind_run_id,
):
    gh = _scene(monkeypatch)
    bind_run_id("a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, _issue(), _config(), REPO)

    bodies = _bodies(gh)
    assert len(bodies) == 1, bodies
    body = bodies[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "statuses: read" in body
    assert "https://github.com/organizations/owner/settings/installations/4242" in body
    assert f"PR #{PR_NUMBER} is open" in body
    # The machine-readable reason rides the failure block the closed set
    # owns (Issue #1918 records it, it is not prose only).
    assert "checks_permission_missing" in body
    assert "orbi:check-permission:v1" in body
    record = check_permission.parse(body)
    assert (record.pr, record.state) == (PR_NUMBER, "waiting")
    # ai-blocked ALONE: the delivery state is terminal.
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]
    assert "checks_permission_blocked" in caplog.text
    assert "issue=7" in caplog.text


def test_the_comment_is_localized(monkeypatch, bind_run_id):
    gh = _scene(monkeypatch)
    bind_run_id("a1b2c3d4")
    config = config_domain.RunnerConfig(
        installation_id=4242, message_language="zh",
    )
    runner.delivery_step(PR_URL, _issue(), config, REPO)
    body = _bodies(gh)[0]
    assert "已打开" in body
    assert "statuses: read" in body


def test_an_ordinary_read_failure_still_crashes_the_tick(monkeypatch):
    """Only the permission refusal is this scene: every other failed read
    keeps its existing failure handling (never silently swallowed)."""
    gh = _scene(monkeypatch)
    gh.set_check_read_failure(True, message="gh: Not Found (HTTP 404)")
    with pytest.raises(subprocess.CalledProcessError):
        runner.delivery_step(PR_URL, _issue(), _config(), REPO)


# --------------------------------------------------------------------------
# 2. A later tick: no comment, no crash
# --------------------------------------------------------------------------

def test_a_still_refused_read_posts_nothing_and_does_not_crash(
    monkeypatch, caplog, bind_run_id,
):
    gh = _scene(monkeypatch)
    bind_run_id("a1b2c3d4")
    runner.delivery_step(PR_URL, _issue(), _config(), REPO)
    posted = _bodies(gh)
    assert len(posted) == 1
    caplog.clear()
    # The next tick only runs the recovery scan: the Issue is ai-blocked.
    assert check_permission.recover(REPO, language_code="en") == []
    assert _bodies(gh) == posted
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]
    # A refused read is the expected waiting state, never a logged crash.
    assert "check_permission_read_failed" not in caplog.text


# --------------------------------------------------------------------------
# 3. The read works again: ai-blocked -> ai-fix-needed
# --------------------------------------------------------------------------

def test_the_recovery_swaps_the_labels_and_the_delivery_resumes(
    monkeypatch, caplog, bind_run_id,
):
    gh = _scene(monkeypatch)
    bind_run_id("a1b2c3d4")
    runner.delivery_step(PR_URL, _issue(), _config(), REPO)
    # The account owner approved the permission.
    gh.set_check_read_failure(False)
    caplog.set_level("INFO")

    assert check_permission.recover(REPO, language_code="en") == [ISSUE_NUMBER]
    # The resumable state the existing review/merge flow picks up.
    assert gh.issues[ISSUE_NUMBER]["labels"] == [FIX_NEEDED_LABEL]
    assert check_permission.parse(_bodies(gh)[0]).state == "resumed"
    assert len(_bodies(gh)) == 1  # the comment is edited, never duplicated
    assert "checks_permission_recovered" in caplog.text


def test_the_scan_leaves_an_untouched_block_alone(monkeypatch):
    """A block already marked resumed authorizes nothing: the scan only
    acts while the record is waiting."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh, body=check_permission.render(
        check_permission.CheckPermission(pr=PR_NUMBER, state="resumed"),
    ))
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO, language_code="en") == []
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_unreadable_block_is_skipped_with_a_warning(monkeypatch, caplog):
    gh = _scene(monkeypatch)
    _blocked_issue(
        gh, body='<!-- orbi:check-permission:v1 {"state": "waiting"} -->',
    )
    caplog.set_level("WARNING")
    assert check_permission.recover(REPO, language_code="en") == []
    assert "unreadable check_permission block" in caplog.text
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_unexpected_read_failure_is_logged_and_skipped(
    monkeypatch, caplog,
):
    """A failed read that is NOT the permission refusal keeps the existing
    failure handling: logged with its evidence, the delivery stays paused."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh, body=check_permission.render(
        check_permission.CheckPermission(pr=PR_NUMBER),
    ))
    gh.set_check_read_failure(True, message="gh: Not Found (HTTP 404)")
    caplog.set_level("INFO")
    assert check_permission.recover(REPO, language_code="en") == []
    assert "check_permission_read_failed" in caplog.text
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_a_block_without_a_comment_id_is_not_edited(monkeypatch, caplog):
    """The in-place edit needs the REST id every real comment carries."""
    caplog.set_level("WARNING")
    record = check_permission.CheckPermission(pr=PR_NUMBER)
    check_permission._edit_state(REPO, ISSUE_NUMBER, {"body": "x"}, record)
    assert "has no REST id" in caplog.text


@pytest.mark.parametrize("payload, message", [
    ({"schema": 2, "pr": 8, "state": "waiting"}, "unsupported schema"),
    ({"schema": 1, "pr": "8", "state": "waiting"}, "positive integer"),
    ({"schema": 1, "pr": 0, "state": "waiting"}, "positive integer"),
    ({"schema": 1, "pr": 8, "state": "paused"}, "unknown state"),
    ({"schema": 1, "pr": 8}, "missing fields"),
])
def test_a_malformed_block_fails_fast(payload, message):
    import json

    body = ("<!-- orbi:check-permission:v1 "
            f"{json.dumps(payload)} -->")
    with pytest.raises(check_permission.CheckPermissionError, match=message):
        check_permission.parse(body)


# --------------------------------------------------------------------------
# The tick wiring: the scan runs BEFORE the resume scan
# --------------------------------------------------------------------------

def test_a_failed_scan_never_stops_the_tick(
    monkeypatch, tmp_path, caplog,
):
    """The scan is a pure bypass (the same rule as every other sweep): a
    broken GitHub query logs and the ordinary delivery scans still run."""
    from orbi import claim
    from orbi.resume_scan import ResumeHooks

    _scene(monkeypatch, fail_on=lambda command: "ai-blocked" in command)
    caplog.set_level("INFO")
    noop = lambda *args, **kwargs: None
    hooks = ResumeHooks(
        resume_scene=noop, route_external_pr_ticket=lambda *args, **kw: False,
        block_scene_failure=noop, recover_missing_pr_scene=noop,
        has_recoverable_pr_scene=lambda *args, **kw: False, comment_pr=noop,
    )
    assert claim.pick_next_delivery([REPO], tmp_path, 1, hooks=hooks) is None
    assert "check_permission_scan_failed" in caplog.text


def test_the_claim_tick_recovers_before_the_resume_scan(
    monkeypatch, tmp_path, caplog,
):
    """Acceptance 3 in a real tick: the recovery scan runs BEFORE the
    resume scan (the same slot `base_ci_red.recover` uses), so a delivery
    whose read works again is already on the resumable path in THIS tick —
    `ai-fix-needed` is the state the existing review/merge flow picks up."""
    from orbi import claim
    from orbi.resume_scan import ResumeHooks

    gh = _scene(monkeypatch)
    _blocked_issue(gh, body=check_permission.render(
        check_permission.CheckPermission(pr=PR_NUMBER),
    ))
    gh.set_check_read_failure(False)
    caplog.set_level("INFO")
    noop = lambda *args, **kwargs: None
    hooks = ResumeHooks(
        resume_scene=noop, route_external_pr_ticket=lambda *args, **kw: False,
        block_scene_failure=noop, recover_missing_pr_scene=noop,
        has_recoverable_pr_scene=lambda *args, **kw: False, comment_pr=noop,
    )
    assert claim.pick_next_delivery([REPO], tmp_path, 1, hooks=hooks) is None
    assert gh.issues[ISSUE_NUMBER]["labels"] == [FIX_NEEDED_LABEL]
    assert "checks_permission_recovered" in caplog.text


def test_the_rendered_block_is_the_documented_example():
    """The docs example is the REAL render output, never prose."""
    assert check_permission.render(
        check_permission.CheckPermission(pr=123),
    ) == (
        '<!-- orbi:check-permission:v1 {"schema":1,"pr":123,'
        '"state":"waiting"} -->'
    )
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in ("docs/workflow.mdx", "docs/zh/workflow.mdx"):
        text = (root / relative).read_text(encoding="utf-8")
        assert '{"schema":1,"pr":123,"state":"waiting"}' in text, relative
        assert "checks_permission_missing" in text, relative
