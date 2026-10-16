#!/usr/bin/env python3
"""Scoped orbi-status observation (Issue #1656).

Implements the settled status observation contract in
docs/public-contracts.mdx:

- --repo OWNER/REPO and --issue N select the scope; --follow only
  changes how long the command observes. Invalid selectors are
  rejected before any data read.
- The observation key is the configured repository plus the Issue
  number. Candidate worktrees come only from the existing full naming
  convention .worktrees/orbi-<owner>-<repo>-issue-<N>-<run_id>; the
  worktree name attributes the run, a complete JSONL header attributes
  the session and a complete activity record attributes the activity
  time. Directory/file mtime is never used.
- The active set comes from a complete GitHub scope read; local
  worktrees only attach observations to those Issues. A task that
  disappears from the set is read directly and shown with its
  confirmed state.
- Local discovery runs on a 0.5-second cycle and renders
  immediately; every GitHub-backed read (active set, ready queue,
  recent results, frozen base) runs on the 15-second GitHub cycle —
  one bounded background read that never blocks the local
  presentation and never overlaps itself. A read failure preserves
  the last view, names the scope and exits non-zero.

No database, queue or daemon: GitHub Issues/labels stay the only state
store, the JSONL files are read in place and nothing is persisted.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
)
from orbi.github import issue_view, parse_paginated_issue_array, run_gh_read_command
from orbi.pi_activity import activity_kind_for, read_new_events
from orbi.progress_summary import safe_target as shared_safe_target

# The two read cycles of the contract (seconds).
LOCAL_CYCLE_SECONDS = 0.5
GITHUB_CYCLE_SECONDS = 15.0

# The Issue fields a delivery state needs (both gh JSON shapes).
ISSUE_FIELDS = "number,title,state,labels,url"

# Delivery states that count as an active delivery in the scope view.
ACTIVE_DELIVERY_LABELS = (
    IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL, AWAITING_MERGE_LABEL,
)

# The run id carried by a task worktree name is the legal eight-hex id.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")

# The activity kinds, as the progress contract publishes them. The
# tool -> kind mapping is NOT repeated here: the progress whitelist
# (`pi_activity.ACTIVITY_KIND_BY_TOOL`, read through `activity_kind_for`)
# stays the single source for every reader of the same JSONL records.
KIND_READ = "read"
KIND_EDIT = "edit"
KIND_ANALYSIS = "analysis"

# The read failures that end observation with the scope named: a
# bounded gh read retry already ran inside the reader, and the same
# family the other gh-reading CLI commands collapse (a hung gh raises
# TimeoutExpired, a missing gh raises OSError, a malformed payload
# raises ValueError).
READ_ERRORS = (subprocess.SubprocessError, OSError, ValueError)


class StatusSelectorError(ValueError):
    """An invalid --repo/--issue selector, rejected before any data read."""


@dataclass(frozen=True)
class StatusScope:
    """The repositories and the optional single Issue the view observes."""

    repos: tuple[str, ...]
    issue: int | None = None


def parse_scope(source_repos: Sequence[str], repo: str | None = None,
                issue: int | None = None) -> StatusScope:
    """Validate the selectors and return the observation scope.

    Raises StatusSelectorError for an unknown repository, a
    non-positive Issue number, or Issue-only selection while several
    repositories are configured. All validation happens here, before
    any local or GitHub read.
    """
    repos = tuple(source_repos)
    if repo is not None:
        if repo not in repos:
            raise StatusSelectorError(
                f"unknown repository {repo!r}; configured: {', '.join(repos)}"
            )
        repos = (repo,)
    if issue is not None:
        if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
            raise StatusSelectorError(
                f"invalid issue number {issue!r}; expected a positive integer"
            )
        if len(repos) != 1:
            raise StatusSelectorError(
                "--issue requires --repo when several repositories are "
                "configured"
            )
    return StatusScope(repos=repos, issue=issue)


@dataclass(frozen=True)
class Delivery:
    """One GitHub Issue reduced to the state the status view shows."""

    repo: str
    number: int
    title: str
    url: str
    state: str
    labels: tuple[str, ...]


def _label_names(labels: object) -> tuple[str, ...]:
    """Normalize gh/REST label objects to their names (order preserved)."""
    if not isinstance(labels, list):
        return ()
    names = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return tuple(names)


def delivery_from_issue(repo: str, issue: object) -> Delivery:
    """Map one raw gh/REST Issue object to a Delivery."""
    if not isinstance(issue, dict):
        raise ValueError(f"issue record is not an object for {repo}: {issue!r}")
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError(f"issue record has no numeric number for {repo}")
    title = issue.get("title")
    url = issue.get("url") or issue.get("html_url")
    state = issue.get("state")
    return Delivery(
        repo=repo,
        number=number,
        title=title if isinstance(title, str) else "",
        url=url if isinstance(url, str) else "",
        state=state.lower() if isinstance(state, str) else "",
        labels=_label_names(issue.get("labels")),
    )


def is_active(delivery: Delivery) -> bool:
    """True while the Issue carries one of the active delivery states.

    A confirmed manual wait wins over local observations (Issue #1669):
    when a maintainer adds `ai-blocked` mid-delivery, the merge gate
    leaves the delivery label in place but the Issue is not running, so
    it must not be listed as active. This mirrors the engine's own
    decision order (`delivery_scene.classify` checks BLOCKED before every
    opened-PR state) instead of inventing a second rule.
    """
    if BLOCKED_LABEL in delivery.labels:
        return False
    return any(label in ACTIVE_DELIVERY_LABELS for label in delivery.labels)


def open_issues_complete(repo: str) -> list[dict]:
    """Read every open Issue of one repository (complete pagination)."""
    raw = run_gh_read_command([
        "gh", "api",
        f"repos/{repo}/issues?state=open&per_page=100",
        "--paginate", "--slurp",
    ], timeout=30)
    return parse_paginated_issue_array(raw)


def read_active_deliveries(
    repo: str, *,
    read_issues: Callable[[str], list] = open_issues_complete,
) -> list[Delivery]:
    """Return every active delivery of one repository, ordered by number.

    Complete pagination through the injected reader; pull requests are
    excluded and the state/label semantics match the existing Orbi
    state rules.
    """
    deliveries = []
    for item in read_issues(repo):
        delivery = delivery_from_issue(repo, item)
        if item.get("pull_request") is not None:
            continue
        if delivery.state == "closed" or not is_active(delivery):
            continue
        deliveries.append(delivery)
    deliveries.sort(key=lambda delivery: delivery.number)
    return deliveries


def confirmed_delivery(repo: str, number: int, *,
                       read_issue: Callable[..., dict] = issue_view) -> Delivery:
    """Read one Issue directly and return its confirmed state."""
    return delivery_from_issue(
        repo, read_issue(number, ISSUE_FIELDS, repo=repo),
    )


@dataclass(frozen=True)
class Worktree:
    """One task worktree attributed by the full naming convention."""

    repo: str
    issue: int
    run_id: str
    path: Path


def worktree_attributions(repo_dir: Path, source_repo: str,
                          issue: int | None = None) -> list[Worktree]:
    """Enumerate candidate task worktrees for one repository.

    Only the existing full naming convention is accepted; a directory
    that does not match contributes nothing. The Issue number matches
    exactly (42 never matches 420).
    """
    base = Path(repo_dir) / ".worktrees"
    if not base.is_dir():
        return []
    prefix = f"orbi-{source_repo.replace('/', '-')}-issue-"
    found = []
    for path in base.iterdir():
        if not path.is_dir():
            continue
        remainder = (
            path.name[len(prefix):] if path.name.startswith(prefix) else None
        )
        if remainder is None:
            continue
        number_text, separator, run_id = remainder.partition("-")
        if not separator or not number_text.isdigit():
            continue
        if not RUN_ID_PATTERN.match(run_id):
            continue
        number = int(number_text)
        if issue is not None and number != issue:
            continue
        found.append(Worktree(source_repo, number, run_id, path))
    found.sort(key=lambda tree: (tree.issue, tree.run_id))
    return found


def session_files(worktree: Path) -> list[Path]:
    """Return the worktree's complete session JSONL files, name-ordered."""
    directory = worktree / ".pi-session"
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.name,
    )


def safe_target(worktree: Path, raw: object, *,
                exclude_paths: frozenset[str] = frozenset()) -> str | None:
    """Return a repository-relative safe activity target, or null.

    The shared path policy, not a second copy of it: the containment
    rule, the internal `RUNNER_RUNTIME_EXCLUDES` paths, `.git`
    metadata, the `<`/`>` and control characters and the length cap
    all come from `progress_summary.safe_target`, the single
    implementation the published activity uses. This function keeps the
    stricter rule that `.git` metadata nested in a subdirectory is
    null as well, and adds the configured credential/provider file; the
    known activity kind and time are kept by the caller.
    """
    if isinstance(raw, str):
        raw = raw.strip()
    target = shared_safe_target(worktree, raw)
    if target is None or ".git" in Path(target).parts:
        return None
    if os.path.abspath(os.path.join(str(worktree), target)) in exclude_paths:
        return None
    return target


def _item_activity(item: dict) -> tuple[str, str | None] | None:
    """Classify one assistant content item, or None when it carries none.

    A tool call goes through the progress contract's own whitelist and
    read/edit path keys (`pi_activity.activity_kind_for`): a tool
    outside it publishes no activity at all. Text and thinking publish
    `analysis`, exactly as the published activity does.
    """
    item_type = item.get("type")
    if item_type == "toolCall":
        name = item.get("name")
        arguments = item.get("arguments")
        return activity_kind_for(
            name if isinstance(name, str) else "",
            arguments if isinstance(arguments, dict) else {},
        )
    if item_type in ("text", "thinking"):
        return (KIND_ANALYSIS, None)
    return None


def activity_of(record: dict) -> tuple[str, str | None] | None:
    """Return (kind, raw target) for a complete activity record, or None.

    The newest recognized content item decides, exactly as the progress
    writer folds the same record, so the local observation and the
    published activity carry the same kind and target for one event. An
    unknown tool call carries no activity and is skipped.
    """
    if record.get("type") != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    activity = None
    for item in content:
        if not isinstance(item, dict):
            continue
        classified = _item_activity(item)
        if classified is not None and classified[0] is not None:
            activity = classified
    return activity


@dataclass(frozen=True)
class Observation:
    """One attributed local observation of a delivery."""

    run_id: str
    session_id: str | None
    session_file: Path | None
    at: str | None
    kind: str | None
    target: str | None


@dataclass
class _SessionState:
    """The accumulated attributed state of one session JSONL file."""

    offset: int = 0
    session_id: str | None = None
    at: str | None = None
    kind: str | None = None
    target: str | None = None


def _apply_record(record: dict, state: _SessionState, worktree: Path,
                  exclude_paths: frozenset[str]) -> None:
    """Fold one complete JSONL record into the session state."""
    if record.get("type") == "session":
        session_id = record.get("id")
        if isinstance(session_id, str) and session_id:
            state.session_id = session_id
        return
    activity = activity_of(record)
    if activity is None:
        return
    kind, raw_target = activity
    state.kind = kind
    state.target = (
        safe_target(worktree, raw_target, exclude_paths=exclude_paths)
        if kind in (KIND_READ, KIND_EDIT) else None
    )
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        state.at = timestamp


class LocalObserver:
    """Attribute local worktree/session activity for active Issues.

    Per-file offsets live in process memory, so follow reads only new
    complete records and a snapshot reads the last complete observation
    from each candidate session. Incomplete or unattributable records
    mean "no readable activity", never "idle".
    """

    def __init__(self, repo_dir: Path, *,
                 exclude_paths: Iterable[str] = ()) -> None:
        self.repo_dir = Path(repo_dir)
        self.exclude_paths = frozenset(exclude_paths)
        self._states: dict[Path, _SessionState] = {}

    def observations(self, source_repo: str,
                     numbers: Iterable[int]) -> dict[int, list[Observation]]:
        """Return the attributed observations for the given Issue numbers."""
        result = {}
        for number in sorted(set(numbers)):
            entries = self._issue_observations(source_repo, number)
            if entries:
                result[number] = entries
        return result

    def _issue_observations(self, source_repo: str,
                            number: int) -> list[Observation]:
        entries = []
        for tree in worktree_attributions(self.repo_dir, source_repo, number):
            files = session_files(tree.path)
            if not files:
                entries.append(Observation(
                    tree.run_id, None, None, None, None, None,
                ))
                continue
            for path in files:
                state = self._states.setdefault(path, _SessionState())
                records, offset = read_new_events(path, state.offset)
                state.offset = offset
                for record in records:
                    _apply_record(record, state, tree.path, self.exclude_paths)
                entries.append(Observation(
                    tree.run_id, state.session_id, path, state.at,
                    state.kind, state.target,
                ))
        entries.sort(key=lambda entry: (
            entry.at or "", entry.run_id, str(entry.session_file or ""),
        ))
        return entries


def format_delivery(delivery: Delivery) -> str:
    """Render a delivery: number, title, url, state and labels."""
    text = " ".join(
        part for part in (f"#{delivery.number}", delivery.title, delivery.url)
        if part
    )
    if delivery.state:
        text += f" state={delivery.state}"
    if delivery.labels:
        text += f" labels={','.join(delivery.labels)}"
    return text


def observation_lines(observations: Sequence[Observation]) -> list[str]:
    """Render the attributed observation lines for one delivery."""
    if not observations:
        return ["    observation: none"]
    lines = []
    for entry in observations:
        if entry.session_id is None and entry.at is None and entry.kind is None:
            lines.append(
                f"    observation: run={entry.run_id} no readable activity"
            )
            continue
        lines.append(
            "    observation: "
            f"run={entry.run_id} session={entry.session_id or '-'} "
            f"at={entry.at or '-'} kind={entry.kind or '-'} "
            f"target={entry.target if entry.target is not None else '-'}"
        )
    return lines


class StatusObserver:
    """Hold the scoped GitHub state and the local attribution.

    refresh performs one complete GitHub read cycle (never overlapping,
    because the command is single-threaded) and computes the ended
    transitions; local provides the 0.5-second local attribution. due
    decides the 15-second GitHub cycle.
    """

    def __init__(self, config, scope: StatusScope, *,
                 read_issues: Callable[[str], list] = open_issues_complete,
                 read_issue: Callable[..., dict] = issue_view,
                 read_ready: Callable[[str], dict | None] | None = None,
                 read_result: Callable[[str], dict | None] | None = None,
                 read_base: Callable[[Path, str], str] | None = None,
                 exclude_paths: Iterable[str] = (),
                 now: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.scope = scope
        self._read_issues = read_issues
        self._read_issue = read_issue
        self._read_ready = read_ready
        self._read_result = read_result
        self._read_base = read_base
        self._now = now
        # The credential/provider file the existing configuration
        # already resolved is excluded from activity targets like the
        # runtime paths (docs/public-contracts.mdx, path policy).
        excluded = tuple(exclude_paths)
        if config.pi_providers is not None:
            excluded += (str(config.pi_providers),)
        self.local = LocalObserver(config.repo_dir, exclude_paths=excluded)
        self.deliveries: dict[str, list[Delivery]] = {}
        self.confirmed: dict[str, Delivery] = {}
        # The GitHub-backed queue, result and frozen base of each
        # repository, read on the GitHub cycle (never by a local render).
        self.base: dict[str, str] = {}
        self.ready: dict[str, dict | None] = {}
        self.result: dict[str, dict | None] = {}
        self.ended: list[str] = []
        self._previous: dict[str, set[int]] = {
            repo: set() for repo in scope.repos
        }
        self._next_github = 0.0
        self._read_thread: threading.Thread | None = None
        self._read_error: BaseException | None = None

    def due(self, timestamp: float | None = None) -> bool:
        """True when the next GitHub read cycle is due."""
        current = self._now() if timestamp is None else timestamp
        return current >= self._next_github

    def refresh(self) -> None:
        """Run one complete GitHub read cycle for the whole scope.

        The active set, the ready queue, the recent results and the
        frozen base all belong to this cycle: the 0.5-second local cycle
        renders the last complete reads and never re-reads GitHub. The
        15-second cycle restarts when this read COMPLETED, so a slow read
        never shortens the interval to the next one.
        """
        ended = []
        for repo in self.scope.repos:
            if self._read_base is not None:
                self.base[repo] = self._read_base(
                    self.config.repo_dir, self.config.base_branch,
                )
            if self._read_ready is not None:
                self.ready[repo] = self._read_ready(repo)
            if self._read_result is not None:
                self.result[repo] = self._read_result(repo)
            if self.scope.issue is not None:
                self.confirmed[repo] = self._read_confirmed(
                    repo, self.scope.issue,
                )
                continue
            active = read_active_deliveries(repo, read_issues=self._read_issues)
            numbers = {delivery.number for delivery in active}
            for number in sorted(self._previous[repo] - numbers):
                ended.append(
                    "  ended: " + format_delivery(
                        self._read_confirmed(repo, number)
                    )
                )
            self._previous[repo] = numbers
            self.deliveries[repo] = active
        self.ended = ended
        self._next_github = self._now() + GITHUB_CYCLE_SECONDS

    def reading(self) -> bool:
        """True while a background GitHub read cycle is in flight."""
        thread = self._read_thread
        return thread is not None and thread.is_alive()

    def start_read(self) -> bool:
        """Start one bounded background GitHub read; no-op while one runs.

        GitHub state never blocks the local presentation: the caller
        keeps rendering the last complete reads on the 0.5-second cycle
        while this thread refreshes them, and state-read cycles never
        overlap — a due cycle is skipped while the previous one runs.
        """
        if self.reading():
            return False
        self._read_error = None
        self._read_thread = threading.Thread(target=self._run_read, daemon=True)
        self._read_thread.start()
        return True

    def _run_read(self) -> None:
        """Run one read cycle, keeping its failure for the foreground."""
        try:
            self.refresh()
        except READ_ERRORS as exc:
            self._read_error = exc

    def read_error(self) -> BaseException | None:
        """The finished background read's failure, consumed once."""
        error, self._read_error = self._read_error, None
        return error

    def _read_confirmed(self, repo: str, number: int) -> Delivery:
        return confirmed_delivery(repo, number, read_issue=self._read_issue)

    def observations(self, source_repo: str,
                     numbers: Iterable[int]) -> dict[int, list[Observation]]:
        """Local attribution for the current render."""
        return self.local.observations(source_repo, numbers)


def scope_label(scope: StatusScope) -> str:
    """Short scope text used in the failure line."""
    issue = f"#{scope.issue}" if scope.issue is not None else ""
    return ",".join(f"{repo}{issue}" for repo in scope.repos)


def follow(observer: StatusObserver, render: Callable[[], str], *,
           sleep: Callable[[float], None] = time.sleep,
           now: Callable[[], float] = time.monotonic,
           max_cycles: int | None = None,
           on_error: Callable[[BaseException, str | None], None] | None = None,
           ) -> int:
    """Follow the scope until Ctrl-C or max_cycles.

    Every cycle renders the current view from the last complete reads
    (the local 0.5-second cycle); a due GitHub read runs as one bounded
    background read, so GitHub never blocks the local presentation and
    two state-read cycles never overlap. A read failure — the background
    GitHub cycle or the rendered view — calls on_error with the last
    successful view and returns non-zero without scheduling another
    cycle.
    """
    last_view = None
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        error = observer.read_error()
        if error is None:
            if observer.due(now()):
                observer.start_read()
            try:
                last_view = render()
            except READ_ERRORS as exc:
                # A partial read failure ends observation: the last
                # successful view is preserved, the scope is named by
                # the handler and no next cycle is scheduled.
                error = exc
            else:
                print(last_view)
        if error is not None:
            if on_error is not None:
                on_error(error, last_view)
            return 1
        sleep(LOCAL_CYCLE_SECONDS)
    return 0
