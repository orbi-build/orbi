"""Behaviour golden for the extracted PR verification (Issue #1864).

The extraction moves `verify_pr` into `orbi.pr_verification` and
`verify_resumed_pr` (with the external-takeover terminal scenes it shares
with `delivery_step`) into `orbi.resume_verification`. Nothing about a
delivery may change, so these goldens pin the two decisions the move
could break silently:

* a resume whose local HEAD is AHEAD of the PR head still CONTINUES: the
  verified URL it returns, the exact journal line recording the fact and
  the in-flight label backfill;
* a recoverable verification failure still stops the delivery in the
  automatic fix loop: the `ai-fix-needed` label transition and the exact
  run-marked failure comment on the Issue plus its PR copy.

Both goldens drive the REAL verifier and the REAL failure routing; only
the subprocess boundary is faked (the seam fan-out), so the text they pin
is the text a maintainer reads on GitHub.

They also pin the structure the extraction relies on: the new modules
never import `runner`, and `runner` reaches them through the module
objects (no re-exported name), so a stale patch aimed at `runner` fails
loudly instead of staying green while testing nothing.
"""
import ast
from pathlib import Path

import pytest

import orbi.journal as journal
import orbi.pr_verification as pr_verification
import orbi.resume_verification as resume_verification
import orbi.runner as runner
from seam import seam
from tests.fakes.github import FakeGh
from tests.fakes.progress import bind_run_id
from tests.test_resume_pr import (
    FAKE_BRANCH,
    FAKE_PR_URL,
    FAKE_RUN_ID,
    expected_resume_worktree,
    make_resume_config,
    make_resume_failure_fake,
    make_resume_issue,
    make_resume_scene,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"
JOURNAL_LOGGER = journal.LOGGER.name

REMOTE_HEAD = "1" * 40
LOCAL_HEAD = "2" * 40


def _imported_module_names(tree: ast.Module) -> set[str]:
    """Every orbi module the file imports, at any scope."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


# ---------------------------------------------------------------------------
# structure: no reverse import, no silent re-export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name", ["pr_verification", "resume_verification"],
)
def test_new_modules_never_import_runner(module_name):
    """Constitution Article 3.3: an extracted module never imports
    `runner` back — not at module scope and not inside a function."""
    path = PACKAGE_DIR / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        name for name in _imported_module_names(tree)
        if name == "runner" or name.startswith("runner.")
    ]
    assert offenders == []
    module = getattr(runner, module_name)
    assert not hasattr(module, "runner")


def test_runner_reaches_the_verifiers_through_the_module_objects():
    """The recorded extraction decision (Issue #1260, applied here): the
    runner call sites go through the module object, so a patch aimed at
    the old `runner` name fails loudly instead of staying green while the
    real verifier runs."""
    assert runner.pr_verification is pr_verification
    assert runner.resume_verification is resume_verification
    for name in (
        "verify_pr", "verify_resumed_pr", "_pr_head_repo",
        "_close_external_triage_issue", "_requeue_closed_external_takeover",
        "MissingFixesError", "ResumeVerificationError",
        "ResumePrClosedError", "ResumeBranchGoneError",
    ):
        assert not hasattr(runner, name), (
            f"orbi.runner must not re-export {name}: the definition is "
            "reached through its module (Issue #1864)"
        )
    # Only the exception the tick boundary catches is imported back.
    assert runner.ReportedMissingFixesError is (
        pr_verification.ReportedMissingFixesError
    )


# ---------------------------------------------------------------------------
# the scene fakes: the real verifier, the real failure routing
# ---------------------------------------------------------------------------


def _scene_run(fake_gh: FakeGh, *, local_head: str):
    """Answer the three local git reads and hand gh commands to FakeGh.

    `git merge-base --is-ancestor` exits 0 (`""`): the PR head is an
    ancestor of the local HEAD. Any other git command is a dispatch bug
    and fails loudly on the missing key.
    """
    reads = {"branch": FAKE_BRANCH, "rev-parse": local_head, "merge-base": ""}

    def run(command, **kwargs):
        if command[0] != "git":
            return fake_gh(command, **kwargs)
        return reads[command[1]]

    return run


# ---------------------------------------------------------------------------
# golden 1: the resume continues on the scene PR
# ---------------------------------------------------------------------------


def test_resume_decision_and_journal_line_are_golden(
    monkeypatch, tmp_path, caplog,
):
    """The recoverable unpushed-commit scene: the local HEAD is AHEAD of
    the PR head and the PR head is its ancestor. The resume CONTINUES
    (the next review pushes), records the exact journal line and
    backfills the in-flight label."""
    fake = FakeGh("owner/repo")
    fake.add_issue(9, title="ship", labels=("ai-pr-opened",))
    fake.add_pr(
        9, head=FAKE_BRANCH, base="main", oid=REMOTE_HEAD, url=FAKE_PR_URL,
        body=f"<!-- orbi:run={FAKE_RUN_ID} -->\n\nFixes #9\n\nPlan",
    )
    monkeypatch.setattr(
        seam, "run_command", _scene_run(fake, local_head=LOCAL_HEAD),
    )
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    bind_run_id(monkeypatch, FAKE_RUN_ID)
    caplog.set_level("INFO")

    url = resume_verification.verify_resumed_pr(
        make_resume_scene(), make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )

    # The decision: the resume continues on the verified scene PR.
    assert url == FAKE_PR_URL
    # The journal line the decision records — golden text, run-prefixed.
    lines = [
        record.getMessage() for record in caplog.records
        if record.name == JOURNAL_LOGGER
    ]
    assert lines == [
        f"[{FAKE_RUN_ID}] local_head_ahead_of_pr_head "
        f"pr_head={REMOTE_HEAD} local_head={LOCAL_HEAD} branch={FAKE_BRANCH}",
    ]
    # The in-flight label is backfilled before the work continues.
    assert "ai-in-progress" in fake.issues[9]["labels"]
    assert "ai-pr-opened" in fake.issues[9]["labels"]


# ---------------------------------------------------------------------------
# golden 2: a recoverable verification failure stays in the fix loop
# ---------------------------------------------------------------------------

_VERIFY_REASON = (
    "The resume verification of PR https://github.com/owner/repo/pull/9 "
    "failed: PR body is missing the stable run marker "
    "<!-- orbi:run=a1b2c3d4 -->; the PR must carry the run marker of this "
    "delivery line (the creating run or any later run of this Issue)"
)
# The exact failure comment (Issue copy) of one recoverable resume
# verification failure: the run markers, the versioned failure block, the
# headline, the scene and the diagnosis.
EXPECTED_ISSUE_BODY = "\n".join([
    f"<!-- orbi:run={FAKE_RUN_ID} -->",
    "<!-- orbi:comment=failure -->",
    "<!-- orbi:fail=47a540f35885f7bf -->",
    '<!-- orbi:failure:v1 {"schema":1,"outcome":"fix_needed",'
    '"reason_code":"unclassified","action_code":"fix_ticket",'
    '"retry_safe":false} -->',
    "",
    "Orbi: fix needed — the engine will retry",
    "",
    "**#9 ship**",
    "",
    f"PR: [{FAKE_PR_URL}]({FAKE_PR_URL})",
    "",
    f"Issue: `owner/repo#9` · run_id={FAKE_RUN_ID}",
    "",
    f"**Reason:** {_VERIFY_REASON}",
    "",
    "<details><summary>Diagnosis</summary>",
    "- disposition: `Orbi needs a fix: see the reason above`",
    f"- run: `{FAKE_RUN_ID}`",
    f"- PR: [{FAKE_PR_URL}]({FAKE_PR_URL})",
    "- issue: `owner/repo#9`",
    "- role: `review`",
    f"- branch: `{FAKE_BRANCH}`",
    "- worktree: `local runner worktree`",
    "- session: `-`",
    "- session log: `<unavailable>`",
    "- phase: `starting`",
    "- last activity: `-`",
    "- action: `-`",
    "- result: `-`",
    f"- legacy correlation: `run={FAKE_RUN_ID} branch={FAKE_BRANCH} "
    "session=- phase=starting last_activity=-`",
    f"- correlation: `run={FAKE_RUN_ID} branch={FAKE_BRANCH} session=-`",
    "- failure detail: `Orbi needs a fix: see the reason above`",
    "</details>",
])
EXPECTED_PR_BODY = EXPECTED_ISSUE_BODY + "\n\n<!-- runner=8a12fb1c -->"


def test_fix_needed_decision_and_comment_text_are_golden(
    monkeypatch, tmp_path,
):
    """A PR body that lost the delivery line's run marker is a
    RECOVERABLE verification failure: the Issue stays in the automatic
    fix loop with `ai-fix-needed` and the failure comment below, on the
    Issue and on the PR."""
    captured, failure_run = make_resume_failure_fake(
        monkeypatch, labels=["ai-pr-opened"],
    )
    fake = FakeGh("owner/repo")
    fake.add_issue(9, title="ship", labels=("ai-pr-opened",))
    fake.add_pr(
        9, head=FAKE_BRANCH, base="main", oid=LOCAL_HEAD, url=FAKE_PR_URL,
        body="please review my change",
    )
    scene_run = _scene_run(fake, local_head=LOCAL_HEAD)

    def run(command, **kwargs):
        if command[0] == "git" or command[0] == "gh" and (
            command[1] == "pr" and command[2] in ("list", "view")
        ):
            return scene_run(command, **kwargs)
        return failure_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    bind_run_id(monkeypatch, FAKE_RUN_ID)

    with pytest.raises(RuntimeError, match="stable run marker"):
        resume_verification.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )

    # The decision: recoverable -> ai-fix-needed, ai-pr-opened removed.
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    # The text: the Issue comment and its PR copy, exactly.
    issue_body = captured["comments"][0][1]["body"]
    assert issue_body == EXPECTED_ISSUE_BODY
    assert captured["pr_comments"] == [EXPECTED_PR_BODY]
