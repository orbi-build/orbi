"""One idle tick reads the shared GitHub state once (Issue #2218).

Measured on a deployment with no ready Issue: one tick read the same
`.github/orbi.toml` eight times, listed the `ai-blocked` Issues twice and
read every blocked Issue's comments twice — on a token running several
deployments that is the hourly API budget spent on the answer the tick
already had.

The acceptance is the `gh` command list of ONE idle tick, so these tests
run the REAL entry points (`runner.main`, `claim.pick_next_delivery`) and
the real scans against the `gh` adapter fake, then count the argv GitHub
was asked for.
"""
import orbi.check_permission as check_permission
import orbi.claim as claim
import orbi.milestone as milestone
import orbi.runner as runner
from orbi import (
    base_ci_fix, blocked_scan, config as config_domain, pilot_slots,
    stopped_scans, tick_reads,
)
from orbi.delivery_labels import BLOCKED_LABEL, FIX_NEEDED_LABEL
from seam import resume_deps
from tests.fakes.github import FakeGh, install_run_command
from tests.test_bootstrap_runner_main import _write_prompts

REPO = "owner/repo"


def _suppress_milestone_sweep(repo_dir):
    """A fresh failure marker: the milestone sweep is another scene's.

    `pick_next_delivery` reconciles Milestones from `slot_dir.parent`; the
    retry marker is what production writes after a failed sweep, and this
    scene is not about that sweep.
    """
    state_dir = pilot_slots.slot_dir_for(repo_dir).parent
    for status in (401, 404):
        marker = milestone._milestone_failure_marker(state_dir, REPO, status)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()


def _idle_tick(monkeypatch, gh, tmp_path):
    """One idle claim scan of one repository, through the real entry point.

    `_preflight` opens the tick's read scope for the whole tick; a scan
    driven on its own models the same tick by opening it here.
    """
    install_run_command(monkeypatch, gh)
    tick_reads.begin()
    _suppress_milestone_sweep(tmp_path)
    return claim.pick_next_delivery(
        [REPO], pilot_slots.slot_dir_for(tmp_path), 1,
        config=config_domain.RunnerConfig(), hooks=resume_deps(),
    )


def _commands(gh, prefix):
    return [command for command in gh.commands if command[:len(prefix)] == prefix]


def _contents_reads(gh):
    return [
        command for command in _commands(gh, ["gh", "api"])
        if "contents" in command[2]
    ]


def _blocked_lists(gh):
    return [
        command for command in _commands(gh, ["gh", "issue", "list"])
        if BLOCKED_LABEL in command
    ]


def _comments_reads(gh):
    """Every `gh issue view <n> ... --json comments` of the tick."""
    return [
        command for command in _commands(gh, ["gh", "issue", "view"])
        if "comments" in command
    ]


def _permission_reads(gh):
    """Every collaborator trust read of the tick, by the user it asked for."""
    return [
        command for command in _commands(gh, ["gh", "api"])
        if "collaborators" in command[2]
    ]


def _permission_stop_body(number, pr_number):
    """The stop comment the permission scene writes for one delivery."""
    return check_permission.comment_body(
        check_permission.CheckPermission(pr=pr_number),
        url="https://github.com/settings/installations/4242",
        run_id="a1b2c3d4", issue_number=number, issue_title="task",
        language_code="en",
    )


# --------------------------------------------------------------------------
# The acceptance: one read per repository / per blocked Issue in one tick
# --------------------------------------------------------------------------

def test_an_idle_tick_reads_each_repository_and_blocked_issue_once(
    monkeypatch, tmp_path,
):
    """One contents read, one `ai-blocked` list, one comments read per
    blocked Issue — in a tick that finds no work."""
    gh = FakeGh(REPO)
    gh.set_repo_config('dispatch_label = "repo-ready"\n')
    gh.add_issue(11, title="blocked one", labels=(BLOCKED_LABEL,))
    gh.add_issue(12, title="blocked two", labels=(BLOCKED_LABEL,))

    assert _idle_tick(monkeypatch, gh, tmp_path) is None

    assert len(_contents_reads(gh)) == 1, gh.commands
    assert len(_blocked_lists(gh)) == 1, gh.commands
    assert sorted(command[3] for command in _comments_reads(gh)) == ["11", "12"]


def test_every_tick_reads_the_shared_state_once_again(monkeypatch, tmp_path):
    """The whole entry point, two ticks in one process: each tick reads the
    policy file, the `ai-blocked` list and each blocked Issue's comments
    once — and the NEXT tick reads them again (the scope is the tick)."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    gh = FakeGh(REPO)
    gh.set_repo_config('dispatch_label = "repo-ready"\n')
    gh.add_issue(11, title="blocked", labels=(BLOCKED_LABEL,))
    install_run_command(monkeypatch, gh)
    _suppress_milestone_sweep(tmp_path)

    assert runner.main(["--config", str(config)]) == 0
    assert runner.main(["--config", str(config)]) == 0

    # One read per tick, and one tick is one process: two ticks, two reads.
    assert len(_contents_reads(gh)) == 2, gh.commands
    assert len(_blocked_lists(gh)) == 2, gh.commands
    assert len(_comments_reads(gh)) == 2, gh.commands


def test_a_scan_run_outside_a_tick_still_reads_for_itself(monkeypatch):
    """Outside a tick every caller reads for itself — the pre-#2218
    behavior a direct scan keeps (a caller that just changed the file is
    never served a stale answer)."""
    gh = FakeGh(REPO)
    gh.add_issue(11, title="blocked", labels=(BLOCKED_LABEL,))
    install_run_command(monkeypatch, gh)

    check_permission.recover(REPO)
    base_ci_fix.recover(REPO, language_code="en")

    assert len(_blocked_lists(gh)) == 2, gh.commands
    assert len(_comments_reads(gh)) == 2, gh.commands


# --------------------------------------------------------------------------
# The shared reader itself
# --------------------------------------------------------------------------

def test_the_reader_answers_from_one_read_per_issue(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_issue(11, title="blocked one", labels=(BLOCKED_LABEL,))
    gh.add_issue(12, title="blocked two", labels=(BLOCKED_LABEL,))
    install_run_command(monkeypatch, gh)
    tick_reads.begin()
    reads = blocked_scan.blocked_reads(REPO)

    assert sorted(issue["number"] for issue in reads.blocked_issues()) == [
        11, 12,
    ]
    assert reads.blocked_issues() == reads.blocked_issues()
    assert reads.comments(11) == reads.comments(11)
    assert reads.comments(12) == []

    assert len(_blocked_lists(gh)) == 1, gh.commands
    assert sorted(command[3] for command in _comments_reads(gh)) == ["11", "12"]


def test_a_retired_issue_is_never_walked_again(monkeypatch):
    """`retire` is what keeps the shared list honest after a scan ACTS: the
    Issue's label changed, so the later scans skip it exactly as a fresh
    list would (and a reader that had not listed yet still reads once)."""
    gh = FakeGh(REPO)
    gh.add_issue(11, title="blocked one", labels=(BLOCKED_LABEL,))
    gh.add_issue(12, title="blocked two", labels=(BLOCKED_LABEL,))
    install_run_command(monkeypatch, gh)
    tick_reads.begin()

    reads = blocked_scan.blocked_reads(REPO)
    assert reads.blocked_issues()
    reads.retire(11)

    assert [issue["number"] for issue in reads.blocked_issues()] == [12]
    assert len(_comments_reads(gh)) == 0, gh.commands

    # A reader that had not listed yet keeps its own (unretired) state.
    unread = blocked_scan.BlockedReads(REPO)
    unread.retire(11)
    assert sorted(issue["number"] for issue in unread.blocked_issues()) == [
        11, 12,
    ]
    assert len(_blocked_lists(gh)) == 2, gh.commands


def test_the_tick_reads_each_blocked_issues_trust_once(monkeypatch):
    """The trust read follows the ONE comments read: one permission read
    per blocked Issue per tick, not one per scan walking it (Issue #2218).

    The permission check itself is unchanged — still one read for each
    read of a blocked Issue's comments — and it is still read again on the
    next tick (it is never cached across ticks): only the repeated read of
    the SAME comments inside one tick is shared.
    """
    gh = FakeGh(REPO)
    for number, author in ((11, "one"), (12, "two"), (13, "three")):
        gh.add_issue(number, title=f"blocked {number}",
                     labels=(BLOCKED_LABEL,))
        gh.add_pr(number + 100, head=f"orbi/issue-{number}", oid="0" * 40)
        gh.comment(number, _permission_stop_body(number, number + 100),
                   login=author, association="NONE")
        gh.add_collaborator(author, "read")
    install_run_command(monkeypatch, gh)

    tick_reads.begin()
    check_permission.recover(REPO)
    base_ci_fix.recover(REPO, language_code="en")
    stopped_scans.run(REPO, language_code="en")

    assert sorted(command[2] for command in _permission_reads(gh)) == [
        f"repos/{REPO}/collaborators/one/permission",
        f"repos/{REPO}/collaborators/three/permission",
        f"repos/{REPO}/collaborators/two/permission",
    ], gh.commands
    assert len(_comments_reads(gh)) == 3, gh.commands

    # The trust read belongs to the tick, not to the process: the next
    # tick reads it again (a revoked permission is never served stale).
    tick_reads.end()
    tick_reads.begin()
    base_ci_fix.recover(REPO, language_code="en")
    assert len(_permission_reads(gh)) == 6, gh.commands


def test_the_second_scan_answers_from_the_same_tick_read(monkeypatch):
    """The base-branch-red scan asks for the state the permission scan
    already read: same list, same comments, no second read."""
    gh = FakeGh(REPO)
    gh.add_issue(11, title="blocked", labels=(BLOCKED_LABEL,))
    install_run_command(monkeypatch, gh)
    tick_reads.begin()

    assert check_permission.recover(REPO) == []
    assert base_ci_fix.recover(REPO, language_code="en") == []

    assert len(_blocked_lists(gh)) == 1, gh.commands
    assert len(_comments_reads(gh)) == 1, gh.commands


def test_one_reader_lets_the_permission_scan_resume_and_retires_it(
    monkeypatch,
):
    """Sharing the read never swallows the action: the permission scan
    resumes from it and retires the Issue, so the base-branch-red scan
    never walks the label its predecessor just removed."""
    gh = FakeGh(REPO)
    gh.add_issue(7, title="task", labels=(BLOCKED_LABEL,))
    gh.add_pr(8, head="orbi/issue-7", oid="0" * 40)
    gh.comment(7, _permission_stop_body(7, 8), login="orbi-bot",
               association="NONE")
    install_run_command(monkeypatch, gh)
    tick_reads.begin()

    assert check_permission.recover(REPO) == [7]
    assert base_ci_fix.recover(REPO, language_code="en") == []

    assert gh.issues[7]["labels"] == [FIX_NEEDED_LABEL]
    assert check_permission.parse(
        gh.issues[7]["comments"][-1]["body"],
    ).state == "resumed"
    assert len(_blocked_lists(gh)) == 1, gh.commands
    assert len(_comments_reads(gh)) == 1, gh.commands
