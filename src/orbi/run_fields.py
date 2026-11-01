"""A run's identity fields as values, never as text to split back (#2055).

The runner and the release build the run's `run_id`, `priority`,
`base_branch`, `base_sha`, `task_type` and `repo_config` once, mid-run,
and every run comment, milestone sentence and journal line renders them.
They used to travel as one space-separated `k=v k=v` string that
`progress.run_info_fields` split back on spaces and `=`: a value could
never carry a space, the same string was parsed twice in one function,
and the opened-PR resume record (`orbi:scene:v1`) was read out of a
display string.

`RunFields` carries the fields as values. `text()` is the ONE renderer of
the space-separated form and is called only where a sentence or a journal
line shows it; nothing parses that text again. The field order below is a
rendering contract: the comments and the claim journal line keep their
existing wording (`docs/public-contracts.mdx` publishes the journal
`key=value` format).

The repo-config audit fields (`repo_config_changed` and friends) are not
run fields: their values may carry spaces, so they are rendered as their
own comment fields (`delivery_comment.started_pi_comment_body`
`extra_fields`) and never enter this record.
"""
from __future__ import annotations

import dataclasses

# The ONE display order. An unset field is omitted, which reproduces every
# form in use: the ticket-only claim (`run_id priority task_type`), the dev
# claim (`base_branch base_sha run_id priority [task_type] [repo_config]`)
# and the release (`base_branch [base_sha] run_id priority`).
FIELD_ORDER = (
    "base_branch",
    "base_sha",
    "run_id",
    "priority",
    "task_type",
    "repo_config",
)


@dataclasses.dataclass(frozen=True)
class RunFields:
    """One run's fields, in display order, as values.

    `run_id` and `priority` are always known (they exist from the claim);
    `base_branch`, `base_sha`, `task_type` and `repo_config` are set as the
    run learns them (with `dataclasses.replace`) -- a release freezes its
    base sha after it publishes its started milestone.
    """

    run_id: str
    priority: str
    base_branch: str | None = None
    base_sha: str | None = None
    task_type: str | None = None
    repo_config: str | None = None

    def as_fields(self) -> dict[str, str]:
        """The set fields in display order, for a comment's field block."""
        values = dataclasses.asdict(self)
        return {
            name: values[name] for name in FIELD_ORDER
            if values[name] is not None
        }

    def text(self) -> str:
        """The `k=v k=v` form, for the templates and journal lines that
        display it. Never parsed back into fields."""
        return " ".join(
            f"{name}={value}" for name, value in self.as_fields().items()
        )
