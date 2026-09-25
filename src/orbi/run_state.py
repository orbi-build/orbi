#!/usr/bin/env python3
"""The per-run state file of one task worktree (moved from `runner.py`).

`.orbi/run-state.json` is the explicit "same run" marker: the next tick
matches a worktree on it (Issue #219), the merge record reads its push
history (Issue #833), and the provider-quota wait records its window in
it (Issue #1356) so a deferred run is not re-attempted every tick. This
module owns that file domain — path, read/write, push recording and the
resume scene — and never imports `orbi.runner` (the runner imports this
module, Article 3.3).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from orbi.delivery_scene import RunContext
from orbi.journal import event


def run_state_path(worktree: Path) -> Path:
    """The run state file of one task worktree.

    It lives in the gitignored `.orbi/` directory, so it never
    dirties the commit boundary and never reaches the
    delivery commit.
    """
    return worktree / ".orbi" / "run-state.json"


def write_run_state(ctx: RunContext) -> None:
    """Write (or refresh) the run state file of one task worktree.

    The file is the explicit "same run" marker: the
    worktree directory name alone is not stable across a repo rename
    (the slug changes), but the state file carries the issue number
    and the repo — the identity the next tick matches on. A resumed
    run refreshes the SAME file (same run id): the file is per-run,
    never per-session.

    An existing `pushed_head`/`pushed_base` (Issue #833: the merge
    record's `external_commits` inputs — the last engine-pushed head
    and the foreign head the engine's push line started from) survive
    the refresh: the resume continues the same delivery line, so its
    push history is not the refresh's business to drop.
    """
    state: dict = {
        "run_id": ctx.run_id,
        "issue": ctx.issue,
        "repo": ctx.source_repo,
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = run_state_path(ctx.worktree)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        state.update({
            key: existing[key]
            for key in ("pushed_head", "pushed_base")
            if isinstance(existing.get(key), str) and existing[key]
        })
    _write_run_state_file(ctx.worktree, state)


def _write_run_state_file(worktree: Path, state: dict) -> None:
    path = run_state_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def record_pushed_head(worktree: Path, head: str) -> None:
    """Record `head` as the last engine-pushed head of this delivery.

    The three write points are the Runner's own branch-push evidence:
    the delivery push in `deliver_pr` (the PR-open head), the review
    session's fix push (the re-frozen advanced head), and the
    round-start adoption of a head a previous session pushed but whose
    round ended before the re-freeze record (a findings verdict or a
    malformed verdict head). An external push never passes through any
    of them, which is exactly the distinction the merge record's
    `external_commits` needs. Recording is bypass-safe (Issue #73):
    the fields only feed the merge record, so a missing or corrupt
    state file — a recreated worktree, for instance — logs
    `pushed_head_unrecorded` and continues; the merge record degrades
    to `unknown` and the delivery is never re-failed by its own
    observability input.
    """
    state = _recordable_state(worktree)
    if state is None:
        return
    state["pushed_head"] = head
    _write_run_state_file(worktree, state)


def record_pushed_base(worktree: Path, base: str) -> None:
    """Record `base` as the head the engine's push line started from.

    The one write point is the external-takeover claim (Issue #608):
    the taken-over branch carries the contributor's commits, so the
    merge record's `external_commits` must subtract only what the
    engine pushes on top of this foreign head, never the head itself.
    Set once per run: a re-claim of the same run re-derives a moved
    HEAD (an interrupted delivery), which is not the line's origin.
    Bypass-safe like `record_pushed_head`.
    """
    state = _recordable_state(worktree)
    if state is None:
        return
    if isinstance(state.get("pushed_base"), str) and state["pushed_base"]:
        return
    state["pushed_base"] = base
    _write_run_state_file(worktree, state)


def _recordable_state(worktree: Path) -> dict | None:
    """The run state to record into, or None when recording must skip.

    A missing file is a normal state here (the #90/#50 worktree
    recreation loses `.orbi/`), a corrupt one fails the resume readers
    before any record point can run — neither may fail a push or a
    merge for the sake of the merge record's own input.
    """
    try:
        state = read_run_state(worktree)
    except ValueError as exc:
        state = None
        error = str(exc)
    else:
        error = "the run state file is missing"
    if state is None:
        event(
            "pushed_head_unrecorded", level=logging.WARNING,
            state_path=str(run_state_path(worktree)), error=error,
        )
    return state


# The provider-quota wait (Issue #1356). A Pi session the model
# provider killed because the subscription's usage limit is exhausted is
# a RECOVERABLE wait, not a per-tick resume: the provider will not accept
# a request until its window resets, so re-running Pi on every 5-minute
# tick only burns the subscription and buries the Issue in identical
# comments. The failure records the window in the run state, every tick
# before `until` logs one `provider_quota_wait` line and posts nothing,
# and the first tick after the window starts Pi again (that success is
# the only thing that resumes the delivery). The window is deliberately
# shorter than the hours-long subscription reset: the point is a bounded
# quiet cadence, not a prediction of the provider's reset time.
PROVIDER_QUOTA_WAIT_SECONDS = 1800.0


def record_provider_quota_wait(worktree: Path, *, provider: str | None,
                               model: str | None,
                               now: float | None = None) -> None:
    """Record the active provider-quota wait of this run (Issue #1356).

    The record rides in the existing per-run `run-state.json` marker
    (no second state file): `write_run_state` rewrites the file on the
    next attempt that actually starts Pi, so the wait clears itself
    exactly when the deferral is over. A missing or corrupt state file
    logs `provider_quota_wait_unrecorded` and continues — the wait only
    shapes the retry cadence, it never fails the delivery being
    recorded.
    """
    try:
        state = read_run_state(worktree)
    except ValueError as exc:
        state, error = None, str(exc)
    else:
        error = "the run state file is missing"
    if state is None:
        event(
            "provider_quota_wait_unrecorded", level=logging.WARNING,
            state_path=str(run_state_path(worktree)), error=error,
        )
        return
    state["provider_quota_wait"] = {
        "until": (time.time() if now is None else now)
        + PROVIDER_QUOTA_WAIT_SECONDS,
        "provider": provider or "-",
        "model": model or "-",
    }
    _write_run_state_file(worktree, state)


def provider_quota_wait(worktree: Path,
                        *, now: float | None = None) -> dict | None:
    """The ACTIVE provider-quota wait of this run, or None.

    An absent, corrupt or malformed record is simply no wait (fail
    open): the record is an optimization of the retry cadence, never a
    gate on the delivery.
    """
    try:
        state = read_run_state(worktree)
    except ValueError:
        return None
    wait = state.get("provider_quota_wait") if state else None
    if not isinstance(wait, dict):
        return None
    until = wait.get("until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return None
    if (time.time() if now is None else now) >= float(until):
        return None
    return wait


def _read_pushed(worktree: Path, key: str) -> str | None:
    """One recorded push-history field, or None when unusable.

    The merge record reads these AFTER the merge landed: a missing or
    corrupt record must degrade the metric to `unknown`, never fail a
    landed delivery and never fabricate a count.
    """
    try:
        state = read_run_state(worktree)
    except ValueError:
        return None
    if not state:
        return None
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def read_pushed_head(worktree: Path) -> str | None:
    """The recorded last engine-pushed head, or None when unusable."""
    return _read_pushed(worktree, "pushed_head")


def read_pushed_base(worktree: Path) -> str | None:
    """The recorded engine push-line base, or None when absent.

    None means the engine's first push created the branch, so the
    merge record's engine interval starts at the merge's base parent.
    """
    return _read_pushed(worktree, "pushed_base")


def read_run_state(worktree: Path) -> dict | None:
    """Read the run state file; None when absent, fail fast when corrupt.

    A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run, and a wrong continuation is
    worse than a blocked Issue.
    """
    path = run_state_path(worktree)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"run state file {path} is unreadable: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise ValueError(
            f"run state file {path} must be a JSON object"
        )
    required: dict[str, type] = {
        "run_id": str, "issue": int, "repo": str,
        "branch": str, "worktree": str,
    }
    for key, expected in required.items():
        value = state.get(key)
        if expected is int:
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, expected) and bool(value)
        if not valid:
            raise ValueError(
                f"run state file {path} is malformed: missing or "
                f"invalid field {key!r}"
            )
    return state


def worktree_resume_scene(repo_dir: Path, source_repo: str,
                 number: int) -> tuple[str, Path] | None:
    """Return the resume scene `(run_id, worktree)` for one Issue, or None.

    The worktrees are matched by the RUN STATE FILE, not
    by the directory name alone: the directory name carries the
    source-repo slug, which changes when the repo is renamed, while
    the state file carries the issue number and the repo NAME (the
    part after the slash — stable across a rename). The newest
    matching worktree (by mtime) wins, as before. The
    scene's worktree path may carry the OLD slug (a rename): it is
    the scene the run continues in, never a reason for a second
    worktree.

    A worktree that claims THIS issue number but has a MISSING or
    CORRUPT run state file cannot be verified as the same run: it
    fails fast with the exact reason — never a silent fresh redo.
    A worktree of another issue without a state file
    (a legacy completed run) is unrelated and skipped.
    """
    name = source_repo.rsplit("/", 1)[-1]
    pattern = re.compile(
        r"^orbi-.+-issue-" + str(number) + r"-[0-9a-f]{8}$",
    )
    candidates: list[Path] = []
    worktrees = repo_dir / ".worktrees"
    if worktrees.is_dir():
        for path in worktrees.iterdir():
            if not path.is_dir() or not pattern.match(path.name):
                continue
            try:
                state = read_run_state(path)
            except ValueError as exc:
                raise RuntimeError(
                    f"worktree {path} has a corrupt run state file "
                    f"({exc}): the same run cannot be verified "
                    "(Issue #219)"
                ) from exc
            if state is None:
                raise RuntimeError(
                    f"worktree {path} has no run state file "
                    f"({run_state_path(path)}): the same run cannot "
                    "be verified (Issue #219)"
                )
            if str(state["repo"]).rsplit("/", 1)[-1] != name:
                continue
            candidates.append(path)
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(read_run_state(newest)["run_id"]), newest


def resume_run_id(repo_dir: Path, source_repo: str,
                  number: int) -> str | None:
    """Return the run id to resume for one Issue, or None.

    Delegates to `worktree_resume_scene` (the worktree is matched by its run
    state file, not the directory name).
    """
    scene = worktree_resume_scene(repo_dir, source_repo, number)
    return scene[0] if scene is not None else None
