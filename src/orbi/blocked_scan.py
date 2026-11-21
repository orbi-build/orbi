"""One tick's `ai-blocked` read, shared by every scan walking it (#2218).

An `ai-blocked` Issue is terminal for the claim scans, so a tick reads
that state in three places: the missing check-read permission scan
(`orbi.check_permission.recover`), the base-branch-red scan
(`orbi.base_ci_fix.recover`) and the stopped-ticket answer scan for the
same label (`orbi.stopped_scans.run`). Each used to list the label and
read the comments of every blocked Issue, so an idle tick issued the same
`gh issue list --label ai-blocked` and the same
`gh issue view N --json comments` two or three times over.

`blocked_reads` is that read, taken once per repository and tick
(`orbi.tick_reads`): the first scan fills it and the others answer from
it. Sharing is safe because an Issue's newest trusted failure names
exactly ONE reason, so at most one scan acts on an Issue; the others skip
it on the reason they read from the same, still-valid comments. A scan
that DOES act calls `retire`, so a later scan never acts on the label its
predecessor just removed. The trust read (the collaborator permission)
stays inside `orbi.comment_trust`, and the tick pays it once too: `trusted`
keeps the tick's one trust read of an Issue, so the permission check is one
read for the tick's one comments read — unchanged in what it decides.
"""
from __future__ import annotations

import dataclasses

from orbi import comment_trust, tick_reads
from orbi import delivery_labels
from orbi.github import issue_comments, list_issues

# One bounded page per tick: `ai-blocked` Issues are few, and an unbounded
# scan would multiply the per-tick read cost.
SCAN_LIMIT = 200


@dataclasses.dataclass
class BlockedReads:
    """One repository's open `ai-blocked` Issues, and their comments."""

    repo: str
    _issues: list[dict] | None = None
    _comments: dict[int, list[dict]] = dataclasses.field(default_factory=dict)
    _trusted: dict[int, list[dict]] = dataclasses.field(default_factory=dict)

    def blocked_issues(self) -> list[dict]:
        """Every open `ai-blocked` Issue, read once for the tick."""
        if self._issues is None:
            self._issues = list_issues(
                self.repo, state="open", label=delivery_labels.BLOCKED_LABEL,
                json_fields="number,title", limit=SCAN_LIMIT,
            )
        return self._issues

    def comments(self, number: int) -> list[dict]:
        """One blocked Issue's comment history, read once for the tick."""
        if number not in self._comments:
            self._comments[number] = issue_comments(number, repo=self.repo)
        return self._comments[number]

    def trusted(self, number: int) -> list[dict]:
        """One blocked Issue's trusted comments, read once for the tick.

        Trust is the existing comment rule (`orbi.comment_trust`): one
        permission read per author. Reading it here, once per tick and
        per Issue, keeps the permission check as it was — one read for the
        tick's one read of that Issue's comments — instead of one read per
        scan walking the same list (Issue #2218).
        """
        if number not in self._trusted:
            self._trusted[number] = comment_trust.trusted_comments(
                self.comments(number), repo=self.repo,
            )
        return self._trusted[number]

    def retire(self, number: int) -> None:
        """Drop an Issue a scan just acted on (its label changed)."""
        self._trusted.pop(number, None)
        self._comments.pop(number, None)
        if self._issues is not None:
            self._issues = [
                issue for issue in self._issues if issue["number"] != number
            ]


def blocked_reads(repo: str) -> BlockedReads:
    """The tick's ONE `ai-blocked` read for `repo`.

    Outside a tick every caller gets its own reader, which is the
    pre-#2218 behavior of a scan run on its own.
    """
    return tick_reads.once(("blocked", repo), lambda: BlockedReads(repo))
