"""Resume yields when another executor's run is live (Issue #2299).

One Issue can be served by two Orbi deployments under two GitHub ACCOUNTS
(an Orbi Cloud bot and a self-hosted engine). The self-hosted engine does not
trust the Cloud bot's comments — the bot has no repository write permission —
so `resume_scene` found no trusted scene and the scan posted
`failure.scene_blocked` while the Cloud run was still reviewing the PR. The
opened-PR scan now reads the shared liveness scan (orbi#2298) BEFORE the scene
recovery and before the external-PR route: another account with a live started
run on the Issue makes this executor YIELD — no label write, no comment,
nothing pushed — and the other delivery keeps running.

These tests drive the real scan over simulated comment lists and a simulated
local account (the gh credential), in both directions: the self-hosted side
sees the Cloud run (an UNTRUSTED author, still live) and Cloud sees the
self-hosted run (a trusted author, a scene it would otherwise resume).
"""
import pytest

import orbi.claim as claim
from orbi import scene as scene_mod
from tests.fakes.github import FakeGh
from tests.seam import resume_deps, seam


REPO = "owner/repo"
ISSUE = 9
OWN_RUN = "aaaa1111"
OTHER_RUN = "bbbb2222"
PR_URL = f"https://github.com/{REPO}/pull/9"


def _started(run_id: str) -> str:
    return (
        f"<!-- orbi:run={run_id} -->\n"
        "<!-- orbi:comment=started -->\n"
        "**Orbi started Pi:** run details"
    )


def _terminal(run_id: str, kind: str) -> str:
    return f"<!-- orbi:run={run_id} -->\n<!-- orbi:comment={kind} -->\ndetail"


def _opened_pr(run_id: str = OTHER_RUN) -> str:
    return (
        f"<!-- orbi:run={run_id} -->\n"
        + scene_mod.render(scene_mod.Scene(
            run_id=run_id, base_branch="main", base_sha="a" * 40,
            pr_url=PR_URL,
        ))
        + f"\nOrbi opened PR: {PR_URL}"
    )


@pytest.fixture
def gh(monkeypatch):
    """One repository, both executor accounts, the self-hosted one local.

    The self-hosted account is a repository writer (its own delivery); the
    Cloud bot is NOT (the observed shape: `resume_scene` finds no trusted
    scene). The local executor is swapped by assigning `gh.login`, so the
    same comment list is read from both sides of the race.
    """
    fake = FakeGh(REPO, login="selfhosted")
    monkeypatch.setattr(seam, "run_command", fake)
    fake.add_collaborator("selfhosted", "admin")
    fake.add_collaborator("maintainer", "admin")
    fake.add_issue(ISSUE, title="delivery", labels=("ai-pr-opened",))
    fake.add_pr(ISSUE, head=f"orbi/owner-repo-issue-{ISSUE}",
                checks=({"name": "tests", "status": "COMPLETED",
                         "conclusion": "SUCCESS"},))
    return fake


def _hooks_recording_calls():
    """The resume hooks, recording which of them the scan reached."""
    seen: list[str] = []
    base = resume_deps()

    def counting(name, call):
        def wrapper(*args, **kwargs):
            seen.append(name)
            return call(*args, **kwargs)
        return wrapper

    hooks = base._replace(
        resume_scene=counting("resume_scene", base.resume_scene),
        route_external_pr_ticket=counting(
            "route_external_pr_ticket", base.route_external_pr_ticket),
    )
    return hooks, seen


def _scan(tmp_path, hooks=None):
    return claim.pick_resumable_delivery(
        REPO, tmp_path / "slots", 1,
        hooks=hooks or resume_deps(), language_code="en",
    )


def _writes(fake) -> list[list[str]]:
    """Every write the local executor made (a label edit or a comment)."""
    return [
        command for command in fake.commands
        if command[:3] in (["gh", "issue", "edit"], ["gh", "issue", "comment"])
    ]


# --- the acceptance scene: two executors, one Issue -----------------------


def test_selfhosted_yields_to_a_live_cloud_run(gh, tmp_path, caplog):
    """Cloud opened the PR and is still reviewing it: the self-hosted engine
    yields instead of writing the missing-scene block it writes today."""
    gh.comment(ISSUE, _opened_pr(), login="cloud")
    gh.comment(ISSUE, _started(OTHER_RUN), login="cloud")
    hooks, seen = _hooks_recording_calls()
    with caplog.at_level("INFO"):
        assert _scan(tmp_path, hooks) is None
    assert "reason=other_executor_live" in caplog.text
    # Neither the scene recovery nor the external-PR route ran, and this
    # executor wrote nothing: labels kept, no comment, no push.
    assert seen == []
    assert _writes(gh) == []
    assert gh.issues[ISSUE]["labels"] == ["ai-pr-opened"]


def test_cloud_yields_to_a_live_selfhosted_run(gh, tmp_path, caplog):
    """Trust is asymmetric: Cloud trusts this owner's comments and would
    resume the self-hosted PR. It yields instead."""
    gh.comment(ISSUE, _opened_pr(run_id=OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.login = "cloud"
    hooks, seen = _hooks_recording_calls()
    with caplog.at_level("INFO"):
        assert _scan(tmp_path, hooks) is None
    assert "reason=other_executor_live" in caplog.text
    assert seen == []
    assert _writes(gh) == []


def test_the_guard_precedes_the_external_pr_route(gh, tmp_path, caplog):
    """The #726 external-PR route lives inside the two `except` branches, so
    the guard before `resume_scene` protects it too: an external marker with
    a live Cloud run is not taken over while that run is still live."""
    gh.issues[ISSUE]["body"] = "<!-- orbi:external-pr:63 -->"
    gh.comment(ISSUE, _started(OTHER_RUN), login="cloud")
    hooks, seen = _hooks_recording_calls()
    with caplog.at_level("INFO"):
        assert _scan(tmp_path, hooks) is None
    assert "reason=other_executor_live" in caplog.text
    assert seen == []
    assert _writes(gh) == []


# --- nothing changes without another executor ----------------------------


def test_a_trusted_scene_is_still_resumed_with_no_other_executor(gh, tmp_path):
    """A same-account restart is this deployment's own run: its trusted scene
    resumes exactly as before."""
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _started(OTHER_RUN), login="selfhosted")
    gh.comment(ISSUE, _opened_pr(run_id=OWN_RUN), login="selfhosted")
    hooks, seen = _hooks_recording_calls()
    issue, found = _scan(tmp_path, hooks)
    assert issue["number"] == ISSUE
    assert found.parsed.run_id == OWN_RUN
    # The guard let the candidate through: the scene recovery ran.
    assert seen == ["resume_scene"]


def test_a_missing_scene_still_blocks_with_no_other_executor(gh, tmp_path):
    """The current `failure.scene_blocked` rule is untouched: with no other
    executor running, a missing scene is still ai-blocked for a human."""
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, "a maintainer note", login="maintainer")
    hooks, seen = _hooks_recording_calls()
    assert _scan(tmp_path, hooks) is None
    # No yield: the scene recovery ran, its missing scene probed the
    # external route (no marker here) and only then blocked.
    assert seen == ["resume_scene", "route_external_pr_ticket"]
    # The terminal state the rule has always written (the opened-PR label
    # beside it is the pre-#2299 patch, outside this ticket).
    assert "ai-blocked" in gh.issues[ISSUE]["labels"]
    posted = [comment["body"] for comment in gh.issues[ISSUE]["comments"]]
    assert any("no trusted 'Orbi opened PR' scene comment" in body
               for body in posted)


def test_a_human_restored_scene_and_orbi_commands_do_not_yield(gh, tmp_path):
    """A maintainer's restore note and an /orbi command steer the delivery;
    neither is another executor's run."""
    gh.comment(ISSUE, "restored the labels after the incident",
               login="maintainer")
    gh.comment(ISSUE, "/orbi retry", login="maintainer")
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _opened_pr(run_id=OWN_RUN), login="selfhosted")
    issue, found = _scan(tmp_path)
    assert issue["number"] == ISSUE
    assert found.parsed.run_id == OWN_RUN


def test_an_external_contributor_takeover_is_not_another_executor(gh, tmp_path):
    """A contributor's PR chatter is not a run: its marker is quoted, not
    anchored at the top of the body, so the scene still resumes."""
    gh.comment(ISSUE, "> <!-- orbi:comment=started -->\nquoted in a review",
               login="contributor")
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _opened_pr(run_id=OWN_RUN), login="selfhosted")
    issue, found = _scan(tmp_path)
    assert issue["number"] == ISSUE
    assert found.parsed.run_id == OWN_RUN


# --- the acceptance addendum: liveness is paired by account ---------------


def test_a_yielded_started_comment_never_blocks_a_later_resume(gh, tmp_path):
    """Cloud's run ended; the self-hosted start posted after it was the
    yielding side and is never live, so this executor resumes its own
    delivery."""
    gh.comment(ISSUE, _opened_pr(run_id=OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _started(OTHER_RUN), login="cloud")
    gh.comment(ISSUE, _started(OWN_RUN), login="selfhosted")
    gh.comment(ISSUE, _terminal(OTHER_RUN, "merged"), login="cloud")
    issue, found = _scan(tmp_path)
    assert issue["number"] == ISSUE
    assert found.parsed.run_id == OWN_RUN


def test_another_accounts_failure_does_not_end_a_live_run(
    gh, tmp_path, caplog,
):
    """paseo-orbi#61: the self-hosted failure landed while the Cloud run was
    still reviewing. A terminal comment ends only its OWN account's run, so
    Cloud still counts as live and this executor still yields."""
    gh.comment(ISSUE, _opened_pr(), login="cloud")
    gh.comment(ISSUE, _started(OTHER_RUN), login="cloud")
    gh.comment(ISSUE, _terminal(OWN_RUN, "failure"), login="selfhosted")
    with caplog.at_level("INFO"):
        assert _scan(tmp_path) is None
    assert "reason=other_executor_live" in caplog.text
    assert _writes(gh) == []
