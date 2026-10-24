"""The release task's terminal comments (Issue #1660).

The auditable success record and the blocked failure scene of a
release task. They live in their own sibling of `release.py` so the
localized wording does not grow the frozen release module (Issue
#1229); the release execution (`orbi.release_run`) and the tag/publish
closeout (`orbi.release_closeout`) import them unchanged (Issue #1859).

It also owns the Epic reconciliation audit sentence and its
either-language duplicate check (Issue #1705): the per-tick claim sweep
and the release Milestone sweep share one renderer here, so neither
frozen module grows for the catalog text.
"""
from __future__ import annotations

from orbi import failure, language
from orbi.progress import field_block, run_info_fields


def _epic_audit(child_evidence: list[str], version: str | None,
                language_code: str) -> str:
    """The one Epic reconciliation sentence, in the bound language.

    The child fragments ("PR #N merged", "Issue #N closed") are the
    shared evidence `github._epic_child_evidence` builds; only the
    sentence around them is translated (Issue #1705).
    """
    prefix = "" if not version else language.text(
        language_code, "epic.reconciliation_version", version=version,
    )
    return language.text(
        language_code, "epic.reconciliation",
        prefix=prefix, children=", ".join(child_evidence),
    )


def epic_audit_posted(comments: list[dict], child_evidence: list[str],
                      version: str | None = None) -> bool:
    """True when a comment already carries the audit in EITHER language.

    The audit sentence is its own duplicate check: both sweeps close the
    Epic right after commenting, so a comment without a close means the
    close failed and the retry may render in another language. Matching
    every rendering keeps today's English audit from being duplicated by
    a Chinese one (Issue #1705).
    """
    renderings = [
        _epic_audit(child_evidence, version, language_code)
        for language_code in language.LANGUAGES
    ]
    return any(
        rendering in str(comment.get("body", ""))
        for comment in comments
        for rendering in renderings
    )


def release_success_comment_body(run_id: str, run_info: str,
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

     The comment is the auditable record of the release:
    run marker, release URL, version/tag/release commit, the
    per-item scope evidence, the gate evidence, the test evidence and
    the Milestone evidence (the Milestone whose title is the released
    version is closed on the success path).
    """
    info = run_info_fields(run_info)
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


def release_failure_comment_body(run_id: str, run_info: str,
                                 error: BaseException,
                                 language_code: str,
                                 status_url: str | None = None,
                                 issue: int | None = None,
                                 title: str | None = None,
                                 ) -> str:
    """The terminal failure comment: the blocked scene.

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker, the same
    machine-readable `orbi:failure:v1` block the delivery path emits
    (`failure.classify`, Issue #1323), and the concrete reason.
    """
    detail = failure.failure_detail(error)
    body = field_block(
        run_id,
        language.text(language_code, "release.failed_headline"),
        {**run_info_fields(run_info), "failure": detail},
        language_code=language_code, status_url=status_url,
        issue=issue, title=title,
    )
    return body.replace(
        "\n",
        f"\n{failure.render(failure.classify(detail, error=error))}\n",
        1,
    )

