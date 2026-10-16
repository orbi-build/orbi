"""The release task's terminal comments (Issue #1660).

The auditable success record and the blocked failure scene of a
release task. They live in their own sibling of `release.py` so the
localized wording does not grow the frozen release module (Issue
#1229); `release.py` imports them unchanged.
"""
from __future__ import annotations

from orbi import failure, language
from orbi.progress import _run_info_fields, field_block


def release_success_comment_body(run_id: str, run_info: str,
                                 release_url: str, tag: str,
                                 release_commit: str,
                                 scope_evidence: list[str],
                                 gate_evidence: list[str],
                                 test_evidence: str,
                                 milestone_evidence: str,
                                 language_code: str = language.DEFAULT_LANGUAGE,
                                 ) -> str:
    """The terminal success comment: the full verification evidence.

     The comment is the auditable record of the release:
    run marker, release URL, version/tag/release commit, the
    per-item scope evidence, the gate evidence, the test evidence and
    the Milestone evidence (the Milestone whose title is the released
    version is closed on the success path).
    """
    info = _run_info_fields(run_info)
    return "\n".join([
        field_block(
            run_id,
            language.text(language_code, "release.released_headline",
                          release_url=release_url),
            {**info, "tag": tag, "release_commit": release_commit},
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
                                 language_code: str = language.DEFAULT_LANGUAGE,
                                 ) -> str:
    """The terminal failure comment: the blocked scene.

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker, the same
    machine-readable `orbi:failure:v1` block the delivery path emits
    (`failure.classify`, Issue #1323), and the concrete reason.
    """
    detail = failure._failure_detail(error)
    body = field_block(
        run_id,
        language.text(language_code, "release.failed_headline"),
        {**_run_info_fields(run_info), "failure": detail},
    )
    return body.replace("\n", f"\n{failure.render(failure.classify(detail))}\n", 1)

