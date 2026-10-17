"""The runner-owned run comments: the started-Pi and opened-PR scenes
(Issue #1660).

Both comments double as the run's machine-readable anchors: the started
comment carries the `started` kind marker and the opened-PR comment
carries the `orbi:scene:v1` block the next tick resumes from. Only their
HEADLINE is human prose and follows the run's bound message language
(the explicit `language_code`); the hidden
markers, the field lines and the scene block stay exactly as they were.

Extracted from `runner.py` unchanged apart from the language, so the
frozen runner module does not grow (Issue #1229) and a caller keeps
reaching both builders through `orbi.runner` (the names stay imported
there).
"""
from __future__ import annotations

from orbi import language, scene
from orbi.comment_kind import COMMENT_KIND_STARTED
from orbi.delivery_scene import RunContext
from orbi.journal import validate_run_id
from orbi.progress import STARTED_HEADER, _run_info_fields, field_block


def started_pi_comment_body(ctx: RunContext, run_info: str,
                            extra_fields: dict | None = None, *,
                            language_code: str) -> str:
    """The start comment doubles as the recoverable run scene.

    `extra_fields` carries the repo-config audit fields;
    their values may contain spaces, so they are rendered by the field
    block and never spliced into the space-separated `run_info`. The
    historical English `STARTED_HEADER` stays the reader's fallback for a
    comment written before the kind marker existed.
    """
    fields = _run_info_fields(run_info)
    fields["branch"] = str(ctx.branch)
    fields["worktree"] = str(ctx.worktree)
    if extra_fields:
        fields.update(extra_fields)
    info = _run_info_fields(run_info)
    headline = language.text(
        language_code, "delivery.started_headline",
    ) + " " + " ".join(
        f"{key}={info[key]}" for key in ("run_id", "priority")
        if key in info
    )
    return field_block(
        ctx.run_id, headline, fields,
        detail_keys={"base_sha", "repo_config", "branch", "worktree", "session"},
        kind=COMMENT_KIND_STARTED,
        language_code=language_code,
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str,
                           external: bool = False, *,
                           language_code: str,
                           ) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR. The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id -- a comment must never be able to name a local path. An
    external takeover marks the scene `external`: the PR is
    the contributor's own (no run marker, no `Fixes` keyword in its
    body), and the delivery branch is the PR's head branch -- derived
    from the takeover worktree, never from the comment.

    The machine-readable record is the single hidden `orbi:scene:v1`
    block rendered from the `Scene`; the field lines below
    the headline stay for humans only.
    """
    fields = _run_info_fields(run_info)
    if external:
        fields["external"] = "true"
    headline = language.text(
        language_code, "delivery.opened_pr_headline",
    ) + " " + pr_url
    scene_record = scene.Scene(
        run_id=validate_run_id(run_id),
        base_branch=fields["base_branch"],
        base_sha=fields["base_sha"],
        pr_url=pr_url,
        external="true" if external else "",
    )
    body = field_block(
        run_id, headline, fields,
        detail_keys={"base_sha", "repo_config", "branch", "worktree", "session"},
        language_code=language_code,
    )
    lines = body.splitlines()
    lines.insert(1, scene.render(scene_record))
    return "\n".join(lines)
