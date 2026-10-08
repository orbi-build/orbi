"""Run state and shared helpers for the delivery and review/merge domains.

The run-state file (`.orbi/run-state.json`), the resume scene, the base
freshness classification and the one-open-PR query are used by both the
delivery machine (`runner.py`) and the review/merge domain
(`review_merge.py`). They live here so neither imports the other
(Article 3.3): `runner` and `review_merge` call them through this module.

Extracted unchanged from the review/merge module into this shared module
(Issue #1263); no behaviour change.
"""
from __future__ import annotations

import json
import logging
import time
from enum import Enum
from pathlib import Path

from orbi import github, scene
from orbi.delivery_scene import RunContext
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    _comment_is_trusted,
    _pr_number,
    pr_view,
    run_gh_read_command,
)
from orbi.gitops import _is_ancestor
from orbi.journal import event


class BaseFreshness(Enum):
    """Classification shared by delivery and merge freshness checks."""

    FRESH = "fresh"
    ABSORBABLE = "absorbable"
    CONFLICTED = "conflicted"


def assess_base_freshness(worktree: Path, base_branch: str, *,
                          head: str = "HEAD",
                          reviewed_head: str | None = None,
                          mergeable: str | None = None) -> BaseFreshness:
    """Classify a delivery head against the fetched base and PR head.

    A moved reviewed head is always conflicted.  Otherwise ancestry is the
    authoritative base decision; a behind head is absorbable unless the
    already-read GitHub mergeability says that absorbing it is conflicted.
    The helper deliberately does not perform a merge or change merge policy.
    """
    if reviewed_head is not None and head != reviewed_head:
        return BaseFreshness.CONFLICTED
    if _is_ancestor(f"origin/{base_branch}", head, cwd=worktree):
        return BaseFreshness.FRESH
    if mergeable is not None and mergeable != "MERGEABLE":
        return BaseFreshness.CONFLICTED
    return BaseFreshness.ABSORBABLE

def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Orbi opened PR:` comment into a resume scene.

    The v1 scene block (orbi.scene) is the machine protocol; the
    human-readable text is the legacy fallback kept for one transition
    version. Returns None when the body is not an
    opened-PR comment. Fails fast when the comment is malformed:
    resuming must recover the exact run (run id, base, PR URL), never a
    guess. Branch and worktree are not parsed: the runner
    derives them from its own config, the Issue number and the run id,
    so a comment can never name an arbitrary local path.
    """
    found = scene.parse(body)
    if found is None:
        return None
    return {
        "run_id": found.run_id,
        "base_branch": found.base_branch,
        "base_sha": found.base_sha,
        "pr_url": found.pr_url,
        "external": found.external,
        # The review-round counter travels with the scene —
        # the round comments carry the updated scene block, so the next
        # resume reads the advanced count instead of re-counting text.
        "review_round": found.review_round,
        # Keep the projection shape of pre-#902 scenes stable when the
        # counter is still zero. A non-zero value is the persisted state
        # needed by the next review session.
        **({"base_advance_round": found.base_advance_round}
           if found.base_advance_round else {}),
        # An unknown verdict head is counted across timer ticks. Preserve
        # the non-zero counter in the projection so the next review cannot
        # silently restart at attempt 1.
        **({"verdict_head_unknown_round": found.verdict_head_unknown_round}
           if found.verdict_head_unknown_round else {}),
    }


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only trusted comments are considered: a comment is trusted when its
    author has maintainer association (OWNER, MAINTAINER, MEMBER or
    COLLABORATOR) or is the account the `gh` credentials resolve to
    (`_comment_is_trusted`, github.py). A public comment can never
    become the recovery scene. The two
    failure shapes stay distinct for the caller:
    `scene.SceneError` when a trusted scene comment is corrupted,
    `scene.SceneMissingError` when no trusted comment carries a scene
    at all. Neither may be guessed at. The projection adds `scene_at`
    (the comment's `createdAt`): the #483 human-recovery budget reset
    compares it against the recovery transition time.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        found = parse_pr_comment(comment.get("body"))
        if found is not None:
            found["scene_at"] = comment.get("createdAt")
            return found
    raise scene.SceneMissingError(
        "no 'Orbi opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )

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


def _query_open_prs(worktree: Path, branch: str) -> list:
    """Return the task branch's open PRs as the raw `gh pr list` list.

    The ONE PR-query contract shared by verify_pr and freeze_pr:
    a single field set, a single ambiguity-guard limit and
    a single parse. The limit is wide enough that the failure evidence
    lists every ambiguous open PR (the resume audit record);
    the "exactly one" decision needs no tighter bound. A non-array
    payload is a broken `gh` response, never "zero PRs" — fail fast
    instead of guessing.
    """
    raw = run_gh_read_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", (
            "number,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner,"
            "isCrossRepository,body"
        ),
        "--limit", "100",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list):
        raise RuntimeError(
            "gh pr list --json returned a non-array payload "
            "(expected exactly one open PR)"
        )
    return github.filter_same_repository_prs(prs, branch)


def _single_open_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    *,
    scene: str,
    external_pr_url: str | None = None,
    source_repo: str | None = None,
) -> dict:
    """Return the one open delivery PR of the task branch, base validated.

    The "exactly one open PR + configured base" decision shared by
    verify_pr and freeze_pr; callers add their own extra
    validations on the returned raw PR dict. `scene` names the calling
    path: the same externally-closed-PR failure used to raise the
    identical sentence from both paths and the log could not tell them
    apart.
    """
    if external_pr_url is not None:
        if source_repo is None:
            raise ValueError("external PR lookup requires source_repo")
        external = pr_view(
            _pr_number(external_pr_url),
            (
                "number,url,state,baseRefName,baseRefOid,headRefName,"
                "headRefOid,headRepository,headRepositoryOwner,body"
            ),
            repo=source_repo, cwd=worktree,
            timeout=RESUME_PR_STATE_TIMEOUT_SECONDS,
        )
        prs = [external] if external.get("state") == "OPEN" else []
    else:
        prs = _query_open_prs(worktree, branch)
    if len(prs) == 0:
        raise RuntimeError(
            f"{scene}: no open PR for the task branch "
            "(expected exactly one open PR)"
        )
    if len(prs) != 1:
        raise RuntimeError(
            f"{scene}: multiple open PRs for the task branch "
            "(expected exactly one open PR)"
        )
    pr = prs[0]
    base_ref = pr.get("baseRefName")
    if base_ref != base_branch:
        event(
            "pr_base_mismatch", level=logging.ERROR, scene=scene,
            expected=base_branch, actual=base_ref, branch=branch,
        )
        raise RuntimeError(
            f"{scene}: PR base is {base_ref}, expected {base_branch}; "
            "recreate the PR against the configured base branch"
        )
    return pr
