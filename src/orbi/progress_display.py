"""The display labels and values of Orbi's run comments (Issue #1729).

The progress comment, the started / opened-PR scene blocks and the merged
milestone share these helpers. The stored value is the machine word; an
unknown word renders verbatim, so external tool names and future stages
are never invented into a translation. `run_id=`, `repo_config:` and the
merged receipt's rows have no entry here and stay byte-identical.
"""
from __future__ import annotations

from orbi import language
from orbi.progress_summary import activity_text


# The progress comment's field labels (Issue #1660): the machine fields
# (`run_id=`, the hidden block) stay verbatim; these human labels
# follow the run's message language.
PROGRESS_FIELD_KEYS = {
    "role": "progress.field.role",
    "step": "progress.field.step",
    "reported_at": "progress.field.reported_at",
    "last_activity": "progress.field.last_activity",
    "activity": "progress.field.activity",
    "tests": "progress.field.tests",
    "pr": "progress.field.pr",
    "recovery": "progress.field.recovery",
    "review_round": "progress.field.review_round",
    "issue": "progress.field.issue",
    "priority": "progress.field.priority",
    "phase": "progress.field.phase",
    "elapsed": "progress.field.elapsed",
    "branch": "progress.field.branch",
    "session": "progress.field.session",
    # The started / opened-PR scene rows, the merged milestone rows and
    # the shared result row (Issue #1729). `run_id` and `repo_config`
    # deliberately stay raw: they are public machine rows.
    "base_branch": "progress.field.base_branch",
    "base_sha": "progress.field.base_sha",
    "task_type": "progress.field.task_type",
    "external": "progress.field.external",
    "repo_config_ignored": "progress.field.repo_config_ignored",
    "repo_config_changed": "progress.field.repo_config_changed",
    "repo_config_diff": "progress.field.repo_config_diff",
    "result": "progress.field.result",
    "merge_commit": "progress.field.merge_commit",
    "review_rounds": "progress.field.review_rounds",
    "external_commits": "progress.field.external_commits",
    "commits": "progress.field.commits",
}


def field_label(language_code: str, key: str) -> str:
    """The display label of one field row; an unknown key stays raw."""
    catalog_key = PROGRESS_FIELD_KEYS.get(key)
    return language.text(language_code, catalog_key) if catalog_key else key


def vocabulary(language_code: str, namespace: str, raw: object) -> str:
    """Render one stored display value in the bound language.

    The stored value is the machine word (`review`, `passed`, `normal`);
    an unknown word renders verbatim, so external tool names and future
    stages are never invented into Chinese.
    """
    value = str(raw)
    return language.term(
        language_code, f"progress.{namespace}.{value}", value,
    )


def tests_value(language_code: str, raw: str) -> str:
    """The `tests` row value: a passing state, a pending state, or the
    failing check names (raw external names) behind a translated prefix."""
    prefix = "failed: "
    if raw.startswith(prefix):
        return language.text(
            language_code, "progress.tests.failed",
            names=raw[len(prefix):],
        )
    return vocabulary(language_code, "tests", raw)


def activity_display(language_code: str, activity: dict | None) -> str:
    """The readable activity (`kind target`) in the bound language."""
    text = activity_text(activity)
    if activity is None:
        return text
    rendered = vocabulary(language_code, "activity", activity["kind"])
    target = activity.get("target")
    return f"{rendered} {target}" if target else rendered


def localized_fields(language_code: str,
                     fields: dict[str, object]) -> dict[str, object]:
    """Translate the display labels/values of one field block.

    `repo_config`, `run_id`, the raw sha/branch/session values and every
    other machine row have no catalogue entry and pass through verbatim.
    """
    localized: dict[str, object] = {}
    for key, value in fields.items():
        rendered: object = value
        if key == "priority":
            rendered = vocabulary(language_code, "priority", value)
        elif key == "task_type":
            rendered = vocabulary(language_code, "task_type", value)
        elif key == "external":
            rendered = vocabulary(language_code, "boolean", value)
        localized[field_label(language_code, key)] = rendered
    return localized

