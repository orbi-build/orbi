"""The release task's terminal comments (Issue #1660).

The auditable success record and the blocked failure scene of a release
task. They live in their own sibling of `release.py` so the localized
wording does not grow the frozen release module (Issue #1229); the release
execution and the closeout import them unchanged (Issue #1859).

The Epic reconciliation sentence and its either-language duplicate check
(Issues #1705, #2356) live in `orbi.epic_reconciliation` since #2356: the
wording now depends on how each child ended, which is that module's domain.
"""
from __future__ import annotations

from orbi import failure, language, terminal_stops
from orbi.progress import field_block
from orbi.run_fields import RunFields


def release_success_comment_body(run_id: str, run_info: RunFields,
                                 release_url: str, tag: str,
                                 release_commit: str,
                                 scope_evidence: list[str],
                                 gate_evidence: list[str],
                                 test_evidence: str,
                                 milestone_evidence: str,
                                 language_code: str,
                                 status_url: str | None = None,
                                 issue: int | None = None,
                                 title: str | None = None,
                                 ) -> str:
    """The terminal success comment: the full verification evidence.

    The auditable record of the release: run marker, release URL,
    version/tag/release commit, the per-item scope evidence, the gate
    evidence, the test evidence and the Milestone evidence. `run_info` is
    the run's fields as values (Issue #2055).
    """
    info = run_info.as_fields()
    return "\n".join([
        field_block(
            run_id,
            language.text(language_code, "release.released_headline",
                          release_url=release_url),
            {**info, "tag": tag, "release_commit": release_commit},
            language_code=language_code, status_url=status_url,
            issue=issue, title=title,
        ),
        "",
        language.text(language_code, "release.section_scope"),
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        language.text(language_code, "release.section_gates"),
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        language.text(language_code, "release.section_tests"),
        "",
        f"- {test_evidence}",
        "",
        language.text(language_code, "release.section_milestone"),
        "",
        f"- {milestone_evidence}",
        "",
        f"run_id={run_id}",
    ])


def release_failure_comment_body(run_id: str, run_info: RunFields,
                                 error: BaseException,
                                 language_code: str,
                                 step: str,
                                 status_url: str | None = None,
                                 issue: int | None = None,
                                 title: str | None = None,
                                 ) -> str:
    """The terminal failure comment: the blocked scene.

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker, the machine-readable
    `orbi:failure:v1` block the delivery path emits (`failure.classify`,
    Issue #1323; `release_failed` when no cause of its own is known), and
    the visible reason names the release `step` that failed (Issue #2222).
    """
    detail = failure.failure_detail(error)
    body = field_block(
        run_id,
        language.text(language_code, "release.failed_headline"),
        {**run_info.as_fields(), "failure": detail},
        language_code=language_code, status_url=status_url,
        issue=issue, title=title,
    ).replace(
        "\n",
        f"\n{failure.render(failure.classify(detail, error=error, release=True))}\n",
        1,
    )
    return terminal_stops.release_failure_reason_line(
        step, detail, body, language_code=language_code,
    )

