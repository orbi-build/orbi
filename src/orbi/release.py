"""Orbi release domain: declaration, scope and pre-release gates.

The declaration/scope/gate half of the deterministic release state
machine the Runner executes for an `ai-release` Issue: the release
contract resolved from the Issue Milestone and its optional overrides,
the per-item scope verification against GitHub, the Milestone-derived
scope, and the two pre-release gates (no in-flight delivery in the
Milestone, green CI on the release commit).

Its GitHub and git data access goes through the `orbi.github` /
`orbi.gitops` leaves and the `orbi.journal` seam. The execution
half lives in `orbi.release_run.process_release` and the tag,
publish, closeout and Milestone steps in `orbi.release_closeout`
(Issue #1859); neither of them is imported here, so this domain never
loads the state machine around it. The Issue-body declaration syntax
itself lives in the leaf `orbi.release_declaration` (Issue #1643).
"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import checks, failure, gh_response
from orbi.delivery_labels import FIX_NEEDED_LABEL, IN_PROGRESS_LABEL, PR_OPENED_LABEL
from orbi.github import (
    commit_check_runs,
    issue_view,
    list_issues,
    list_milestones,
    milestone_issues,
    pr_view,
)
from orbi.gitops import is_ancestor
from orbi.journal import event, run_command
from orbi.release_git import RELEASE_VERSION_FILE_OPTIONS, RELEASE_VERSION_TAG_RE

if TYPE_CHECKING:
    # Annotation-only: this module imports `orbi.runner` never — the
    # runner imports this module at runtime — while
    # `resolve_release_declaration` annotates the frozen host config of
    # #790.
    from orbi.config import RunnerConfig


# --- release-domain constants, scene and gates (moved from
# --- `orbi.runner`: the release contract lives here) --------

# The Pi role of a release run: the delivery state
# machine executes it, never a Pi session.
ROLE_RELEASE = "release"

# Release CI wait: the release commit is born from the last
# delivery PR merge, so its CI is almost always still running when the
# gate checks it — a pending check (queued/in_progress) is an
# intermediate state, not a failure. The gate waits for completion up to
# this limit and decides on the FINAL conclusions; a wait timeout is its
# own failure reason, never reported as a CI failure. The TOML field
# `release_ci_wait_seconds` overrides the default (the #228 pattern).
RELEASE_CI_WAIT_SECONDS = 1800
# Release delivery wait: an early release ticket yields the
# slot while other deliveries finish. The limit applies to one gate attempt;
# the next tick retries the same ready release ticket.
RELEASE_DELIVERIES_WAIT_SECONDS = 1800
# Poll cadence while waiting: one `release_waiting_ci` journal line plus
# one progress-comment PATCH per poll — the same 30s GitHub cadence as
# the live progress heartbeat (PI_HEARTBEAT_SECONDS).
RELEASE_CI_POLL_INTERVAL = 30.0


def resolve_release_declaration(
        issue: dict, overrides: dict, config: RunnerConfig,
        source_repo: str, repo_dir: Path, release_commit: str) -> dict:
    """Build the release contract from GitHub facts and optional overrides.

    The Issue Milestone is the only derived source for the release version and
    scope.  Repository configuration supplies the base branch and the frozen
    tree supplies the version metadata file.  A legacy complete ``## Release``
    declaration still wins field-by-field, but its version must agree with the
    Milestone so a renamed Milestone can never be guessed around.
    """
    milestone = issue.get("milestone")
    milestone_title = milestone.get("title") if isinstance(milestone, dict) else None
    if not isinstance(milestone_title, str) or not milestone_title:
        milestone_title = None
    version = overrides.get("version", milestone_title)
    if version is None:
        raise ValueError(
            "release Issue must have a Milestone; set the Milestone title "
            "to the release version (for example `v0.5.8`)"
        )
    if milestone_title is not None and version != milestone_title:
        raise ValueError(
            f"release version {version!r} does not match the Issue Milestone "
            f"title {milestone_title!r}; rename the Milestone or correct "
            "the `- version:` override"
        )
    if RELEASE_VERSION_TAG_RE.fullmatch(version) is None:
        raise ValueError(
            f"release version {version!r} must be a v-prefixed numeric tag "
            "(for example `v0.5.8`); rename the Milestone or correct the "
            "`- version:` override"
        )

    # config.base_branch is already the fused value: the [[repositories]]
    # entry fallback is applied unconditionally at dispatch, then the
    # repository policy overrides it. Re-reading the raw entry here used
    # to discard the policy layer and freeze the release on the wrong
    # branch.
    base_branch = config.base_branch
    version_file = overrides.get("version_file")
    if version_file is None:
        root_entries = set(run_command(
            ["git", "ls-tree", "--name-only", release_commit], cwd=repo_dir,
        ).splitlines())
        version_file = next(
            (candidate for candidate in RELEASE_VERSION_FILE_OPTIONS
             if candidate != "none" and candidate in root_entries),
            None,
        )
        if version_file is None:
            raise ValueError(
                "release version file could not be detected in the frozen "
                "repository tree; add `- version_file: <supported file>` "
                "or `- version_file: none` to the optional `## Release` "
                "section"
            )

    scope_from_milestone = overrides.get(
        "scope_from_milestone", milestone_title,
    )
    if overrides.get("scope"):
        scope = overrides["scope"]
        scope_from_milestone = None
    else:
        scope = []
        if scope_from_milestone is None:
            raise ValueError(
                "release Issue must have a Milestone or an explicit `scope`"
            )
    return {
        "version": version,
        "base_branch": overrides.get("base_branch", base_branch),
        "scope": scope,
        "scope_from_milestone": scope_from_milestone,
        "version_file": version_file,
    }


def verify_release_version_file(repo_dir: Path, release_commit: str,
                                version_file: str) -> None:
    """Prove the declared `version_file` exists in the frozen base.

    `version_file` is an optional declaration field that silently defaults
    to the Python ecosystem's `pyproject.toml`; a repository without that
    file used to discover the mismatch only at the version-write step —
    after the gates and the scope verification had already run — and
    burned the ticket `ai-blocked` with a bare FileNotFoundError
    (orbi-cloud#246). The check probes the ROOT of the frozen release
    commit's tree (`git ls-tree --name-only` — the exact content the
    version write will see) and runs at claim time, before any gate wait.
    A mismatch fails fast with an actionable message: the supported
    ecosystem files that DO exist at the repository root (the value to
    declare), and `none` for a repository with no version metadata file
    at all.
    """
    if version_file == "none":
        return
    root_entries = set(run_command(
        ["git", "ls-tree", "--name-only", release_commit],
        cwd=repo_dir,
    ).splitlines())
    if version_file in root_entries:
        return
    existing = [
        name for name in RELEASE_VERSION_FILE_OPTIONS
        if name != "none" and name in root_entries
    ]
    raise RuntimeError(
        f"release version_file {version_file!r} does not exist at the "
        f"root of the release tree {release_commit} — supported files "
        f"present at the repository root: "
        f"{', '.join(existing) if existing else '(none)'}; declare one "
        "of those as `- version_file: <file>`, or `- version_file: "
        "none` to skip version metadata changes"
    )


def verify_release_scope(repo: str, scope: list[int], repo_dir: Path,
                         release_commit: str) -> list[str]:
    """Verify every release scope item ONE BY ONE.

    The scope is verified against GitHub, never by parsing Issue-body
    checkboxes: each number is probed as a PR first (`gh pr view` —
    which exits 1 with the real "Could not resolve to a PullRequest"
    error when the number is an Issue, verified against the live CLI)
    and, failing that, as an Issue (`gh issue view`). A PR must be
    `MERGED` and its merge commit must be an ancestor of the frozen
    release commit; an Issue must be `CLOSED` with `stateReason`
    `COMPLETED`: a `NOT_PLANNED` closure (duplicate /
    won't fix) is not a delivery — its evidence line visibly annotates
    the exclusion instead of silently counting the ticket into the
    release. This ancestor check proves
    the scoped PR is actually contained in the tag, rather than merely
    having been merged into some other branch. An item that is neither, a
    PR that is not merged/in the release base, or an Issue that is not
    closed fails fast with the concrete number and state. A real `gh` or
    git failure (auth, rate limit, missing object) is re-raised, never
    misread as "not a PR".
    """
    evidence: list[str] = []
    for number in scope:
        try:
            pr = pr_view(number, "number,state,mergeCommit", repo=repo)
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to a PullRequest" not in (exc.stderr or ""):
                raise
            pr = None
        if pr is not None:
            state = pr.get("state")
            if state != "MERGED":
                raise RuntimeError(
                    f"release scope PR #{number} is not merged "
                    f"(state={state})"
                )
            merge_commit = (pr.get("mergeCommit") or {}).get("oid")
            if not merge_commit:
                raise RuntimeError(
                    f"release scope PR #{number} is merged but has no "
                    "merge commit evidence"
                )
            if not is_ancestor(merge_commit, release_commit, cwd=repo_dir):
                raise RuntimeError(
                    f"release scope PR #{number} merge commit {merge_commit} "
                    f"is not contained in release commit {release_commit}"
                )
            evidence.append(
                f"PR #{number} merged (mergeCommit={merge_commit})"
            )
            continue
        try:
            issue = issue_view(number, "number,state,stateReason", repo=repo)
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to an Issue" not in (exc.stderr or ""):
                raise
            raise RuntimeError(
                f"release scope item #{number} is neither a PR nor an "
                "Issue"
            ) from exc
        state = issue.get("state")
        if state != "CLOSED":
            raise RuntimeError(
                f"release scope Issue #{number} is not closed "
                f"(state={state})"
            )
        if issue.get("stateReason") == "NOT_PLANNED":
            # A NOT_PLANNED closure (duplicate / won't fix)
            # is not released work — annotate the exclusion visibly
            # instead of silently counting the ticket into the release.
            evidence.append(
                f"Issue #{number} closed (not planned, excluded)"
            )
        else:
            evidence.append(f"Issue #{number} closed")
    return evidence


def derive_release_scope_from_milestone(
        repo: str, milestone_title: str,
        release_issue: int | None = None) -> tuple[list[int], list[str]]:
    """Derive the release scope from a Milestone.

    The release scope is the Milestone's COMPLETED Issues under the
    Milestone whose title is EXACTLY `milestone_title` (the same
    exact-title rule as `close_release_milestone` — never guessed,
    never fuzzy-matched, never a different Milestone). The Milestone
    decides whether a release may run (gates and scope evidence), not
    what its notes say: the Changelog is derived from the tagged commit
    range by `orbi.release_notes` (Issue #1492), so a PR merged after
    the tag never reaches the notes and a PR in the tag that is not on
    the Milestone still does.

    - no Milestone with that exact title -> fail fast;
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - open Issues are NEVER part of the scope (unfinished work is a
      human decision point) but are returned as a separate evidence list
      so the release run surfaces them instead of swallowing them;
    - `release_issue` (the driving release Issue's own number) is
      exempt from that open-item evidence: it is necessarily still
      open while the release runs (it closes after the notes are
      published), so listing it would put a self-referential "NOT
      released" line on an immutable Release page — the same exemption
      the #663 completeness gate applies.

    Returns (scope numbers sorted ascending, open-item evidence
    strings). A real `gh` failure (auth, rate limit, API error)
    propagates unchanged — a scope that cannot be derived is a failed
    release, never a guessed one.
    """
    milestones = list_milestones(repo)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == milestone_title
    ]
    if not matches:
        raise RuntimeError(
            f"release milestone derivation: no Milestone with the exact "
            f"title {milestone_title!r} in {repo} — never guessed or "
            "fuzzy-matched"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"release milestone derivation: {len(matches)} Milestones "
            f"share the exact title {milestone_title!r} in {repo} — "
            "ambiguous, refusing to guess which to derive from"
        )
    number = matches[0].get("number")

    def issues(state: str) -> list[dict]:
        return milestone_issues(repo, int(number), state)

    closed_issues = [item for item in issues("closed")
                     if "pull_request" not in item]
    open_issues = [item for item in issues("open")
                   if "pull_request" not in item]

    scope = sorted(
        int(item["number"])
        for item in closed_issues
        if isinstance(item.get("number"), int)
    )
    open_evidence = [
        f"open Issue #{item.get('number')} {item.get('title')}"
        for item in open_issues
        if item.get("number") != release_issue
    ]
    return scope, open_evidence


def check_release_gates(repo: str, release_commit: str,
                        release_number: int, *,
                        milestone: str | None = None,
                        ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS,
                        delivery_wait_seconds: float = RELEASE_DELIVERIES_WAIT_SECONDS,
                        delivery_waited_seconds: float = 0.0,
                        on_wait: Callable[[str], None] | None = None,
                        on_delivery_wait: Callable[[str], None] | None = None,
                        repo_has_ci: bool = False,
                        ) -> tuple[list[str], bool]:
    """Enforce the pre-release gates and return their evidence.

    Returns ``(evidence, repo_has_ci)`` — the second element is whether
    any check run was observed on the gated commit, so the release flow
    can feed gate 1's observation (on the frozen base) into gate 2 (on
    the just-pushed version commit) as ``repo_has_ci``.

    Two gates, each checked against GitHub (never against local
    state), each failure raising with the concrete offender:

    1. No open Issue still carries `ai-in-progress`, `ai-pr-opened`
       or `ai-fix-needed` — the release Issue itself is excluded (it
       carries `ai-in-progress` while the release runs). With a
       `milestone`, the check is scoped to that Milestone (the documented `gh issue list --milestone <title>` filter), the
       same criterion as the #663 completeness gate: an in-flight Issue
       of another Milestone — or of no Milestone — is not this
       release's delivery and never blocks it. Without a `milestone`
       there is nothing to scope by and the pre-#671 repository-wide
       scan is unchanged.
    2. CI on the release commit is green: every check run reported by
       the GitHub API for the commit is `completed` with a
       `success`/`neutral`/`skipped` conclusion (a failing, cancelled
       or error check fails the gate; no check runs at all is recorded
       as such, not invented — EXCEPT when the caller passes
       ``repo_has_ci=True``, the frozen base already showed checks, so
       this repository runs CI and an empty list on a freshly pushed
       commit means the checks have not REGISTERED yet (GitHub creates
       CheckRuns seconds after the push): the gate then
       polls within the same budget until the first check appears and
       never passes an empty list as "nothing to gate"). A PENDING
       check (queued/in_progress —
       the release commit is born from the last delivery merge, so its
       CI is almost always still running) is not a
       conclusion: the gate polls until every check completes (one
       `release_waiting_ci` journal line per poll, the wait reflected
       through `on_wait`), then decides on the final conclusions.
       Waiting past `ci_wait_seconds` fails with its own timeout
       reason, explicitly distinct from a CI failure. The rule is
       `checks.classify_checks` (Issue #1889), shared with the merge gate.

    Open PRs are deliberately NOT a gate: an open PR is queue state, never a release
    premise. The release contract is the milestone's closed-Issue scope
    check, green full tests and green CI on the release commit —
    whether open PRs exist, how many, or who authored them says nothing
    about the release. A stranded PR is the delivery loop's takeover /
    reconciliation job, not the release gate's.

    A real `gh` failure (auth, rate limit, API error) propagates
    unchanged — a gate that cannot be checked is a failed gate. The one
    exception is GitHub's HTTP 403 **with quota left** for the check-runs
    query (read from the response status, never from the output text): it
    is reported as a missing Checks:read credential permission so the
    blocked release is actionable. A rate-limited 403 (no quota left) is
    a transient GitHub failure and propagates unchanged, so it requeues
    instead of asking for a credential repair (Issue #1768). A wait budget
    that runs out raises `failure.ReleaseGateWaitTimeout`, which the
    blocked comment reports as a human decision rather than a transient
    failure (Issue #1760).
    """
    evidence: list[str] = []
    open_deliveries: set[int] = set()
    for label in (IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL):
        # The leftover-delivery check is scoped to the
        # release's Milestone, so an unrelated in-flight Issue can
        # no longer block the release indefinitely.
        issues = list_issues(
            repo, label=label, state="open", milestone=milestone,
            json_fields="number", limit=50,
        )
        for item in issues:
            number = int(item["number"])
            if number != release_number:
                open_deliveries.add(number)
    if open_deliveries:
        numbers = sorted(open_deliveries)
        detail = ", ".join(f"Issue #{number}" for number in numbers)
        event(
            "release_waiting_deliveries", issue=release_number,
            open=numbers, waited=f"{int(delivery_waited_seconds)}s",
            limit=f"{int(delivery_wait_seconds)}s",
        )
        if on_delivery_wait is not None:
            on_delivery_wait(
                f"{detail}; waited {int(delivery_waited_seconds)}s / "
                f"{int(delivery_wait_seconds)}s"
            )
        if delivery_waited_seconds >= delivery_wait_seconds:
            # A spent wait budget is a terminal, human-decided stop — its
            # own type, so the classification never reads the message and
            # never calls it a transient GitHub failure (Issue #1760).
            raise failure.ReleaseGateWaitTimeout(
                f"release gate: waiting for open deliveries {numbers} "
                f"timed out after {int(delivery_wait_seconds)}s "
                "— delivery wait timeout, not a CI failure"
            )
        raise ReleaseDeliveriesWaiting(
            numbers, delivery_waited_seconds, delivery_wait_seconds,
        )
    scope = f" in milestone {milestone!r}" if milestone else ""
    evidence.append(
        f"no open Issue{scope} carries "
        f"{IN_PROGRESS_LABEL} / {PR_OPENED_LABEL} / {FIX_NEEDED_LABEL}"
    )
    def fetch_check_runs() -> list[dict]:
        try:
            return commit_check_runs(repo, release_commit)
        except subprocess.CalledProcessError as error:
            # The response status — not the output text — tells a missing
            # Checks:read permission from every other read failure
            # (Issue #1760). A rate-limited 403 is GitHub's own limit, not
            # a permission: it propagates unchanged and classifies as the
            # transient failure that requeues (Issue #1768).
            if (gh_response.status_of(error) == 403
                    and not gh_response.transient(error)):
                # Its own type: the status meaning travels on the type
                # while the reader-facing sentence names the missing
                # scope, so classification never reads the words
                # (Issue #1760).
                raise failure.GitHubPermissionDenied(
                    "CI gate could not be evaluated: the credential lacks "
                    "Checks:read permission (GitHub returned HTTP 403 while "
                    "listing check runs)"
                ) from error
            raise

    check_runs = fetch_check_runs()
    waited = 0.0
    while True:
        # One pending/failed/passed rule for both GitHub shapes
        # (Issue #1889), shared with the merge gate.
        pending, failed, _passed = checks.classify_checks(check_runs)
        if repo_has_ci and not check_runs:
            # An empty list on a just-pushed commit in a CI
            # repository is "not registered yet", not "nothing to gate"
            # — wait for the first check within the same budget; a
            # timeout below fails with the wait-timeout reason.
            detail = (
                "the first check run to register on the just-pushed "
                f"commit {release_commit} (GitHub creates CheckRuns "
                "seconds after the push)"
            )
        elif pending:
            detail = ", ".join(
                f"check '{check['name']}' is {check['status']}/"
                f"{check['conclusion']}" for check in pending
            )
        else:
            break
        event(
            "release_waiting_ci", issue=release_number, commit=release_commit,
            pending=detail, waited=f"{int(waited)}s",
            limit=f"{int(ci_wait_seconds)}s",
        )
        if on_wait is not None:
            on_wait(
                f"{detail}; waited {int(waited)}s / "
                f"{int(ci_wait_seconds)}s"
            )
        if waited >= ci_wait_seconds:
            raise failure.ReleaseGateWaitTimeout(
                f"release gate: waiting for CI on the release commit "
                f"{release_commit} timed out after "
                f"{int(ci_wait_seconds)}s (still pending: {detail}) "
                "— wait timeout, not a CI failure"
            )
        step = min(RELEASE_CI_POLL_INTERVAL, ci_wait_seconds - waited)
        time.sleep(step)
        waited += step
        check_runs = fetch_check_runs()
    if failed:
        check = failed[0]
        raise RuntimeError(
            f"release gate: CI check '{check['name']}' is "
            f"{check['status']}/{check['conclusion']} "
            f"on the release commit {release_commit}"
        )
    if check_runs:
        evidence.append(
            f"CI on the release commit: {len(check_runs)} check(s) all "
            "success/neutral/skipped"
            + (f" (waited {int(waited)}s for pending checks)" if waited
               else "")
        )
    else:
        evidence.append(
            f"CI on the release commit: no check runs on "
            f"{release_commit} (nothing to gate)"
        )
    # No open-PR gate — an open PR is queue state, not a
    # release premise (see the docstring for the maintainer ruling).
    return evidence, bool(check_runs)


class ReleaseDeliveriesWaiting(RuntimeError):
    """Gate 1 found deliveries still in flight; retry next tick."""

    def __init__(self, issue_numbers: list[int], waited: float, limit: float):
        self.issue_numbers = issue_numbers
        self.waited = waited
        self.limit = limit
        super().__init__(
            "release gate: waiting for open deliveries "
            f"{issue_numbers} (waited {int(waited)}s / {int(limit)}s)"
        )


def release_target_milestone(
    issue: dict, active_milestone: str | None = None,
) -> str | None:
    """Return the Milestone a release Issue releases.

    The completeness gate judges the Milestone the release belongs to:
    the Issue's own GitHub Milestone is authoritative, and the configured
    `active_milestone` is the fallback (the release fallback scan is
    already scoped to it). Without any Milestone there is nothing to
    check and the release keeps the pre-#663 behavior, so the function
    returns None and the caller skips the gate.
    """
    milestone = issue.get("milestone")
    if isinstance(milestone, dict):
        title = milestone.get("title")
        if isinstance(title, str) and title:
            return title
    if isinstance(active_milestone, str) and active_milestone:
        return active_milestone
    return None
