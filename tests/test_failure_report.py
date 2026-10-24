"""The `orbi.failure_report` module boundary (Issue #1260, Articles 3.2/3.3).

Failure reporting is one module: `orbi.runner` reaches it through
`orbi.failure_report` and re-exports none of the moved definitions, and
the module imports nothing from `orbi.runner` back (an extraction that
re-exported or imported back would let a stale patch silently stop
intercepting).

The extraction must change no behaviour at all, so the goldens below were
captured by calling the PRE-move `runner.report_delivery_failure` on the
three rendering modes: the implement-phase terminal template
(`classify=False`), the classified recoverable Issue+PR body
(`classify=True, evidence=True` — the bounded `stderr_tail` /
`stdout_tail` / `session_last_events` / `test_log_tail` segments) and the
terminal progress scene (`finish=True`, rendered through
`progress.finish_progress_body`). Each golden is asserted byte for byte
against the post-move call, and the AI/human decision (`blocked` vs
`fix needed`) with it.
"""
import ast
import json
import logging
import re
import subprocess
from pathlib import Path

import pytest

from orbi import (
    failure_report, language, progress, runner, runner_health, scene,
)
from tests.fakes.github import FakeGh
from tests.fakes.progress import freeze_report_time

from seam import seam

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"


@pytest.fixture(autouse=True)
def _repository_permissions(monkeypatch):
    """Issue #1806: the history these reports read is written by
    repository maintainers (`maintainer`); a non-writer names an
    outsider, and the runner's own account is a pinned bot.
    """
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: (
            "none" if login in {"drive-by", "stranger", "outsider"}
            else "admin"
        ),
    )
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "orbi-bot[bot]",
    )


# The failure-reporting definitions moved out of `runner.py` (Issue #1260).
MOVED = (
    "report_delivery_failure",
    "_session_summary",
    "block_scene_failure",
    "_failure_evidence",
    "block_repo_config_failure",
    "_failure_streak",
    "_report_resume_failure",
    "is_unrecoverable_failure",
    "_raise_if_preexisting_ci_failure",
    "_tail_text",
    "_reported_failure_comment",
    "_is_line_failure_comment",
    "_latest_session_file",
    "GateCIFailure",
    "PreExistingCIFailure",
    "_classify_failure",
    "UnrecoverableDeliveryError",
    "ReviewRoundsExhausted",
    "HumanDecisionRequired",
    "_SNAPSHOT_PLACEHOLDER",
    "_failure_scene",
    "_snapshot_or_placeholder",
    "FAILURE_COMMENT_MAX_CHARS",
    "FAILURE_STREAK_LIMIT",
    "SESSION_SUMMARY_LIMIT",
    # Issue #1660 turned the fixed phrase into a catalog key (still
    # owned by this module) so the sentence follows the message language.
    "_BLOCKED_PRECONDITION_PHRASE_KEY",
    "_SGR_RE",
    "comment_pr",
)
# `comment_pr` moved here instead of `github.py`: the frozen size ratchet
# (github.py at 1146 lines against its 1162 ceiling) forbids the +58 lines,
# and the Issue names this module as the fallback. Issue #1659 moved the
# review-round accounting into the `orbi.review_rounds` sibling (it grew
# past the 1,000-line cap here); this module imports and re-exports
# `review_rounds_so_far` for its existing callers. The names below are the
# only moved ones the runner still uses directly, so it imports them back by
# name; everything else it reaches as `failure_report.<name>` and the moved
# list above must not be re-exported.
RUNNER_IMPORTS = (
    "_classify_failure",
    "GateCIFailure",
    "HumanDecisionRequired",
    "ReviewRoundsExhausted",
    "UnrecoverableDeliveryError",
)

RUN_ID = "abcdef12"
SOURCE_REPO = "orbi-build/orbi"
ISSUE = {"number": 42, "title": "Fix the thing", "labels": []}
PR_URL = "https://github.com/orbi-build/orbi/pull/7"
BRANCH = "orbi/orbi-build-orbi-issue-42"
SESSION_RECORD = '{"type": "message", "timestamp": "2026-09-28T10:00:00Z", "message": {"role": "assistant", "toolName": "bash", "content": [{"type": "text", "text": "working"}, {"type": "toolCall", "name": "bash"}]}}'
TEST_LOG = "collecting ...\n1 failed, 3 passed in 1.20s\n"

GOLDEN = {
    "classify": {
        "outcome": """blocked""",
        "issue_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:comment=failure -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"blocked","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: blocked — waiting on a human decision

**#42 Fix the thing**

PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)

Issue: `orbi-build/orbi#42` · run_id=abcdef12

**Action:** Repair provider access.

**Reason:** delivery failed

<details><summary>Diagnosis</summary>
- disposition: `Orbi failed: see the reason above`
- run: `abcdef12`
- PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)
- issue: `orbi-build/orbi#42`
- role: `review`
- branch: `orbi/orbi-build-orbi-issue-42`
- worktree: `local runner worktree`
- session: `-`
- session log: `<unavailable>`
- phase: `starting`
- last activity: `-`
- action: `-`
- result: `-`
- legacy correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=- phase=starting last_activity=-`
- correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=-`
- failure detail: `provider failed`
</details>

Need help? Join the Orbi community: https://t.me/+kJRTTpFGBRU0M2Y9""",
        "pr_comment": None,
        "milestones": ["""blocked: delivery failed"""],
        "finish": None,
    },
    "evidence": {
        "outcome": """fix needed""",
        "issue_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:comment=failure -->
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

**#42 Fix the thing**

PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)

Issue: `orbi-build/orbi#42` · run_id=abcdef12

**Action:** Repair provider access.

**Reason:** delivery failed

<details><summary>Diagnosis</summary>
- disposition: `Orbi needs a fix: see the reason above`
- run: `abcdef12`
- PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)
- issue: `orbi-build/orbi#42`
- role: `review`
- branch: `orbi/orbi-build-orbi-issue-42`
- worktree: `local runner worktree`
- session: `-`
- session log: `local session log`
- phase: `bash`
- last activity: `2026-09-28T10:00:00Z`
- action: `bash`
- result: `-`
- legacy correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=- phase=bash last_activity=2026-09-28T10:00:00Z`
- correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=-`
- failure detail: `provider failed`
Failure evidence (captured before cleanup):
exit_code=2

stderr_tail:
```
provider failed

```

stdout_tail:
```
stdout line

```

session_last_events (last 20 records; full log: local session log):
```
2026-09-28T10:00:00Z message role=assistant tool=bash content=text,toolCall:bash
```

test_log_tail:
```
collecting ...
1 failed, 3 passed in 1.20s
```
</details>""",
        "pr_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:comment=failure -->
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

**#42 Fix the thing**

PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)

Issue: `orbi-build/orbi#42` · run_id=abcdef12

**Action:** Repair provider access.

**Reason:** delivery failed

<details><summary>Diagnosis</summary>
- disposition: `Orbi needs a fix: see the reason above`
- run: `abcdef12`
- PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)
- issue: `orbi-build/orbi#42`
- role: `review`
- branch: `orbi/orbi-build-orbi-issue-42`
- worktree: `local runner worktree`
- session: `-`
- session log: `local session log`
- phase: `bash`
- last activity: `2026-09-28T10:00:00Z`
- action: `bash`
- result: `-`
- legacy correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=- phase=bash last_activity=2026-09-28T10:00:00Z`
- correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=-`
- failure detail: `provider failed`
Failure evidence (captured before cleanup):
exit_code=2

stderr_tail:
```
provider failed

```

stdout_tail:
```
stdout line

```

session_last_events (last 20 records; full log: local session log):
```
2026-09-28T10:00:00Z message role=assistant tool=bash content=text,toolCall:bash
```

test_log_tail:
```
collecting ...
1 failed, 3 passed in 1.20s
```
</details>""",
        "milestones": ["""fix needed: delivery failed"""],
        "finish": None,
    },
    "finish": {
        "outcome": """fix needed""",
        "issue_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:comment=failure -->
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

**#42 Fix the thing**

PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)

Issue: `orbi-build/orbi#42` · run_id=abcdef12

**Action:** Repair provider access.

**Reason:** delivery failed

<details><summary>Diagnosis</summary>
- disposition: `Orbi needs a fix: see the reason above`
- run: `abcdef12`
- PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)
- issue: `orbi-build/orbi#42`
- role: `review`
- branch: `orbi/orbi-build-orbi-issue-42`
- worktree: `local runner worktree`
- session: `-`
- session log: `local session log`
- phase: `bash`
- last activity: `2026-09-28T10:00:00Z`
- action: `bash`
- result: `-`
- legacy correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=- phase=bash last_activity=2026-09-28T10:00:00Z`
- correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=-`
- failure detail: `provider failed`
</details>""",
        "pr_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:comment=failure -->
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

**#42 Fix the thing**

PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)

Issue: `orbi-build/orbi#42` · run_id=abcdef12

**Action:** Repair provider access.

**Reason:** delivery failed

<details><summary>Diagnosis</summary>
- disposition: `Orbi needs a fix: see the reason above`
- run: `abcdef12`
- PR: [https://github.com/orbi-build/orbi/pull/7](https://github.com/orbi-build/orbi/pull/7)
- issue: `orbi-build/orbi#42`
- role: `review`
- branch: `orbi/orbi-build-orbi-issue-42`
- worktree: `local runner worktree`
- session: `-`
- session log: `local session log`
- phase: `bash`
- last activity: `2026-09-28T10:00:00Z`
- action: `bash`
- result: `-`
- legacy correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=- phase=bash last_activity=2026-09-28T10:00:00Z`
- correlation: `run=abcdef12 branch=orbi/orbi-build-orbi-issue-42 session=-`
- failure detail: `provider failed`
</details>""",
        "milestones": ["""fix needed: delivery failed"""],
        "finish": """<!-- orbi:run=abcdef12 -->
<!-- orbi:progress:v1 {"step":"review","reported_at":"2026-10-15T06:20:20Z","activity":{"at":"2026-09-28T10:00:00Z","kind":"command","target":null}} -->

**Orbi fix needed — the engine will retry**

What you need to do: Repair provider access.

What happened: delivery failed

<details><summary>Raw error</summary>
provider failed
</details>

**Orbi progress**

**#42 Fix the thing**

- role: review
- step: review
- reported at: 2026-10-15T06:20:20Z
- last activity: 2026-09-28T10:00:00Z
- activity: command
- tests: -
- PR: https://github.com/orbi-build/orbi/pull/7

<details><summary>Run details</summary>

- run_id=abcdef12
- priority: normal
- phase: bash
- elapsed: 0s
- branch: orbi/orbi-build-orbi-issue-42
- session: -

</details>

<!-- runner=deadbeef -->""",
    },
}


def _module_names(name: str) -> set[str]:
    """Every top-level binding a package module defines or assigns."""
    path = PACKAGE_DIR / f"{name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every imported module path plus each `from orbi import X` member."""
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            if module == "orbi":
                imported.extend(alias.name for alias in node.names)
    return imported


def test_failure_report_owns_the_moved_definitions():
    assert set(MOVED) <= _module_names("failure_report")


def test_runner_no_longer_defines_the_moved_definitions():
    # Acceptance: `grep -n "def report_delivery_failure\|class
    # UnrecoverableDeliveryError" src/orbi/runner.py` finds nothing.
    assert set(MOVED).isdisjoint(_module_names("runner"))


def test_runner_does_not_re_export_the_moved_names():
    """The runner call sites go through the module object: a stale
    `runner.<name>` patch must not silently stop intercepting."""
    for name in set(MOVED) - set(RUNNER_IMPORTS):
        assert not hasattr(runner, name), (
            f"orbi.runner must not re-export {name}: the definition is "
            "reached through `failure_report` (Issue #1260)"
        )


def test_runner_imports_back_only_what_it_uses():
    for name in RUNNER_IMPORTS:
        assert getattr(runner, name) is getattr(failure_report, name)
    assert runner.failure_report is failure_report


def test_failure_report_never_imports_runner():
    """Constitution Article 3.3: an extracted module never imports
    `runner` back — not at module scope and not inside a function."""
    path = PACKAGE_DIR / "failure_report.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        name for name in _imported_modules(tree)
        if name == "runner" or name.startswith("runner.")
    ]
    assert offenders == []
    assert not hasattr(failure_report, "runner")


def test_finish_progress_body_moved_to_progress():
    moved = {"_finish_outcome_body", "finish_progress_body"}
    assert moved <= _module_names("progress")
    assert moved.isdisjoint(_module_names("runner"))


class FakePublisher:
    """Records the milestone and finish publishes without GitHub."""

    language_code = "en"

    def __init__(self) -> None:
        self.milestones: list[str] = []
        self.finish_bodies: list[str] = []

    def milestone(self, body: str, **kwargs) -> None:
        self.milestones.append(body)

    def finish(self, body: str) -> None:
        self.finish_bodies.append(body)


def _worktree(tmp_path: Path, *, session: bool, test_log: str = TEST_LOG,
              session_content: str = SESSION_RECORD) -> Path:
    """A worktree with a test log, and a Pi session log for the evidence.

    `test_log` and `session_content` let a test hand the reporter real
    oversized evidence instead of a shape production never posts
    (Issue #1711).
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if session:
        (worktree / ".pi-session").mkdir()
        (worktree / ".pi-session" / "session.jsonl").write_text(
            f"{session_content}\n", encoding="utf-8",
        )
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(test_log, encoding="utf-8")
    return worktree


def _failure() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        2, ["pi"], output="stdout line\n", stderr="provider failed\n",
    )


def _report(monkeypatch, tmp_path: Path, *, session: bool,
            publisher: FakePublisher, exc: BaseException | None = None,
            history: list | None = None,
            test_log: str = TEST_LOG, session_content: str = SESSION_RECORD,
            **flags):
    """Call the moved reporter with GitHub and the fingerprint stubbed.

    `exc` and the two worktree contents feed the real evidence render;
    every other flag goes straight to the reporter.
    """
    posted: list[str] = []
    pr_comments: list[str] = []
    # The blocked path posts through the ONE blocked exit (Issue #1730): the
    # seam fans the write out to every module that binds it, so the capture
    # never pins one home.
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda number, *, repo, body: pr_comments.append(body),
    )
    monkeypatch.setattr(failure_report, "issue_labels", lambda *a, **k: set())
    # One read serves the dedup, the #825 guard and the round count: the
    # guard fixture supplies the trusted history, every other test the
    # empty list (Issue #1701).
    monkeypatch.setattr(
        failure_report, "issue_comments",
        lambda *a, **k: list(history or ()),
    )
    # The seam fans the read out to every module that binds it, so the
    # golden bodies stay deterministic without pinning one home.
    monkeypatch.setattr(seam, "runner_fingerprint", lambda: "deadbeef")
    # The versioned progress block carries the report generation time
    # (Issue #1657): the golden bodies pin it through the clock.
    freeze_report_time(monkeypatch)
    kwargs = dict(
        issue=dict(ISSUE), source_repo=SOURCE_REPO, run_id=RUN_ID,
        pr_url=PR_URL,
        worktree=_worktree(
            tmp_path, session=session, test_log=test_log,
            session_content=session_content,
        ),
        branch=BRANCH, role=runner.ROLE_REVIEW,
        action="Repair provider access.", reason="delivery failed",
        diagnosis="provider failed", publisher=publisher,
        language_code="en",
    )
    kwargs.update(flags)
    error = _failure() if exc is None else exc
    outcome = failure_report.report_delivery_failure(error, **kwargs)
    return outcome, posted, pr_comments


def test_classify_mode_body_is_byte_identical_to_the_pre_extraction_render(
        monkeypatch, tmp_path):
    """The implement-phase terminal template (`classify=False`): the run
    scene is space-joined onto the first body line and the terminal
    blocked state is Issue-only — no PR copy."""
    publisher = FakePublisher()
    outcome, posted, pr_comments = _report(
        monkeypatch, tmp_path, session=False, publisher=publisher,
        classify=False, evidence=False, finish=False,
    )
    assert outcome == GOLDEN["classify"]["outcome"]
    assert posted == [GOLDEN["classify"]["issue_comment"]]
    assert pr_comments == []
    assert publisher.milestones == GOLDEN["classify"]["milestones"]
    assert publisher.finish_bodies == []


def test_evidence_mode_body_is_byte_identical_to_the_pre_extraction_render(
        monkeypatch, tmp_path):
    """The classified recoverable body keeps its bounded evidence block
    and is mirrored onto the PR (the next tick resumes the same PR)."""
    publisher = FakePublisher()
    outcome, posted, pr_comments = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=True, evidence=True, finish=False,
    )
    assert outcome == GOLDEN["evidence"]["outcome"]
    assert posted == [GOLDEN["evidence"]["issue_comment"]]
    assert pr_comments == [GOLDEN["evidence"]["pr_comment"]]
    assert publisher.milestones == GOLDEN["evidence"]["milestones"]
    assert publisher.finish_bodies == []
    for segment in (
        "exit_code=2",
        "stderr_tail:",
        "stdout_tail:",
        "session_last_events (last 20 records; full log: local session log):",
        "test_log_tail:",
    ):
        assert segment in posted[0]


def test_full_failure_is_kinded_but_the_short_milestone_is_not(
        monkeypatch, tmp_path):
    """Issue #1659: the complete failure diagnostic carries the `failure`
    kind; the short milestone echo beside it must not claim it."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=True, evidence=False, finish=False,
    )
    assert outcome == "fix needed"
    assert posted[0].splitlines()[1] == "<!-- orbi:comment=failure -->"
    assert all(
        "<!-- orbi:comment=" not in milestone
        for milestone in publisher.milestones
    )


def test_finish_mode_body_is_byte_identical_to_the_pre_extraction_render(
        monkeypatch, tmp_path):
    """The terminal progress scene (`finish=True`) renders through
    `progress.finish_progress_body` with the same bytes."""
    publisher = FakePublisher()
    outcome, posted, pr_comments = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=True, evidence=False, finish=True,
    )
    assert outcome == GOLDEN["finish"]["outcome"]
    assert posted == [GOLDEN["finish"]["issue_comment"]]
    assert pr_comments == [GOLDEN["finish"]["pr_comment"]]
    assert publisher.milestones == GOLDEN["finish"]["milestones"]
    assert publisher.finish_bodies == [GOLDEN["finish"]["finish"]]


def _pr_comment(pr_number, round_no, *, opened=False, run_id=RUN_ID):
    """One trusted comment carrying `pr_number`'s scene at `round_no`.

    `opened=True` is the "Orbi opened PR" scene comment; otherwise it is a
    recorded review round (the heading `review_round_comment_body`
    writes).
    """
    pr_url = f"https://github.com/orbi-build/orbi/pull/{pr_number}"
    heading = (
        f"Orbi opened PR: {pr_url}" if opened
        else f"Orbi review round {round_no} for PR #{pr_number}: findings"
    )
    return {
        "body": (
            f"<!-- orbi:run={run_id} -->\n{heading}\n"
            + scene.render(scene.Scene(
                run_id=run_id, base_branch="main", base_sha="abc123",
                pr_url=pr_url, review_round=round_no,
            ))
        ),
        "authorAssociation": "OWNER",
        "author": {"login": "maintainer"},
    }


def test_finish_scene_reports_the_current_pr_scene_round(
        monkeypatch, tmp_path):
    """Issue #1772 acceptance 1: five completed rounds on an EARLIER PR
    (#100) followed by a new PR (#200) at round 0. The reported round is
    the new PR's scene round — the value the review loop reads — so the
    old PR's rounds are never recounted into the report (which showed
    "review/fix round: 5" before)."""
    history = [
        _pr_comment(100, round_no) for round_no in (1, 2, 3, 4, 5)
    ] + [_pr_comment(200, 0, opened=True)]
    publisher = FakePublisher()
    outcome, _, _ = _report(
        monkeypatch, tmp_path, session=False, publisher=publisher,
        history=history, classify=True, evidence=False, finish=True,
    )
    assert outcome == "fix needed"
    finish = publisher.finish_bodies[0]
    assert "- review/fix round: 5" not in finish
    assert "- review/fix round:" not in finish


def test_finish_scene_reports_a_single_prs_scene_round(
        monkeypatch, tmp_path):
    """Issue #1772 acceptance 2 with Issue #1779's one number: a single PR
    whose scene completed 3 rounds is reviewing round 4, and the report
    says 4 — the round the review loop uses (the row renders from round 3
    on; the helper is pinned in the round-accounting unit tests)."""
    publisher = FakePublisher()
    outcome, _, _ = _report(
        monkeypatch, tmp_path, session=False, publisher=publisher,
        history=[_pr_comment(42, 0, opened=True),
                 _pr_comment(42, 1), _pr_comment(42, 2),
                 _pr_comment(42, 3)],
        classify=True, evidence=False, finish=True,
    )
    assert outcome == "fix needed"
    assert "- review/fix round: 4" in publisher.finish_bodies[0]


@pytest.mark.parametrize(
    "language_code, remove_step, add_step",
    [("en", "remove `ai-blocked`", "add `ai-fix-needed`"),
     ("zh", "移除 `ai-blocked`", "添加 `ai-fix-needed`")],
)
def test_blocked_classified_next_step_tells_how_to_resume(
        monkeypatch, tmp_path, language_code, remove_step, add_step):
    """Issue #1701 acceptance 5: after a classified blocked outcome the
    final progress comment's next-step line says how to resume —
    remove `ai-blocked`, THEN add `ai-fix-needed`; relabelling alone never
    resumes a blocked Issue (the claim scans skip `ai-blocked`)."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=False, publisher=publisher,
        exc=runner.ReviewRoundsExhausted("review/fix loop exhausted"),
        action=None, classify=True, evidence=False, finish=True,
        language_code=language_code,
    )
    assert outcome == "blocked"
    assert "<!-- orbi:failure:v1" in posted[0]
    finish = publisher.finish_bodies[0]
    next_step = language.text(
        language_code, "failure.next_step_blocked_classified",
    )
    assert remove_step in next_step
    assert add_step in next_step
    assert next_step in finish
    assert "relabel the Issue ai-fix-needed" not in finish


@pytest.mark.parametrize(
    "language_code, remove_step, add_step",
    [("en", "remove `ai-blocked`", "add `ai-fix-needed`"),
     ("zh", "移除 `ai-blocked`", "添加 `ai-fix-needed`")],
)
def test_blocked_guard_escalation_keeps_the_next_step_line(
        monkeypatch, tmp_path, language_code, remove_step, add_step):
    """Issue #1701 acceptance 5 on the shape a blocked REVIEW failure
    really produces: the review handler carries no action for a failure it
    cannot repair itself, and the repeated-failure guard escalates the
    third identical failure to `ai-blocked`. Its final progress comment's
    next-step line must then carry the resume steps — never the bare
    engine-wait line, which leaves the human no way back to
    `ai-fix-needed`."""
    exc = RuntimeError("review worktree missing")
    fingerprint = runner_health.failure_fingerprint(exc)
    history = [
        {
            "id": 900 + index,
            "body": (
                f"<!-- orbi:run={RUN_ID} -->\n"
                f"<!-- orbi:fail={fingerprint} -->\n"
                "Orbi needs a fix: boom"
            ),
            "authorAssociation": "OWNER",
            "author": {"login": "maintainer"},
        }
        for index in range(failure_report.FAILURE_STREAK_LIMIT - 1)
    ]
    publisher = FakePublisher()
    outcome, _, _ = _report(
        monkeypatch, tmp_path, session=False, publisher=publisher,
        exc=exc, history=history, action="", classify=True,
        evidence=False, finish=True, language_code=language_code,
    )
    assert outcome == "blocked"
    finish = publisher.finish_bodies[0]
    next_step = language.text(
        language_code, "failure.next_step_blocked_classified",
    )
    assert remove_step in next_step
    assert add_step in next_step
    assert language.text(
        language_code, "progress.what_next", action=next_step,
    ) in finish
    assert language.text(
        language_code, "progress.engine_wait_pr", pr_url=PR_URL,
    ) not in finish


def test_recoverable_failure_without_a_pr_posts_the_issue_comment_only(
        monkeypatch, tmp_path):
    """The reporter's PR copy is optional (the blocked terminal state is
    Issue-only and never reaches it): a recoverable failure reported
    without a PR still posts the Issue comment — and, being a retrying
    diagnostic rather than a blocked scene, it carries no community help
    line (Issue #1730)."""
    posted: list[str] = []
    pr_comments: list = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    # The seam fans both names out to every module that binds them.
    monkeypatch.setattr(
        seam, "comment_pr", lambda *a, **k: pr_comments.append((a, k)),
    )
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [])
    publisher = FakePublisher()
    outcome = failure_report.report_delivery_failure(
        RuntimeError("provider failed"),
        issue=dict(ISSUE), source_repo=SOURCE_REPO, run_id=RUN_ID,
        pr_url=None, worktree=_worktree(tmp_path, session=False),
        branch=BRANCH, role=runner.ROLE_REVIEW, classify=True,
        finish=False, publisher=publisher, current_labels=set(),
        language_code="en",
    )
    assert outcome == "fix needed"
    assert len(posted) == 1
    assert language.text("en", "help.community") not in posted[0]
    assert pr_comments == []


def test_evidence_redacts_local_paths(monkeypatch, tmp_path):
    """Issue #1233 stays intact after the move: a host path in the Pi
    streams never reaches the comment."""
    publisher = FakePublisher()
    posted: list[str] = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setattr(failure_report, "comment_pr", lambda *a, **k: None)
    monkeypatch.setattr(failure_report, "issue_labels", lambda *a, **k: set())
    monkeypatch.setattr(failure_report, "issue_comments", lambda *a, **k: [])
    # The seam fans the read out to every module that binds it, so the
    # golden bodies stay deterministic without pinning one home.
    monkeypatch.setattr(seam, "runner_fingerprint", lambda: "deadbeef")
    error = subprocess.CalledProcessError(
        1, ["pi"],
        output="stdout line\n",
        stderr="/tmp/delivery/wt/secret file: boom\n",
    )
    failure_report.report_delivery_failure(
        error, issue=dict(ISSUE), source_repo=SOURCE_REPO, run_id=RUN_ID,
        pr_url=PR_URL, worktree=_worktree(tmp_path, session=True),
        branch=BRANCH, role=runner.ROLE_REVIEW, reason="delivery failed",
        classify=True, evidence=True, finish=False, publisher=publisher, language_code="en",
    )
    assert "/tmp/delivery" not in posted[0]
    assert "local runner path file: boom" in posted[0]


RESUME_WORKTREE = "/home/orbi-acme--app-1a2b3c4d/delivery/app/.worktrees/x"


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_resume_blocked_comment_is_redacted_and_ends_with_help_line(
        monkeypatch, language_code):
    """Issue #1721 acceptance 2 + Issue #1722: the "cannot continue the
    interrupted run" comment never shows the runner's worktree path, keeps
    `local runner path` where it was, still says the run state file is
    missing, and — because it blocks the Issue for a human — ends with the
    community help line as its own last paragraph, in the comment's language.
    Nothing else about the comment moves."""
    gh = FakeGh(SOURCE_REPO)
    gh.add_issue(42, title="Fix the thing", labels=("ai-in-progress",))
    monkeypatch.setattr(seam, "run_command", gh)
    error = RuntimeError(
        f"worktree {RESUME_WORKTREE} has no run state file "
        f"({RESUME_WORKTREE}/.orbi/run-state.json)",
    )
    failure_report._report_resume_failure(
        number=42, source_repo=SOURCE_REPO, run_id=RUN_ID,
        error=error, language_code=language_code,
    )
    assert len(gh.issues[42]["comments"]) == 1
    body = gh.issues[42]["comments"][0]["body"]
    assert "/home/" not in body
    # Acceptance 2: the markers and the sentence are untouched; the only
    # difference from the #1721 comment is the help paragraph at the end.
    sentence = language.text(
        language_code, "failure.resume_blocked",
        detail="worktree local runner path has no run state file "
               "(local runner path)",
        run_id=RUN_ID,
    )
    help_line = language.text(language_code, "help.community")
    # The comment layer appends the hidden runner marker; everything before
    # it is this function's body, asserted byte for byte.
    assert re.fullmatch(
        re.escape(
            f"<!-- orbi:run={RUN_ID} -->\n"
            f"<!-- orbi:comment=failure -->\n"
            f"{sentence}\n"
            f"\n"
            f"{help_line}"
        ) + r"\n\n<!-- runner=[^>]+ -->",
        body,
    )
    # Acceptance 1: the help line is the comment's last paragraph, in the
    # comment's language.
    commented = re.sub(r"\n\n<!-- runner=[^>]+ -->$", "", body)
    assert commented.split("\n\n")[-1] == help_line


# The failure-comment size limit and the two report-itself-failed guards:
# the branches the moved code brings along. A new file counts EVERY line as
# a changed line for tools/diff_coverage_gate.py (Issue #234, tier 2), so the
# new module has to be 100% covered and these tests keep it there.
#
# Issue #1711: an oversized comment shrinks ONLY its four evidence segments
# (capped at 4000 chars each by default), never the structure around them —
# a cut lands inside the last fence, so the Diagnosis closer and the help
# line would render as code in the collapsed block (the #1695 acceptance).
LONG_TEST_LOG = ("test line " + "y" * 290 + "\n") * 20
LONG_SESSION = "".join(
    json.dumps({
        "type": "message",
        "timestamp": f"2026-09-28T10:00:{index:02d}Z",
        "message": {
            "role": "assistant",
            "toolName": "bash-" + "x" * 200,
            "content": [
                {"type": "text", "text": f"step {index}"},
                {"type": "toolCall", "name": "bash"},
            ],
        },
    }) + "\n"
    for index in range(20)
)


def _oversized_evidence_error() -> subprocess.CalledProcessError:
    """Real Pi streams, both longer than one segment's 4000-char cap."""
    return subprocess.CalledProcessError(
        2, ["pi"], output="stdout line\n" * 400, stderr="stderr line\n" * 400,
    )


def _body_before_markers(posted: str) -> str:
    """The exact body the size check measured, before the prepended markers.

    The reporter puts the run marker, the failure kind marker and — on the
    recoverable path only — the failure fingerprint marker above the body
    (Issue #1711: the limit applies before them). A plain split on the
    first blank line would drop the body's own `orbi:failure:v1` record
    and so measure a shorter string than the one the limit checked.
    """
    lines = posted.splitlines(keepends=True)
    assert lines[0].startswith("<!-- orbi:run="), lines[0]
    assert lines[1] == "<!-- orbi:comment=failure -->\n", lines[1]
    start = 2
    if lines[start].startswith("<!-- orbi:fail="):
        start += 1
    assert lines[start].startswith("<!-- orbi:failure:v1 "), lines[start]
    return "".join(lines[start:])


def _fence_lines(body: str) -> list[int]:
    """Line indexes of whole code fences (three or more backticks)."""
    return [
        index for index, line in enumerate(body.splitlines())
        if len(line) >= 3 and set(line) == {"`"}
    ]


def _omission_notes(body: str) -> list[str]:
    return re.findall(r"_\[truncated, \d+ chars omitted\]_", body)


def _assert_comment_structure_intact(body: str) -> None:
    """Acceptance 1/2: closed fences, a closed Diagnosis and no code tail."""
    fences = _fence_lines(body)
    assert fences and len(fences) % 2 == 0, "every fence closes"
    assert body.count("<details>") == body.count("</details>")
    lines = body.splitlines()
    assert "</details>" in lines[fences[-1] + 1:], (
        "the Diagnosis closer after the last fence is not inside it"
    )


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_oversized_blocked_comment_shrinks_evidence_and_keeps_structure(
        monkeypatch, tmp_path, language_code):
    """Acceptance 1 (Issue #1711): maxed evidence plus a long action passes
    the limit, so the SEGMENTS shrink — each with its own omission note —
    and the help line stays last, outside the closed Diagnosis block."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        exc=_oversized_evidence_error(), test_log=LONG_TEST_LOG,
        session_content=LONG_SESSION, classify=False, evidence=True,
        finish=False, action="a" * 3000, language_code=language_code,
    )
    body = _body_before_markers(posted[0])
    help_line = language.text(language_code, "help.community")
    assert outcome == "blocked"
    assert len(body) <= failure_report.FAILURE_COMMENT_MAX_CHARS
    _assert_comment_structure_intact(body)
    assert body.endswith(help_line)
    # The rest of the comment is untouched: the long action is all there.
    assert "a" * 3000 in body
    notes = _omission_notes(body)
    assert len(notes) == 4
    # The shrink really ran: the 4800-char stderr segment is docked below
    # the 4000-char default, so more than its 800 default chars were cut.
    assert int(re.search(r"\d+", notes[0]).group()) > 800


def test_oversized_fix_needed_comment_shrinks_evidence_without_a_help_line(
        monkeypatch, tmp_path):
    """Acceptance 2: the recoverable comment follows the same one rule —
    evidence shrunk, structure intact, and never a community help line."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        exc=_oversized_evidence_error(), test_log=LONG_TEST_LOG,
        session_content=LONG_SESSION, classify=True, evidence=True,
        finish=False, action="a" * 3000,
    )
    body = _body_before_markers(posted[0])
    assert outcome == "fix needed"
    assert len(body) <= failure_report.FAILURE_COMMENT_MAX_CHARS
    _assert_comment_structure_intact(body)
    assert language.text("en", "help.community") not in body
    notes = _omission_notes(body)
    assert len(notes) == 4
    assert int(re.search(r"\d+", notes[0]).group()) > 800


def test_comment_under_the_limit_is_posted_unchanged(monkeypatch, tmp_path):
    """Acceptance 3: a body under the limit keeps today's bytes — the full
    default-cap evidence with no omission note (the goldens above pin the
    byte-for-byte render of the same under-limit path)."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=False, evidence=True, finish=False,
    )
    body = _body_before_markers(posted[0])
    assert outcome == "blocked"
    assert len(body) < failure_report.FAILURE_COMMENT_MAX_CHARS
    assert _failure().stderr.rstrip("\n") in body
    assert _omission_notes(body) == []
    assert failure_report._failure_evidence(
        tmp_path / "wt", _failure(), language_code="en",
    ).lstrip() in body


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_oversized_comment_without_evidence_is_posted_unchanged(
        monkeypatch, tmp_path, language_code):
    """Acceptance 4: only a comment WITH evidence can shrink, so one without
    it is posted exactly as rendered, however long — a cut there would bring
    back the unclosed-structure bug (Issue #1711)."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=False, evidence=False, finish=False,
        blocked_suffix="x" * 25000, language_code=language_code,
    )
    body = _body_before_markers(posted[0])
    help_line = language.text(language_code, "help.community")
    assert outcome == "blocked"
    assert "x" * 25000 in body
    assert language.text(
        language_code, "failure.evidence.heading") not in body
    assert "chars omitted" not in body
    assert body.endswith(f"</details>\n\n{help_line}")
    assert body.count("<details>") == body.count("</details>")


def test_oversized_comment_with_text_only_posts_cap_zero_evidence(
        monkeypatch, tmp_path):
    """Acceptance 4: when the text outside the evidence alone passes the
    limit, the evidence is posted at cap 0 — every segment an empty closed
    fence with its own omission note — and the comment is still never cut."""
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        exc=_oversized_evidence_error(), test_log=LONG_TEST_LOG,
        session_content=LONG_SESSION, classify=False, evidence=True,
        finish=False, action="a" * 25000,
    )
    body = _body_before_markers(posted[0])
    assert outcome == "blocked"
    assert "a" * 25000 in body
    assert body.count("```\n\n```") == 4
    # Cap 0: every segment is dropped whole and noted after its closed fence.
    notes = _omission_notes(body)
    assert len(notes) == 4
    assert int(re.search(r"\d+", notes[0]).group()) == 4800
    assert int(re.search(r"\d+", notes[1]).group()) == 4800
    _assert_comment_structure_intact(body)
    assert body.endswith(language.text("en", "help.community"))


def test_block_scene_failure_never_raises_a_reporting_failure(
        monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("github down")

    monkeypatch.setattr(seam, "apply_label_patch", broken)
    with caplog.at_level(logging.ERROR):
        failure_report.block_scene_failure(
            {"number": 42}, ValueError("bad scene"), "orbi-build/orbi", [], language_code="en",
        )
    assert "failure reporting failed" in caplog.text


def test_block_repo_config_failure_never_raises_a_reporting_failure(
        monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("github down")

    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue", broken)
    with caplog.at_level(logging.ERROR):
        failure_report.block_repo_config_failure(
            42, "orbi-build/orbi", ValueError("bad key"), RUN_ID, language_code="en",
        )
    assert "repo_config_failure_report_failed" in caplog.text
