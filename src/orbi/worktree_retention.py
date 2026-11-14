"""Task-worktree lifecycle: the retention window and terminal retention.

Two host-only settings and the terminal cleanup they control (Issue #2175):

- `worktree_retain_hours` (Issue #760) — how long a CLOSED Issue's scene
  stays inspectable before the tick-start reclamation removes it.
- `keep_failed_worktrees` (Issue #2175, default on) — whether a delivery
  that ends `ai-blocked` keeps its worktree instead of removing it. The
  failed attempt's uncommitted candidate changes, its `.pi-session/`
  session and its `.orbi/` run artifacts are the complete local recovery
  evidence, which a bounded public diagnostic tail cannot replace;
  retention never changes `ai-blocked`, never authorizes a retry and
  never makes the attempt runnable. Cleanup stays the reclamation pass's
  job, so a retained scene of an Issue left open in `ai-blocked` is never
  reclaimed automatically.

The module is a sibling of `runner` (CONSTITUTION Article 3): it owns one
domain, imports the `journal` seam and never imports `runner`.
"""
import math
import shutil
from pathlib import Path

from orbi.delivery_scene import RunContext
from orbi.journal import LOGGER, event, run_command

WORKTREE_RETAIN_HOURS = 72
# ON by default: a failed attempt's evidence stays on disk unless the
# host opts out (a disk-tight host sets `keep_failed_worktrees = false`).
KEEP_FAILED_WORKTREES = True


def load_worktree_retain_hours(data: dict) -> float:
    """Load and validate the optional `worktree_retain_hours` (Issue #760).

    Omitted -> `WORKTREE_RETAIN_HOURS` (default 72 hours): a closed
    Issue's scene stays inspectable for three days before the tick-start
    reclamation removes it. Present -> must be a finite positive number
    (int or float); booleans, zero, negative, NaN/infinity and
    non-numeric values fail fast at config load with the field name and
    the concrete reason.
    """
    value = data.get("worktree_retain_hours", WORKTREE_RETAIN_HOURS)
    if isinstance(value, bool):
        raise ValueError(
            "worktree_retain_hours must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "worktree_retain_hours must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            "worktree_retain_hours must be a finite number of hours "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "worktree_retain_hours must be a positive number of hours "
            f"(got {value!r})"
        )
    return number


def load_keep_failed_worktrees(data: dict) -> bool:
    """Load and validate the optional `keep_failed_worktrees` key.

    Omitted -> `KEEP_FAILED_WORKTREES` (True): the terminal `ai-blocked`
    path keeps the failed attempt's scene. Present -> must be a boolean;
    any other type (the string `"true"`, a `0`/`1` integer, a list)
    fails fast at config load with the field name and the concrete
    reason, never a truthiness guess.
    """
    value = data.get("keep_failed_worktrees", KEEP_FAILED_WORKTREES)
    if not isinstance(value, bool):
        raise ValueError(
            "keep_failed_worktrees must be a boolean "
            f"(got {type(value).__name__} {value!r})"
        )
    return value


def cleanup_task_worktree(ctx: RunContext, repo_dir: Path, *,
                         keep: bool = False) -> None:
    """Remove (or, with `keep`, retain) a terminally failed scene.

    Called ONLY on the terminal `ai-blocked` outcome AFTER the Issue
    evidence (journal line + `Orbi failed` comment) is recorded. The
    recoverable `ai-fix-needed` / model_wait paths keep the worktree for
    the same-run resume and must never call this.

    `keep` is the host's `keep_failed_worktrees` decision: the scene is
    then LEFT IN PLACE on a DETACHED HEAD — detaching frees the stable
    delivery branch for a retry without touching the working tree or its
    uncommitted changes — and the reclamation pass removes it once the
    Issue is closed and `worktree_retain_hours` has passed.

    Without `keep` (the pre-#2175 behavior) the worktree is removed and
    git's metadata pruned. A failure is logged as
    `worktree_cleanup_failed` — never swallowed, never re-raised (the
    tick already handled the delivery failure).
    """
    try:
        if keep and ctx.worktree.is_dir():
            retain_worktree(
                worktree=ctx.worktree, issue=ctx.issue,
                run_id=ctx.run_id, branch=ctx.branch,
            )
            return
        if ctx.worktree.is_dir():
            shutil.rmtree(ctx.worktree)
        run_command(["git", "worktree", "prune"], cwd=repo_dir)
        event(
            "worktree_cleaned", issue=ctx.issue, run_id=ctx.run_id,
            worktree=ctx.worktree,
        )
    except Exception as exc:
        LOGGER.exception(
            "worktree_cleanup_failed issue=%s run_id=%s worktree=%s: %s",
            ctx.issue, ctx.run_id, ctx.worktree, exc,
        )


def retain_worktree(*, worktree: Path, issue: int, run_id: str,
                    branch: str) -> None:
    """Keep a terminally failed scene on disk and log it."""
    run_command(["git", "checkout", "--detach"], cwd=worktree)
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    event(
        "worktree_retained", issue=issue, run_id=run_id,
        worktree=worktree, branch=branch, head=head,
    )
