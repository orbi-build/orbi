"""The runner-owned run comments and the PR body's footer: the
started-Pi and opened-PR scenes (Issue #1660) and the attribution footer
(Issue #1729).

Both comments double as the run's machine-readable anchors: the started
comment carries the `started` kind marker and the opened-PR comment
carries the `orbi:scene:v1` block the next tick resumes from. Their
human text — the headline, the field labels and the stored display
values — follows the run's bound message language (the explicit
`language_code`); the hidden markers, the public machine rows
(`run_id=`, `repo_config:`) and the scene block stay exactly as they
were.

Extracted from `runner.py` unchanged apart from the language, so the
frozen runner module does not grow (Issue #1229) and a caller keeps
reaching both builders through `orbi.runner` (the names stay imported
there).

It also owns the merged receipt builder (moved here from the size-capped
`review_merge.py`; `review_merge` re-exports it) so every run comment is
rendered beside its siblings (Issue #1826).
"""
from __future__ import annotations

from orbi import language, scene, user_summary
from orbi.comment_kind import (
    COMMENT_KIND_MERGED,
    COMMENT_KIND_STARTED,
    comment_kind_marker,
)
from orbi.delivery_scene import RunContext
from orbi.journal import validate_run_id
from orbi.progress import (
    STARTED_HEADER,
    field_block,
    run_info_fields,
    run_marker,
)
from orbi.progress_display import (
    field_label,
    localized_fields,
    vocabulary,
)


def started_pi_comment_body(ctx: RunContext, run_info: str,
                            extra_fields: dict | None = None, *,
                            language_code: str,
                            status_url: str | None = None,
                            issue_title: str | None = None) -> str:
    """The start comment doubles as the recoverable run scene.

    `extra_fields` carries the repo-config audit fields;
    their values may contain spaces, so they are rendered by the field
    block and never spliced into the space-separated `run_info`. The
    historical English `STARTED_HEADER` stays the reader's fallback for a
    comment written before the kind marker existed.
    """
    fields = run_info_fields(run_info)
    fields["branch"] = str(ctx.branch)
    if extra_fields:
        fields.update(extra_fields)
    info = run_info_fields(run_info)
    rendered_info = {
        key: (vocabulary(language_code, "priority", info[key])
              if key == "priority" else info[key])
        for key in ("run_id", "priority")
        if key in info
    }
    # The headline's key is the same display label the rows use
    # (Issue #1746): `run_id` has no catalogue entry and stays the
    # machine row, `priority` renders `优先级` in a Chinese comment.
    headline = language.text(
        language_code, "delivery.started_headline",
    ) + " " + " ".join(
        f"{field_label(language_code, key)}={value}"
        for key, value in rendered_info.items()
    )
    detail_keys = {"base_sha", "repo_config", "branch", "session"}
    return field_block(
        ctx.run_id, headline, localized_fields(language_code, fields),
        detail_keys={field_label(language_code, key) for key in detail_keys},
        kind=COMMENT_KIND_STARTED,
        language_code=language_code,
        status_url=status_url, issue=ctx.issue, title=issue_title,
    )


def pr_attribution_footer(issue: int, attribution_link: str, *,
                          language_code: str) -> str:
    """The PR body's attribution footer in the bound message language.

    The link URL and `#<issue>` are the machine facts and stay verbatim.
    """
    return language.text(
        language_code, "runner.pr_attribution",
        link=attribution_link, issue=issue,
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str,
                           external: bool = False, *,
                           language_code: str,
                           status_url: str | None = None,
                           issue: int | None = None,
                           issue_title: str | None = None,
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
    fields = run_info_fields(run_info)
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
    detail_keys = {"base_sha", "repo_config", "branch", "session"}
    body = field_block(
        run_id, headline, localized_fields(language_code, fields),
        detail_keys={field_label(language_code, key) for key in detail_keys},
        language_code=language_code,
        status_url=status_url, issue=issue, title=issue_title,
    )
    lines = body.splitlines()
    lines.insert(1, scene.render(scene_record))
    return "\n".join(lines)


def merged_pr_comment_body(run_id: str, pr_url: str, merge_commit: str,
                           review_rounds: int, external_commits: object,
                           commits: object, base_branch: str,
                           review: str,
                           language_code: str,
                           status_url: str | None = None,
                           issue: int | None = None,
                           title: str | None = None,
                           ) -> str:
    """Render the final delivery record.

    The record carries the canonical ``<!-- orbi:comment=merged -->``
    kind right after the run marker (Issue #1659), so a consumer
    identifies it without parsing the translatable `Orbi merged PR:`
    headline. The visible part carries only what the user acts on: the
    PR headline, the review result and the target branch. Every other
    field lives once, in `key=value` form, inside the single `Run
    details` fold — those rows are machine-read anchors (orbi-cloud
    `mergeCommentFields`, the reader contracts), so they keep the `=`
    form while the visible review/merged-into rows use the human `:`.
    The fixed rows are `merge_commit`, `review_rounds`, `commits`,
    `external_commits`, `run_id` and `pr_url`: the PR URL is a
    machine fact, never extracted from the translated heading.
    """
    visible_review = language.text(
        language_code, "delivery.review_row", review=review,
    )
    if int(review_rounds) > 1:
        visible_review += language.text(
            language_code, "delivery.review_rounds",
            rounds=review_rounds,
        )
    details_summary = language.text(language_code, "progress.run_details")
    # The task identity and the optional project status link stay above
    # the receipt rows; nothing renders when neither is known (#1826).
    summary = "\n".join(user_summary.summary_lines(
        status_url=status_url, issue=issue, title=title,
        language_code=language_code,
    )) + "\n" if title or status_url else ""
    return (
        f"{run_marker(run_id)}\n"
        f"{comment_kind_marker(COMMENT_KIND_MERGED)}\n"
        + language.text(
            language_code, "delivery.merged_headline", pr_url=pr_url,
        ) + "\n"
        + summary
        + f"{visible_review}\n"
        + language.text(
            language_code, "delivery.merged_into", base_branch=base_branch,
        ) + "\n\n"
        f"<details><summary>{details_summary}</summary>\n\n"
        f"- merge_commit={merge_commit}\n"
        f"- review_rounds={review_rounds}\n"
        f"- commits={commits}\n"
        f"- external_commits={external_commits}\n"
        f"- run_id={run_id}\n"
        f"- pr_url={pr_url}\n\n"
        "</details>"
    )
