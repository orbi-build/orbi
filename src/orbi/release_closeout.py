"""Orbi release publication and closeout (Issue #1859).

The irreversible tail of a release run, extracted verbatim from the
1520-line `orbi.release`: the annotated tag primitives, the GitHub
Release publish, the Milestone close, the Epic reconciliation sweep and
the terminal success closeout (the `ai-merged` transition, the Issue
close, the Milestone evidence and the success comment).

The module owns no orchestration: `orbi.release_run.process_release`
calls every step here, in the same order it always did. It imports
neither `orbi.release` (the declaration/scope/gate domain) nor
`orbi.runner`, so the closeout wording and the tag/publish contracts
can change without loading the state machine around them.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from orbi import language
from orbi.blocked_comment import post_blocked_comment
from orbi.delivery_labels import EPIC_LABEL, EVENT_MERGED, IN_PROGRESS_LABEL
from orbi.github import (
    apply_label_patch,
    close_issue,
    close_milestone,
    comment_issue,
    issue_comments,
    list_milestones,
    milestone_open_issues,
    verify_epic_complete,
)
from orbi.gitops import acquire_base_sync_lock, is_ancestor
from orbi.journal import LOGGER, run_command, run_git_network_command
from orbi.progress import run_marker
from orbi.release_comments import (
    _epic_audit,
    epic_audit_posted,
    release_failure_comment_body,
    release_success_comment_body,
)
from orbi.release_git import (
    release_create,
    release_edit_notes,
    release_view,
    run_git_write,
)


def release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return the commit the tag points to on the remote, or None.

     `git ls-remote` keeps the existence probe read-only and
    its exit semantics unambiguous: exit 0 with empty output means the
    remote has no such tag (None); ANY non-zero exit is a real failure
    (network/auth) and propagates. The previous `git fetch` probe read
    exit 128 as "missing", but a network failure also exits 128 — a
    transient outage would read as "no tag on the remote", the retry
    would re-create the tag, and a local residue from the failed push
    deadlocked the release ticket. Annotated tags are
    peeled with the `^{}` line ls-remote reports alongside the tag
    object.

    The base-sync lock is kept: `ls-remote` does not write refs, but
    the sibling steps around it do, and the lock orders them against
    task worktrees sharing the checkout's common dir.
    """
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        output = run_git_network_command(
            ["git", "ls-remote", "origin",
             f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            cwd=repo_dir,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    refs: dict[str, str] = {}
    for line in output.splitlines():
        oid, _, ref = line.partition("\t")
        refs[ref.strip()] = oid.strip()
    peeled = refs.get(f"refs/tags/{tag}^{{}}")
    if peeled:
        return peeled
    return refs.get(f"refs/tags/{tag}")


def local_release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return a local tag's peeled commit, or None when it is absent."""
    try:
        return run_command(
            ["git", "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}^{{commit}}"], cwd=repo_dir,
        ).strip()
    except subprocess.CalledProcessError:
        return None


def ensure_release_tag_created(repo_dir: Path, tag: str,
                               release_commit: str) -> None:
    """Create the annotated release tag locally, idempotently."""
    local_tag_commit = local_release_tag_commit(repo_dir, tag)
    if local_tag_commit is None:
        run_git_write(
            ["git", "tag", "-a", tag, "-m", f"Release {tag}",
             release_commit], cwd=repo_dir,
        )
    elif local_tag_commit != release_commit:
        raise RuntimeError(
            f"local tag {tag} already exists and points at "
            f"{local_tag_commit}, not the release commit {release_commit} "
            "— an existing tag is never moved or overwritten"
        )


def ensure_release_tag_pushed(repo_dir: Path, tag: str,
                              release_commit: str) -> None:
    """Create the annotated release tag and push it — idempotently."""
    ensure_release_tag_created(repo_dir, tag, release_commit)
    run_git_network_command(
        ["git", "push", "origin", f"refs/tags/{tag}"], cwd=repo_dir,
    )


def tag_commit_is_ancestor_of_base(tag_commit: str, base_commit: str,
                                   repo_dir: Path) -> bool:
    """True when the tag commit is an ancestor of the base commit — the
    resume case where an existing tag is behind an advanced base."""
    return is_ancestor(tag_commit, base_commit, cwd=repo_dir)


def publish_release(*, repo: str, tag: str, version: str,
                    release_commit: str, changelog: str,
                    scope_evidence: list[str], gate_evidence: list[str], test_evidence: str,
                    run_id: str, issue_number: int,
                    attribution_footer: bool = True,
                    attribution_link: str = "https://github.com/orbi-build/orbi",
                    ) -> str:
    """Create the GitHub Release for the tag — idempotently.

    When a Release for the tag already exists (a restart after a
    successful `gh release create`) its URL is reused, never a second
    Release is created. A legacy existing Release without this changelog
    is upgraded in place; a Release that already contains it is unchanged.
    Otherwise the Release is created from the EXISTING tag (the tag was
    created and pushed by the caller first — `gh release create` never
    creates or moves the tag itself) with notes carrying the full
    verification evidence: version, tag,
    release commit, the per-item scope evidence, the gate evidence, the
    test evidence and the run marker (the same stable machine-readable
    marker every run comment carries).
    """
    notes = "\n".join([
        f"# {version}",
        "",
        changelog,
        "",
        f"- tag: `{tag}`",
        f"- release commit: `{release_commit}`",
        f"- release task: Issue #{issue_number}",
        "",
        "## Scope (verified item by item)",
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        "## Pre-release gates",
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        "## Tests",
        "",
        f"- {test_evidence}",
        "",
        run_marker(run_id),
        f"run_id={run_id}",
        *(["", f"Released by [Orbi]({attribution_link})"]
          if attribution_footer else []),
    ])
    try:
        release = release_view(repo, tag, fields="tagName,url,body")
        if changelog not in release.get("body", ""):
            release_edit_notes(repo, tag, notes=notes)
        return release["url"]
    except subprocess.CalledProcessError as exc:
        if "not found" not in (exc.stderr or ""):
            raise
    release_create(repo, tag=tag, version=version, notes=notes)
    return release_view(repo, tag, fields="tagName,url")["url"]


# The GitHub issue index is eventually consistent —
# `gh issue close` returning success does not mean the
# `issues?milestone=N&state=open` list reflects it yet, and v0.4.10 read
# a stale non-empty list in the same second as the close. A non-empty
# gate read is re-checked with these backoff delays first; only a list
# that stays non-empty across all of them fails fast (bounded ≈ 10 s
# including the reads — a real unfinished issue must not drag the
# release out).
MILESTONE_OPEN_RETRY_DELAYS = (1.0, 2.0, 4.0)


def close_release_milestone(repo: str, version: str, *, run_id: str | None = None,
                            release_issue: int | None = None,
                            language_code: str = language.DEFAULT_LANGUAGE) -> str:
    """Close the Milestone whose title is exactly `version`.

    Runs on the release success path (after the tag is pushed, the
    GitHub Release is published and the release Issue is closed with
    `ai-merged`). The Milestone is matched by EXACT title — never
    guessed, never fuzzy-matched, never a different Milestone:

    - no Milestone with that exact title -> fail fast (the release
      must not be reported as fully successful);
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - already `closed` -> idempotent success (no mutation, no reopen);
    - `open` with open issues -> fail fast with the version, the
      Milestone number/url and the open issue list — but only after
      bounded backoff re-reads (the issue index is
      eventually consistent, so a list read in the same second as the
      release Issue's `gh issue close` can still show it as open);
    - `open` with 0 open issues -> closed via the official REST
      contract `PATCH /repos/{owner}/{repo}/milestones/{number}`
      with `state=closed` (OpenAPI `issues/update-milestone`).

    `release_issue` is THIS release's own ticket number.
    The gate never counts it as unfinished work: it is being closed by
    this very release, so a stale-open listing of exactly that ticket
    is the known index lag, and waiting out the backoff for
    it would make the milestone close depend on the index refresh
    timing. Every gate read drops it before the 0-open-issues
    judgment; a refusal then names only the REAL leftovers and
    annotates the exclusion. Without `release_issue` the gate is
    unchanged.

    The list query asks for `state=all`: the default `state=open`
    would hide already-closed Milestones and break the idempotent
    case. Returns a short evidence string for the success path. A
    real `gh` failure (auth, rate limit, API error) propagates
    unchanged — like the release gates, a check that cannot be made
    is a failed check.
    """
    milestones = list_milestones(repo)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == version
    ]
    if not matches:
        raise RuntimeError(
            f"release {version}: no Milestone with the exact title "
            f"{version!r} in {repo} — the Milestone is missing, never "
            "guessed or fuzzy-matched"
        )
    if len(matches) > 1:
        numbers = ", ".join(
            f"#{m.get('number')} ({m.get('html_url') or m.get('url')})"
            for m in matches
        )
        raise RuntimeError(
            f"release {version}: {len(matches)} Milestones share the "
            f"exact title {version!r} in {repo} — ambiguous, refusing "
            f"to guess which to close: {numbers}"
        )
    milestone = matches[0]
    number = milestone.get("number")
    html_url = milestone.get("html_url") or milestone.get("url")
    if milestone.get("state") == "closed":
        return (
            f"Milestone #{number} ({html_url}) already closed — "
            "idempotent success, nothing to do"
        )
    excluded_tickets: list[int] = []

    def open_leftovers() -> list[dict]:
        # The gate judges the milestone's REAL unfinished
        # work — never this release's own ticket.
        issues = milestone_open_issues(repo, int(number))
        if release_issue is None:
            return issues
        leftovers = [
            item for item in issues
            if item.get("number") != release_issue
        ]
        if len(leftovers) != len(issues):
            excluded_tickets.append(release_issue)
        return leftovers

    open_issues = open_leftovers()
    epic_evidence: list[str] = []
    if run_id is not None and open_issues:
        # Verify every listed Epic before the exact-Milestone list is re-read.
        epic_evidence = reconcile_release_epics(
            repo, int(number), version, run_id, language_code)
        open_issues = open_leftovers()
    retries = 0
    while open_issues and retries < len(MILESTONE_OPEN_RETRY_DELAYS):
        # The release Issue close succeeded seconds ago; a non-empty read
        # here is more likely the index lagging than real
        # unfinished work. Re-read with bounded backoff; the raise below
        # fires only once the list stays non-empty across all retries.
        time.sleep(MILESTONE_OPEN_RETRY_DELAYS[retries])
        retries += 1
        open_issues = open_leftovers()
    if open_issues:
        listing = ", ".join(
            f"#{i.get('number')} {i.get('title')}" for i in open_issues
        )
        excluded = (
            f" (this release's own ticket #{release_issue} is excluded "
            "from this gate — it is being closed by this release)"
            if release_issue is not None else ""
        )
        raise RuntimeError(
            f"release {version}: Milestone #{number} ({html_url}) still "
            f"has {len(open_issues)} open issue(s) — closing it would hide "
            f"unfinished work; open issues: {listing}{excluded}"
        )
    close_milestone(repo, int(number))
    epic_suffix = f"; {'; '.join(epic_evidence)}" if epic_evidence else ""
    excluded_suffix = (
        f"; release ticket #{release_issue} excluded — it is being "
        "closed by this release"
        if excluded_tickets else ""
    )
    return (
        f"Milestone #{number} ({html_url}) closed after release "
        f"{version} (0 open issues){epic_suffix}{excluded_suffix}"
    )


def release_success_closeout(
        *, repo: str, number: int, run_id: str, run_info: str, tag: str,
        release_commit: str, release_url: str, scope_evidence: list[str],
        gate_evidence: list[str], test_evidence: str, ready_label: str,
        language_code: str, status_url: str | None, title: str,
) -> None:
    """Record the published release: labels, Issue, Milestone, comment.

    Runs on the release success path after the tag is pushed and the
    GitHub Release is published. Every step below is a bypass of that
    already-irreversible fact: a failure is logged and can never rewrite
    the published result as `ai-blocked` (the same rule the milestone
    evidence states explicitly in its failure sentence).
    """
    try:
        # A release ticket is claimed through `label:ai-ready` and the
        # claim keeps the ready queue entry, so the real labels at
        # this point are `{ai-ready, ai-in-progress}`. The merged
        # event clears both, so the released Issue ends
        # `ai-merged` ALONE — never `ai-merged` plus a stale
        # `ai-ready` that reads as "still queued" (Issue #1526).
        apply_label_patch(
            number, repo=repo, event=EVENT_MERGED,
            current_labels={ready_label, IN_PROGRESS_LABEL},
            ready_label=ready_label,
        )
        close_issue(int(number), repo=repo)
    except Exception:
        # The tag and GitHub Release are already published at this
        # point. The ai-merged transition and the Issue close are
        # bookkeeping of that irreversible fact — a transient failure
        # here must not fall through to the generic handler and
        # rewrite the published result as ai-blocked (a
        # bypass — never a terminal rewrite; same rule as the
        # milestone evidence below).
        LOGGER.exception(
            "issue=%s release_publish_closeout_failed", number,
        )
    try:
        milestone_evidence = close_release_milestone(
            repo, tag, run_id=run_id, release_issue=int(number),
            language_code=language_code,
        )
    except Exception as exc:
        # The tag and GitHub Release are already published at this point.
        # Milestone closure is evidence only and must not rewrite that
        # irreversible release result as ai-blocked. The
        # wording states plainly that the third release criterion was
        # missed — the milestone was NOT closed.
        LOGGER.exception(
            "issue=%s release_milestone_evidence_failed", number,
        )
        milestone_evidence = (
            "milestone NOT closed: " + str(exc)
        )
    try:
        comment_issue(
            number, repo=repo,
            body=release_success_comment_body(
                run_id, run_info, release_url, tag, release_commit,
                scope_evidence, gate_evidence, test_evidence,
                milestone_evidence,
                language_code=language_code,
                status_url=status_url, issue=number, title=title,
            ),
        )
    except Exception:
        # Same bypass rule: the success comment is evidence of the
        # already-published release — its failure is logged and must
        # not rewrite the published result as ai-blocked.
        LOGGER.exception(
            "issue=%s release_success_comment_failed", number,
        )


def release_failure_closeout(
        *, repo: str, number: int, run_id: str, run_info: str,
        error: Exception, ready_label: str, language_code: str,
        status_url: str | None, title: str,
) -> None:
    """Record a failed release: the `ai-blocked` terminal and its comment.

    The failure comment is the human-facing record the blocked ticket
    carries, and the label transition clears the in-flight label AND the
    resolved queue label in one write (Issue #1775).
    """
    LOGGER.exception("issue=%s release_failed", number)
    post_blocked_comment(
        number, repo=repo,
        body=release_failure_comment_body(
            run_id, run_info, error,
            language_code=language_code,
            status_url=status_url, issue=number, title=title,
        ),
        # A failed release is a blocked terminal: the claimed
        # ticket still carries the resolved queue label, so it is
        # cleared with the in-flight label (Issue #1775).
        current_labels={IN_PROGRESS_LABEL, ready_label},
        language_code=language_code, ready_label=ready_label,
    )


def reconcile_release_epics(repo: str, milestone_number: int, version: str,
                            run_id: str, language_code: str) -> list[str]:
    """Close only provably complete open Epics in this exact Milestone."""
    issues = milestone_open_issues(repo, milestone_number)
    evidence: list[str] = []
    for listed_epic in issues:
        labels = listed_epic.get("labels", [])
        names = {label.get("name") for label in labels if isinstance(label, dict)}
        if EPIC_LABEL not in names:
            continue
        number = listed_epic.get("number")
        try:
            child_evidence = verify_epic_complete(repo, listed_epic)
        except (ValueError, json.JSONDecodeError) as exc:
            evidence.append(f"Epic #{number} kept open: {exc}")
            continue
        audit = _epic_audit(child_evidence, version, language_code)
        comments = issue_comments(int(number), repo=repo)
        if not epic_audit_posted(comments, child_evidence, version):
            comment_issue(int(number), repo=repo,
                         body=f"<!-- orbi:run={run_id} -->\n{audit}\nrun_id={run_id}")
        close_issue(int(number), repo=repo)
        evidence.append(f"Epic #{number} closed after verification ({'; '.join(child_evidence)})")
    return evidence
