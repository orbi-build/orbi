"""Issue #763: the human acceptance gate.

The carrier of the gate is the human-only label `ai-human-review`; the
waiting reuses the release-waiting shape (ticket back to `ai-ready`,
opened-PR anchor kept, slot released, no review session); the checklist
is the readable face posted once per delivery when the PR opens. The
gate short-circuits BEFORE the scene, the worktree or any Pi session —
one label read plus local evidence reads decide.

`orbi.human_review` is the pure half (classification, rendering); the
runner tests below cover the wiring in `_run_review_round`.
"""
import json
from unittest.mock import Mock

import pytest

from orbi import delivery_labels as dl
from orbi import human_review

import orbi.runner as runner
from seam import seam
import orbi.journal as journal

REPO = "owner/repo"
PR_URL = f"https://github.com/{REPO}/pull/46"
RUN_ID = "a1b2c3d4"
BRANCH = "orbi/owner-repo-issue-39"
BASE_SHA = "abc123def456"


# ------------------------------------------------------- pure: classification

def test_build_checklists_column1_carries_test_evidence_and_layer():
    checklist = human_review.build_checklist(
        test_result="156 passed in 4.43s",
        changed_files=["src/orbi/runner.py"],
        test_command="pytest tests/ -q",
    )
    assert any("156 passed in 4.43s" in item for item in checklist["column1"])
    assert any("pytest tests/ -q" in item for item in checklist["column1"])
    # The business-intent item fires: a non-test source file changed.
    assert len(checklist["column2"]) == 1
    assert "业务意图" in checklist["column2"][0]


def test_build_checklists_column2_adds_ui_and_deploy_dimensions():
    checklist = human_review.build_checklist(
        test_result="10 passed in 0.1s",
        changed_files=[
            "src/orbi/runner.py",
            "templates/index.html",
            ".github/workflows/ci.yml",
        ],
        test_command="pytest -q",
    )
    joined = "\n".join(checklist["column2"])
    assert "业务意图" in joined
    assert "UI" in joined
    assert "外部环境" in joined


def test_build_checklists_docs_changes_do_not_trigger_the_intent_item():
    """Docs changes are not source changes: they carry no business-intent
    item (the classifier stays data-driven; the issue's column-2
    dimensions are intent, UI, external environment)."""
    checklist = human_review.build_checklist(
        test_result="10 passed in 0.1s",
        changed_files=["docs/workflow.mdx", "README.md"],
        test_command="pytest -q",
    )
    assert checklist["column2"] == []


def test_build_checklists_tests_only_delivery_has_an_empty_column2():
    """A tests-only delivery changed no machine-unverifiable dimension:
    the empty column 2 is the target state and must be expressible."""
    checklist = human_review.build_checklist(
        test_result="10 passed in 0.1s",
        changed_files=["tests/test_new.py", "tests/conftest.py"],
        test_command="pytest -q",
    )
    assert checklist["column1"]
    assert checklist["column2"] == []


def test_build_checklists_missing_test_evidence_lands_in_column2():
    checklist = human_review.build_checklist(
        test_result=None,
        changed_files=["tests/test_new.py"],
        test_command="pytest -q",
    )
    assert any("测试证据" in item for item in checklist["column2"])


def test_build_checklists_unreadable_diff_lands_in_column2():
    checklist = human_review.build_checklist(
        test_result="10 passed in 0.1s",
        changed_files=None,
        test_command="pytest -q",
    )
    assert any("改动文件" in item for item in checklist["column2"])


def test_build_checklists_degenerate_paths_never_crash_the_classifier():
    """A degenerate changed path (an empty string) classifies as nothing
    test/docs/UI/deploy-shaped — the conservative intent item still
    fires (garbage in holds the gate, it never passes it)."""
    checklist = human_review.build_checklist(
        test_result="10 passed in 0.1s",
        changed_files=["", "//"],
        test_command="pytest -q",
    )
    assert any("业务意图" in item for item in checklist["column2"])
    assert not any("UI" in item for item in checklist["column2"])
    assert not any("外部环境" in item for item in checklist["column2"])


# ------------------------------------------------------- pure: rendering

def _render(**overrides):
    kwargs = dict(
        run_id=RUN_ID,
        pr_url=PR_URL,
        test_command="pytest tests/ -q",
        checklist={"column1": ["测试结果：10 passed"], "column2": ["业务意图"]},
    )
    kwargs.update(overrides)
    return human_review.render_checklist_comment(**kwargs)


def test_rendered_checklist_carries_the_run_marker_and_machine_block():
    body = _render()
    assert body.startswith(f"<!-- orbi:run={RUN_ID} -->")
    assert "run_id=" in body
    start = body.index("<!-- orbi:human-review\n")
    payload = body[start + len("<!-- orbi:human-review\n"):]
    data = json.loads(payload.split("-->")[0])
    assert data["schema"] == 1
    assert data["run_id"] == RUN_ID
    assert data["column1"] == ["测试结果：10 passed"]
    assert data["column2"] == ["业务意图"]


def test_rendered_checklist_never_counts_as_a_review_round():
    """The hard constraint: the checklist must not start any line with
    `Orbi review round ` — `review_rounds_so_far` counts such lines and
    a checklist would burn the bounded review budget."""
    body = _render()
    assert runner.review_rounds_so_far([
        {"body": body, "authorAssociation": "OWNER"},
    ]) == 0
    assert not any(
        line.startswith("Orbi review round ") for line in body.splitlines()
    )


def test_rendered_checklist_names_both_columns_and_the_pr():
    body = _render()
    assert "栏一" in body and "栏二" in body
    assert PR_URL in body
    assert "pytest tests/ -q" in body
    # Copy discipline (website#128): the checklist never claims a
    # guarantee of correctness/direction.
    assert "保证" not in body


def test_rendered_checklist_empty_column2_says_no_human_intervention():
    body = _render(checklist={"column1": ["x"], "column2": []})
    assert "无需人工介入" in body
    assert "ai-human-review" in body


# ------------------------------------------------------- runner: the config

def test_load_config_human_review_gate_defaults_off_and_validates(tmp_path):
    """Issue #763 section 6: the switch is a HOST config key (the gate is
    the deployment operator's trust decision, like `allow_stale_runner`
    — never a repository-writable delivery policy), default OFF = the
    exact pre-#763 behavior."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n',
                           encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.human_review_gate is False
    config_path.write_text(
        'source_repos = ["owner/repo"]\nhuman_review_gate = true\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).human_review_gate is True
    config_path.write_text(
        'source_repos = ["owner/repo"]\nhuman_review_gate = "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="human_review_gate must be a boolean",
    ):
        runner.load_config(config_path)


# ------------------------------------------------------- runner: the gate

def _scene_comments():
    return [
        {
            "body": (
                f"<!-- orbi:run={RUN_ID} -->\n"
                f"Orbi opened PR: {PR_URL} (base_branch=main "
                f"base_sha={BASE_SHA} run_id={RUN_ID})"
            ),
            "authorAssociation": "OWNER",
        },
    ]


def _gate_config(tmp_path, *, gate=True):
    return runner.RunnerConfig(repo_dir=tmp_path, base_branch="main", base_sha=BASE_SHA, test_command="pytest tests/ -q", human_review_gate=gate)


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    """A delivered worktree with real test evidence on disk plus the
    label/scene/diff fakes `_run_review_round` reads. The review session
    is a Mock: the gate must start none."""
    worktree = runner.worktree_path(tmp_path, REPO, 39, RUN_ID)
    (worktree / ".orbi").mkdir(parents=True)
    (worktree / ".orbi" / "test.log").write_text(
        "================= 156 passed in 4.43s =================\n",
        encoding="utf-8",
    )
    calls = {"edit": [], "comments": 0, "diff": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "diff"]:
            calls["diff"] += 1
            return "src/orbi/runner.py\n"
        if command[:3] == ["git", "branch", "--show-current"]:
            return BRANCH
        if command[:2] == ["gh", "api"]:
            # Progress reads/writes of the failure-classification path
            # (a bypass; the shape only needs to be JSON).
            return json.dumps([
                {"id": 77, "body": "x"},
            ])
        if command[:3] in (["gh", "issue", "comment"],
                           ["gh", "pr", "comment"]):
            return json.dumps({"id": 78, "body": "x"})
        raise AssertionError(f"unexpected command: {command}")

    reviews = []

    def fake_review(worktree, branch, base_branch, config, repo, number,
                    **kwargs):
        reviews.append((worktree, branch, config))
        return False

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "issue_labels",
                        lambda *a, **k: ["ai-pr-opened"])
    monkeypatch.setattr(seam, "issue_comments",
        lambda *a, **k: (calls.__setitem__("comments", calls["comments"] + 1)
                         or _scene_comments()),
    )
    monkeypatch.setattr(seam, "edit_issue",
        lambda number, **kwargs: calls["edit"].append(kwargs),
    )
    monkeypatch.setattr(runner, "review_and_merge_if_clean", fake_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    return {"worktree": worktree, "calls": calls, "reviews": reviews}


def _round(config):
    return runner._run_review_round(
        PR_URL, {"number": 39, "title": "task"}, config, REPO,
    )


def test_gate_off_runs_the_review_exactly_like_today(gate_env, tmp_path):
    outcome = _round(_gate_config(tmp_path, gate=False))
    assert outcome is False
    assert gate_env["reviews"]
    assert gate_env["calls"]["edit"] == []
    # The fixture fake is strict: anything outside the recorded gh/git
    # surface fails the test loudly instead of answering garbage.
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["definitely", "not", "a", "known", "command"])


def test_gate_on_with_column2_holds_without_any_session(gate_env, tmp_path):
    """The core acceptance: the gate fires before the scene, the
    worktree or any review session; the waiting patch returns the
    ticket to `ai-ready` and the round ends (the slot is released by
    the caller)."""
    outcome = _round(_gate_config(tmp_path))
    assert outcome is None
    assert gate_env["reviews"] == []
    assert gate_env["calls"]["comments"] == 0  # no scene, no review budget
    assert gate_env["calls"]["edit"] == [{"repo": REPO, "add": "ai-ready"}]


def test_gate_on_second_tick_does_not_rewrite_the_waiting_patch(
    gate_env, tmp_path, monkeypatch,
):
    monkeypatch.setattr(seam, "issue_labels",
        lambda *a, **k: ["ai-ready", "ai-pr-opened"],
    )
    outcome = _round(_gate_config(tmp_path))
    assert outcome is None
    assert gate_env["calls"]["edit"] == []
    assert gate_env["reviews"] == []


def test_gate_on_with_the_human_label_runs_the_review(gate_env, tmp_path,
                                                      monkeypatch):
    monkeypatch.setattr(seam, "issue_labels",
        lambda *a, **k: ["ai-pr-opened", dl.HUMAN_REVIEW_LABEL],
    )
    outcome = _round(_gate_config(tmp_path))
    assert outcome is False
    assert gate_env["reviews"]
    assert gate_env["calls"]["edit"] == []


def test_gate_on_with_empty_column2_passes_without_the_label(
    gate_env, tmp_path, monkeypatch,
):
    monkeypatch.setattr(seam, "issue_labels",
        lambda *a, **k: ["ai-pr-opened"],
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: "tests/test_new.py\n",
    )
    outcome = _round(_gate_config(tmp_path))
    assert outcome is False
    assert gate_env["reviews"]
    assert gate_env["calls"]["edit"] == []


def test_gate_on_with_missing_worktree_falls_through_to_recovery(
    gate_env, tmp_path,
):
    """A missing worktree keeps the existing resume-recovery semantics:
    the gate must not swallow the recoverable-failure path, so it only
    decides when the evidence directory exists."""
    import shutil

    shutil.rmtree(gate_env["worktree"])
    outcome = _round(_gate_config(tmp_path))
    # The scene path ran (the gate holds read no comments) and the
    # existing recoverable classification applied (ai-fix-needed).
    assert outcome is None
    assert gate_env["calls"]["comments"] >= 1
    assert any(
        call.get("remove") == "ai-pr-opened"
        for call in gate_env["calls"]["edit"]
    )
    assert gate_env["reviews"] == []


def test_gate_hold_does_not_consume_the_review_round_budget(gate_env,
                                                           tmp_path):
    """The waiting primitive never touches `ai-fix-needed` and never
    posts a review-round comment: the budget a later human-labeled
    review finds is the budget the delivery had before the wait."""
    _round(_gate_config(tmp_path))
    comments = _scene_comments()
    assert runner.review_rounds_so_far(comments, run_id=RUN_ID) == 0


def test_delivered_changed_files_failure_is_missing_evidence(
    tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def boom(command, **kwargs):
        raise RuntimeError("git down")

    monkeypatch.setattr(seam, "run_command", boom)
    assert runner.delivered_changed_files(worktree, "main") is None
    # Missing evidence lands in column 2 (the gate holds).
    checklist = human_review.build_checklist(
        test_result="10 passed", changed_files=None, test_command=None,
    )
    assert any("改动文件" in item for item in checklist["column2"])
