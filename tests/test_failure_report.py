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
`progress._finish_progress_body`). Each golden is asserted byte for byte
against the post-move call, and the AI/human decision (`blocked` vs
`fix needed`) with it.
"""
import ast
import logging
import subprocess
from pathlib import Path

from orbi import failure_report, progress, runner

from seam import seam

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

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
    "_main_ci_triage_url",
    "_snapshot_or_placeholder",
    "FAILURE_COMMENT_MAX_CHARS",
    "FAILURE_STREAK_LIMIT",
    "SESSION_SUMMARY_LIMIT",
    "_BLOCKED_PRECONDITION_PHRASE",
    "_SGR_RE",
    "comment_pr",
    "review_rounds_so_far",
)
# `comment_pr` and `review_rounds_so_far` moved here instead of `github.py`:
# the frozen size ratchet (github.py at 1146 lines against its 1162 ceiling)
# forbids the +58 lines, and the Issue names this module as the fallback.
# The names below are the only moved ones the runner still uses directly, so
# it imports them back by name; everything else it reaches as
# `failure_report.<name>` and the moved list above must not be re-exported.
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
<!-- orbi:failure:v1 {"schema":1,"outcome":"blocked","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: blocked — waiting on a human decision

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
</details>""",
        "pr_comment": None,
        "milestones": ["""blocked: delivery failed"""],
        "finish": None,
    },
    "evidence": {
        "outcome": """fix needed""",
        "issue_comment": """<!-- orbi:run=abcdef12 -->
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

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
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

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
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

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
<!-- orbi:fail=f4037a77e6e12cdc -->
<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed","reason_code":"unclassified","action_code":"fix_ticket","retry_safe":false} -->

Orbi: fix needed — the engine will retry

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
        "finish": """**Orbi fix needed — the engine will retry**

What you need to do: Repair provider access.

What happened: delivery failed

<details><summary>Raw error</summary>
provider failed
</details>

<!-- orbi:run=abcdef12 -->

**Orbi progress**

- role: review
- last activity: 2026-09-28T10:00:00Z
- tests: 1 failed, 3 passed in 1.20s
- PR: https://github.com/orbi-build/orbi/pull/7

<details><summary>Run details</summary>

- issue: #42 Fix the thing
- run_id=abcdef12
- priority: normal
- phase: bash
- elapsed: 0s
- last action: bash
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
    moved = {"_finish_outcome_body", "_finish_progress_body"}
    assert moved <= _module_names("progress")
    assert moved.isdisjoint(_module_names("runner"))


class FakePublisher:
    """Records the milestone and finish publishes without GitHub."""

    def __init__(self) -> None:
        self.milestones: list[str] = []
        self.finish_bodies: list[str] = []

    def milestone(self, body: str, **kwargs) -> None:
        self.milestones.append(body)

    def finish(self, body: str) -> None:
        self.finish_bodies.append(body)


def _worktree(tmp_path: Path, *, session: bool) -> Path:
    """A worktree with a test log, and a Pi session log for the evidence."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if session:
        (worktree / ".pi-session").mkdir()
        (worktree / ".pi-session" / "session.jsonl").write_text(
            SESSION_RECORD + "\n", encoding="utf-8",
        )
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(TEST_LOG, encoding="utf-8")
    return worktree


def _failure() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        2, ["pi"], output="stdout line\n", stderr="provider failed\n",
    )


def _report(monkeypatch, tmp_path: Path, *, session: bool,
            publisher: FakePublisher, **flags):
    """Call the moved reporter with GitHub and the fingerprint stubbed."""
    posted: list[str] = []
    pr_comments: list[str] = []
    monkeypatch.setattr(
        failure_report, "apply_label_patch", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        failure_report, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda number, *, repo, body: pr_comments.append(body),
    )
    monkeypatch.setattr(failure_report, "issue_labels", lambda *a, **k: set())
    monkeypatch.setattr(failure_report, "issue_comments", lambda *a, **k: [])
    # The seam fans the read out to every module that binds it, so the
    # golden bodies stay deterministic without pinning one home.
    monkeypatch.setattr(seam, "runner_fingerprint", lambda: "deadbeef")
    kwargs = dict(
        issue=dict(ISSUE), source_repo=SOURCE_REPO, run_id=RUN_ID,
        pr_url=PR_URL, worktree=_worktree(tmp_path, session=session),
        branch=BRANCH, role=runner.ROLE_REVIEW,
        action="Repair provider access.", reason="delivery failed",
        diagnosis="provider failed", publisher=publisher,
    )
    kwargs.update(flags)
    outcome = failure_report.report_delivery_failure(_failure(), **kwargs)
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


def test_finish_mode_body_is_byte_identical_to_the_pre_extraction_render(
        monkeypatch, tmp_path):
    """The terminal progress scene (`finish=True`) renders through
    `progress._finish_progress_body` with the same bytes."""
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


def test_evidence_redacts_local_paths(monkeypatch, tmp_path):
    """Issue #1233 stays intact after the move: a host path in the Pi
    streams never reaches the comment."""
    publisher = FakePublisher()
    posted: list[str] = []
    monkeypatch.setattr(
        failure_report, "apply_label_patch", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        failure_report, "comment_issue",
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
        classify=True, evidence=True, finish=False, publisher=publisher,
    )
    assert "/tmp/delivery" not in posted[0]
    assert "local runner path file: boom" in posted[0]


# The failure-comment cap and the two report-itself-failed guards: the
# branches the moved code brings along. A new file counts EVERY line as a
# changed line for tools/diff_coverage_gate.py (Issue #234, tier 2), so the
# new module has to be 100% covered and these three tests keep it there.
def test_oversized_body_is_truncated_and_names_the_session_log(
        monkeypatch, tmp_path):
    publisher = FakePublisher()
    outcome, posted, _ = _report(
        monkeypatch, tmp_path, session=True, publisher=publisher,
        classify=False, evidence=False, finish=False,
        blocked_suffix="x" * 25000,
    )
    assert outcome == "blocked"
    # The cap bounds the body; the run marker rides on top of it.
    assert len(posted[0]) < failure_report.FAILURE_COMMENT_MAX_CHARS + 200
    assert "x" * 25000 not in posted[0]
    assert (
        f"[comment truncated at {failure_report.FAILURE_COMMENT_MAX_CHARS} "
        "chars; full session log: local session log]" in posted[0]
    )


def test_block_scene_failure_never_raises_a_reporting_failure(
        monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("github down")

    monkeypatch.setattr(failure_report, "apply_label_patch", broken)
    with caplog.at_level(logging.ERROR):
        failure_report.block_scene_failure(
            {"number": 42}, ValueError("bad scene"), "orbi-build/orbi", [],
        )
    assert "failure reporting failed" in caplog.text


def test_block_repo_config_failure_never_raises_a_reporting_failure(
        monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("github down")

    monkeypatch.setattr(
        failure_report, "apply_label_patch", lambda *a, **k: None,
    )
    monkeypatch.setattr(failure_report, "comment_issue", broken)
    with caplog.at_level(logging.ERROR):
        failure_report.block_repo_config_failure(
            42, "orbi-build/orbi", ValueError("bad key"), RUN_ID,
        )
    assert "repo_config_failure_report_failed" in caplog.text
