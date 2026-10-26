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
from orbi import failure, gh_response
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


def _block_body(*, state: str = "waiting", pr: int = PR_NUMBER) -> str:
    """The stop's real comment body: marker, failure block and record."""
    return check_permission.comment_body(
        check_permission.CheckPermission(pr=pr, state=state),
        url="https://github.com/settings/installations/4242",
        run_id="a1b2c3d4", issue_number=ISSUE_NUMBER, issue_title="task",
        language_code="en",
    )


def _append_comment(gh: FakeGh, body: str, *, number: int = 99) -> None:
    """Append one runner comment in the shape REAL gh reports it.

    `gh issue view --json comments` answers the GraphQL node id in `id`
    (a string no REST route takes) and the permalink whose
    `#issuecomment-<digits>` suffix is the REST id the in-place edit
    needs — the shape a numeric-only fake hid (Issue #1918).
    """
    gh.issues[ISSUE_NUMBER]["comments"].append({
        "id": f"IC_kwDOUC1jsc8AAAABfdXK{number}",
        "url": (
            f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}"
            f"#issuecomment-{number}"
        ),
        "author": {"login": "orbi-bot"}, "authorAssociation": "NONE",
        "createdAt": "2026-09-13T00:00:00Z", "body": body,
    })


def _blocked_issue(gh: FakeGh, *, body: str | None = None, **kwargs) -> None:
    """Replace the scene with an `ai-blocked` Issue carrying one stop."""
    gh.add_issue(ISSUE_NUMBER, title="task", labels=("ai-blocked",))
    # A comment WITHOUT a block: the scan reads past it.
    gh.comment(ISSUE_NUMBER, "Orbi opened PR: ...", login="orbi-bot",
               association="NONE")
    _append_comment(gh, _block_body(**kwargs) if body is None else body)


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
    assert check_permission.recover(REPO) == []
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

    assert check_permission.recover(REPO) == [ISSUE_NUMBER]
    # The resumable state the existing review/merge flow picks up.
    assert gh.issues[ISSUE_NUMBER]["labels"] == [FIX_NEEDED_LABEL]
    assert check_permission.parse(_bodies(gh)[0]).state == "resumed"
    assert len(_bodies(gh)) == 1  # the comment is edited, never duplicated
    assert "checks_permission_recovered" in caplog.text


def test_the_scan_leaves_an_untouched_block_alone(monkeypatch):
    """A block already marked resumed authorizes nothing: the scan only
    acts while the record is waiting."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh, state="resumed")
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == []
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_unreadable_block_is_skipped_with_a_warning(monkeypatch, caplog):
    gh = _scene(monkeypatch)
    _blocked_issue(gh, body=_block_body().replace(
        '<!-- orbi:check-permission:v1 {"schema":1',
        '<!-- orbi:check-permission:v1 {"schema":1,oops',
    ))
    caplog.set_level("WARNING")
    assert check_permission.recover(REPO) == []
    assert "unreadable check_permission block" in caplog.text
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_issue_without_a_failure_stop_is_skipped(monkeypatch):
    """A trusted record only authorizes a step from the Issue's failure
    stop: an Issue with no such comment is never scanned."""
    gh = _scene(monkeypatch)
    gh.add_issue(ISSUE_NUMBER, title="task", labels=("ai-blocked",))
    gh.comment(ISSUE_NUMBER, "please look", login="maintainer")
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == []
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_unreadable_failure_block_is_skipped_with_a_warning(
    monkeypatch, caplog,
):
    """A corrupted `orbi:failure:v1` block states no reason: it never
    falls back to an older record it superseded."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh, body=(
        "<!-- orbi:run=a1b2c3d4 -->\n"
        "<!-- orbi:comment=failure -->\n\n"
        "**blocked**\n\n"
        '<!-- orbi:failure:v1 {"schema":1} -->\n\n'
        + check_permission.render(check_permission.CheckPermission(
            pr=PR_NUMBER,
        ))
    ))
    caplog.set_level("WARNING")
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == []
    assert "unreadable orbi:failure:v1 block" in caplog.text
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_a_later_stop_for_another_reason_is_never_resumed(monkeypatch):
    """The Issue #1833 gate: a `waiting` record an older stop left behind
    never clears a LATER `ai-blocked` decided for another reason."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh)
    # A human relabelled the Issue instead of approving, and the resumed
    # delivery then stopped for a different reason.
    _append_comment(gh, body=(
        "<!-- orbi:run=e5f6a7b8 -->\n"
        "<!-- orbi:comment=failure -->\n\n"
        + failure.render(failure.Failure(
            reason_code="provider_quota", action_code="wait_quota",
            retry_safe=True, outcome="blocked",
        ))
    ), number=100)
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == []
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_a_newer_stop_without_its_own_record_never_falls_back(monkeypatch):
    """The gate reads the record of the SAME comment: a newer trusted
    failure that names the reason but carries no permission block
    authorizes nothing, and the older waiting record it superseded is
    never resumed."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh)
    _append_comment(gh, body=(
        "<!-- orbi:run=e5f6a7b8 -->\n"
        "<!-- orbi:comment=failure -->\n\n"
        + failure.render(failure.Failure(
            reason_code=check_permission.REASON_CODE,
            action_code="check_credentials", retry_safe=False,
            outcome="blocked",
        ))
    ), number=100)
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == []
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_an_unexpected_read_failure_is_logged_and_skipped(
    monkeypatch, caplog,
):
    """A failed read that is NOT the permission refusal keeps the existing
    failure handling: logged with its evidence, the delivery stays paused."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh)
    gh.set_check_read_failure(True, message="gh: Not Found (HTTP 404)")
    caplog.set_level("INFO")
    assert check_permission.recover(REPO) == []
    assert "check_permission_read_failed" in caplog.text
    assert gh.issues[ISSUE_NUMBER]["labels"] == ["ai-blocked"]


def test_the_recovery_edits_the_real_gh_comment_shape(monkeypatch):
    """`gh issue view --json comments` reports the GraphQL node id in `id`:
    the in-place state edit must still land, from the permalink's REST id."""
    gh = _scene(monkeypatch)
    _blocked_issue(gh)
    gh.set_check_read_failure(False)
    assert check_permission.recover(REPO) == [ISSUE_NUMBER]
    assert check_permission.parse(_bodies(gh)[-1]).state == "resumed"
    assert len(_bodies(gh)) == 2


def test_a_block_without_a_comment_id_is_not_edited(monkeypatch, caplog):
    """The in-place edit needs the REST id every real comment carries."""
    caplog.set_level("WARNING")
    record = check_permission.CheckPermission(pr=PR_NUMBER)
    check_permission._edit_state(REPO, ISSUE_NUMBER, {"body": "x"}, record)
    assert "has no REST id" in caplog.text


@pytest.mark.parametrize("comment, expected", [
    # `gh issue view --json comments` reports the GraphQL node id next to
    # the permalink: the permalink's suffix is the REST id a PATCH takes.
    ({"id": "IC_kwDOUC1jsc8AAAABfdXK_Q",
      "url": "https://github.com/o/r/issues/7#issuecomment-6406130429"},
     6406130429),
    # A numeric id field is the REST id already (a `gh api` read).
    ({"id": 41, "url": "https://github.com/o/r/issues/7#issuecomment-41"}, 41),
    # A bool is not an id (it is an `int` subclass in Python).
    ({"id": True, "url": "https://github.com/o/r/issues/7#issuecomment-42"}, 42),
    ({"url": "https://github.com/o/r/issues/7"}, None),
    ({}, None),
])
def test_the_rest_id_of_a_comment_matches_the_real_gh_shape(
    comment, expected,
):
    assert check_permission.rest_comment_id(comment) == expected


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
    _blocked_issue(gh)
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
