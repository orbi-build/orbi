"""Fan-out patch target for the subprocess seam (Issue #785).

Since Issue #785 the seam primitives (`run_command`,
`run_git_network_command`, `run_gh_read_command`) live in `orbi.journal`
/ `orbi.github` / `orbi.gitops`, and every module binds them into its own
namespace. A fake must therefore replace the name in EVERY module that
binds it, so it intercepts the seam no matter which module executes the
function under test. ``monkeypatch.setattr(seam, "run_command", fake)``
does exactly that: reading resolves the current binding (monkeypatch's
old-value capture) and writing fans the value out to all bindings, so
monkeypatch's teardown fans the original back.
"""
import orbi.base_ci_red as base_ci_red
import orbi.blocked_comment as blocked_comment
import orbi.cli as cli
import orbi.cli_commands as cli_commands
import orbi.cli_doctor as cli_doctor
import orbi.cli_source as cli_source
import orbi.branch_reclaim as branch_reclaim
import orbi.check_permission as check_permission
import orbi.criteria_command as criteria_command
import orbi.comment_trust as comment_trust
import orbi.suggest as suggest
import orbi.split_command as split_command
import orbi.clarify as clarify
import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.github as github
import orbi.github_feedback as github_feedback
import orbi.gitops as gitops
import orbi.journal as journal
import orbi.merge_exec as merge_exec
import orbi.milestone as milestone
import orbi.milestone_command as milestone_command
import orbi.ticket_command as ticket_command
import orbi.progress as progress
import orbi.repo_config as repo_config
import orbi.release as release
import orbi.release_closeout as release_closeout
import orbi.release_git as release_git
import orbi.release_run as release_run
import orbi.release_notes as release_notes
import orbi.review_merge as review_merge
import orbi.review_rounds as review_rounds
import orbi.pi_session as pi_session
import orbi.run_state as run_state
import orbi.pr_verification as pr_verification
import orbi.resume_scan as resume_scan
import orbi.resume_verification as resume_verification
import orbi.runner as runner

_MODULES = (
    journal, github, github_feedback, gitops, milestone, milestone_command,
    ticket_command,
    progress, cli_source, release, release_closeout, release_git,
    release_notes, release_run, pi_session,
    runner, review_merge, merge_exec, review_rounds, run_state, claim,
    resume_scan,
    pr_verification, resume_verification,
    repo_config, clarify, comment_trust,
    branch_reclaim, failure_report, suggest, cli, blocked_comment,
    base_ci_red, cli_commands, cli_doctor, check_permission, split_command,
    criteria_command,
)


class Seam:
    """Set/read/undo a seam name across every module that binds it."""

    def __init__(self) -> None:
        # Per overlaid name, the (module, binding) pairs its FIRST write
        # replaced, so the overlay can be undone (Issue #1948). Written
        # without `__setattr__`: the proxy keeps no instance attribute of
        # its own, which is exactly why pytest's undo path deletes.
        object.__setattr__(self, "_previous", {})

    def __getattr__(self, name: str):
        for module in _MODULES:
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        # Rebind only the modules whose CURRENT binding is the same
        # object as the first resolved one. A module's own same-named
        # object — `cli.LOGGER` is `orbi.cli`'s own logger, not
        # `journal.LOGGER` — must be left alone: rewriting it to the
        # journal object in teardown breaks the CLI's own log records
        # (Issue #1576, the #785 fan-out's one blind spot).
        # `getattr(self, name, None)` keeps an unknown name a no-op (the
        # membership check below then matches no module).
        original = getattr(self, name, None)
        overlaid = [
            (module, original) for module in _MODULES
            if name in vars(module) and vars(module)[name] is original
        ]
        if not overlaid:
            return
        previous = self._previous.setdefault(name, overlaid)
        if all(value is binding for _, binding in previous):
            # Writing back what the overlay replaced is an undo — a test
            # restoring `seam.x = original` by hand — so nothing is left
            # for a later teardown to restore.
            del self._previous[name]
        for module, _ in overlaid:
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        """Undo the overlay of `name` (pytest 9.2.0's undo path).

        pytest 9.2.0 reads a patch's old value from the target's instance
        `__dict__`; the proxy's dict is empty, so its `MonkeyPatch.undo`
        DELETES the attribute instead of restoring it (Issue #1948). The
        overlay lives in the modules, so the delete fans the recorded
        original bindings back. A name with no overlay left — already
        restored, or the seam changed nothing — is a no-op.
        """
        for module, previous in self._previous.pop(name, ()):
            setattr(module, name, previous)


seam = Seam()


def _module_hook(module, name: str):
    """Resolve one delivered hook from its owning module at call time."""
    def call(*args, **kwargs):
        return getattr(module, name)(*args, **kwargs)
    return call


def _runner_hook(name: str):
    """Resolve one runner-owned resume hook at call time."""
    return _module_hook(runner, name)


def resume_deps() -> claim.ResumeHooks:
    """The resume hooks `runner.main` injects into the claim scans.

    `orbi.claim` never imports `orbi.runner` (Article 3.3): the
    delivery-side scene helpers arrive as `ResumeHooks`. A test that calls
    a claim scan directly wires the same callables here; the lookup
    happens per call, so a `monkeypatch.setitem(runner.__dict__, ...)`
    still intercepts the real delivery path. `comment_pr` and
    `block_scene_failure` come from `orbi.failure_report` (Issue #1260):
    `reconcile_orphan_prs` reports through the same failure-report writer
    the delivery failure path uses.
    """
    return claim.ResumeHooks(
        resume_scene=_module_hook(run_state, "resume_scene"),
        route_external_pr_ticket=_runner_hook("_route_external_pr_ticket"),
        block_scene_failure=_module_hook(failure_report, "block_scene_failure"),
        recover_missing_pr_scene=_runner_hook("_recover_missing_pr_scene"),
        has_recoverable_pr_scene=_runner_hook("_has_recoverable_pr_scene"),
        comment_pr=_module_hook(failure_report, "comment_pr"),
    )
