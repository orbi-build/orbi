"""The Epic reconciliation domain (Issue #2356).

An Epic's children are verified against GitHub's live state and each one is
classified by how it ended: delivered by Orbi (a merged PR child, or an
Issue carrying `ai-merged`), closed while it carried `ai-blocked`, or
closed by a person with GitHub's own `state_reason`. The two decisions
built on that — the reconciliation sentence and the Epic's close reason —
live in one module so the per-tick sweep and the release sweep can never
disagree.

Every test drives the real `gh` command boundary (`run_command`), so the
argv and the parsed live payload shapes are the contract under test.
"""
import json

import pytest

import orbi.epic_reconciliation as epic
from seam import seam

EPIC = {"number": 20, "blockedBy": {"nodes": []}}


def _child(number, *, pr=False):
    child = {"number": number, "repository": {"full_name": "o/r"},
             "state": "closed"}
    if pr:
        child["pull_request"] = {}
    return child


def _gh_fake(children, issues, pulls=None):
    """The `gh` boundary fake: sub-issues plus one live read per child.

    The endpoints are the ones the adapter emits, `--include` included
    (Issue #1760 asks for the response status on every `gh api` read).
    """
    def fake_run(command, **kwargs):
        if command[:2] != ["gh", "api"]:
            raise AssertionError(command)
        assert command[-1] == "--include", command
        target = command[2]
        if target == "repos/o/r/issues/20/sub_issues?per_page=100":
            return json.dumps([children])
        number = int(target.rsplit("/", 1)[1])
        if target.startswith("repos/o/r/pulls/"):
            return json.dumps((pulls or {})[number])
        if target.startswith("repos/o/r/issues/"):
            return json.dumps(issues[number])
        raise AssertionError(command)

    return fake_run


def test_every_child_is_classified_by_how_it_ended(monkeypatch):
    """Acceptance 1-4: a merged PR and an `ai-merged` Issue are delivered by
    Orbi; `ai-blocked` is a child closed while blocked; every other closed
    Issue was closed by a person, and GitHub's `state_reason` says how."""
    children = [
        _child(31, pr=True), _child(32), _child(33), _child(34),
        _child(35), _child(36),
    ]
    issues = {
        31: {"pull_request": {}, "state": "closed"},
        32: {"state": "closed", "labels": [{"name": "ai-merged"}]},
        33: {"state": "closed", "labels": [{"name": "ai-blocked"}]},
        34: {"state": "closed", "state_reason": "completed", "labels": []},
        35: {"state": "closed", "state_reason": "not_planned", "labels": []},
        # A closed Issue without the field is GitHub's default: completed.
        36: {"state": "closed"},
    }
    monkeypatch.setattr(seam, "run_command",
                        _gh_fake(children, issues, {31: {"merged": True}}))
    assert epic.verify_epic_complete("o/r", EPIC) == [
        epic.EpicChild("pr", 31, epic.CHILD_DELIVERED),
        epic.EpicChild("issue", 32, epic.CHILD_DELIVERED),
        epic.EpicChild("issue", 33, epic.CHILD_BLOCKED),
        epic.EpicChild("issue", 34, epic.CHILD_COMPLETED),
        epic.EpicChild("issue", 35, epic.CHILD_NOT_PLANNED),
        epic.EpicChild("issue", 36, epic.CHILD_COMPLETED),
    ]
    # The fake answers the two documented read families and nothing else.
    fake_run = _gh_fake(children, issues, {31: {"merged": True}})
    with pytest.raises(AssertionError):
        fake_run(["gh", "issue", "list"])
    with pytest.raises(AssertionError):
        fake_run(["gh", "api", "repos/o/r/milestones/7", "--include"])


def test_an_unmerged_pr_child_keeps_the_epic_open(monkeypatch):
    monkeypatch.setattr(seam, "run_command", _gh_fake(
        [_child(31, pr=True)], {31: {"pull_request": {}, "state": "open"}},
        {31: {"merged": False}},
    ))
    with pytest.raises(ValueError, match="child PR #31 is not merged"):
        epic.verify_epic_complete("o/r", EPIC)


def test_an_all_delivered_epic_keeps_todays_sentence_byte_for_byte():
    """Acceptance 1: the fully delivered Epic's comment must not move."""
    children = [epic.EpicChild("pr", 9, epic.CHILD_DELIVERED),
                epic.EpicChild("issue", 21, epic.CHILD_DELIVERED)]
    assert epic.epic_audit(children, None, "en") == (
        "Epic reconciliation: complete; children: PR #9 merged, "
        "Issue #21 closed; no open native blockers/dependencies."
    )
    assert epic.epic_audit(children, "v0.4.0", "en") == (
        "Epic reconciliation for v0.4.0: complete; children: PR #9 merged, "
        "Issue #21 closed; no open native blockers/dependencies."
    )
    versioned_zh = epic.epic_audit(children, "v0.4.0", "zh")
    assert versioned_zh == (
        "Epic 对账（v0.4.0）：已完成；子项：PR #9 merged, Issue #21 closed；"
        "没有未关闭的原生阻塞/依赖。"
    )


def test_a_person_closed_epic_lists_each_child_and_never_says_complete():
    """Acceptance 2: blocked-then-closed plus not planned never reads as
    "Orbi finished it", in either language."""
    children = [epic.EpicChild("issue", 21, epic.CHILD_BLOCKED),
                epic.EpicChild("issue", 22, epic.CHILD_NOT_PLANNED)]
    english = epic.epic_audit(children, None, "en")
    assert "complete" not in english
    assert english == (
        "Epic reconciliation: all children are closed, but not all of them "
        "were delivered by Orbi; children: Issue #21 closed while blocked "
        "(ai-blocked), Issue #22 closed by a person as not planned; "
        "no open native blockers/dependencies."
    )
    chinese = epic.epic_audit(children, None, "zh")
    assert "已完成" not in chinese
    assert chinese == (
        "Epic 对账：所有子项均已关闭，但并非全部由 Orbi 交付；子项："
        "Issue #21 在 ai-blocked 状态下关闭, Issue #22 由人关闭，标记为未计划；"
        "没有未关闭的原生阻塞/依赖。"
    )


def test_a_partly_delivered_epic_lists_the_delivery_and_the_person_close():
    """Acceptance 3: one delivered child and one closed as not planned are
    both named, and the sentence still does not claim completion."""
    children = [epic.EpicChild("pr", 9, epic.CHILD_DELIVERED),
                epic.EpicChild("issue", 22, epic.CHILD_NOT_PLANNED)]
    english = epic.epic_audit(children, "v0.4.0", "en")
    assert "complete" not in english
    assert "Epic reconciliation for v0.4.0: all children are closed" in english
    assert "PR #9 merged by Orbi" in english
    assert "Issue #22 closed by a person as not planned" in english
    chinese = epic.epic_audit(children, None, "zh")
    assert "PR #9 已由 Orbi 合并" in chinese
    assert "Issue #22 由人关闭，标记为未计划" in chinese


def test_a_child_delivered_as_an_issue_is_named_with_its_label():
    """Acceptance 4: an `ai-merged` child reads as Orbi's delivery."""
    children = [epic.EpicChild("issue", 32, epic.CHILD_DELIVERED),
                epic.EpicChild("issue", 34, epic.CHILD_COMPLETED)]
    english = epic.epic_audit(children, None, "en")
    assert "Issue #32 delivered by Orbi (ai-merged)" in english
    assert "Issue #34 closed by a person as completed" in english
    assert "complete;" not in english
    chinese = epic.epic_audit(children, None, "zh")
    assert "Issue #32 由 Orbi 交付（ai-merged）" in chinese
    assert "Issue #34 由人关闭，标记为已完成" in chinese


@pytest.mark.parametrize(("outcomes", "reason"), [
    ((epic.CHILD_DELIVERED, epic.CHILD_DELIVERED), None),
    ((epic.CHILD_DELIVERED, epic.CHILD_NOT_PLANNED), None),
    ((epic.CHILD_COMPLETED,), None),
    ((epic.CHILD_COMPLETED, epic.CHILD_BLOCKED), None),
    ((epic.CHILD_BLOCKED, epic.CHILD_NOT_PLANNED), "not planned"),
    ((epic.CHILD_NOT_PLANNED,), "not planned"),
])
def test_the_epic_close_reason_follows_the_child_outcomes(outcomes, reason):
    children = [epic.EpicChild("issue", 21 + index, outcome)
                for index, outcome in enumerate(outcomes)]
    assert epic.epic_close_reason(children) == reason


def test_the_audit_duplicate_check_matches_either_language():
    """Acceptance 5: after a failed close the next sweep posts no second
    comment, whichever language it renders."""
    children = [epic.EpicChild("issue", 21, epic.CHILD_BLOCKED),
                epic.EpicChild("issue", 22, epic.CHILD_NOT_PLANNED)]
    assert epic.epic_audit_posted(
        [{"body": "run\n" + epic.epic_audit(children, None, "en")}], children,
    )
    assert epic.epic_audit_posted(
        [{"body": epic.epic_audit(children, None, "zh")}], children,
    )
    assert not epic.epic_audit_posted([], children)
    assert not epic.epic_audit_posted(
        [{"body": "Epic #20 kept open: open blockers: #3"}], children,
    )
    # The version is part of the rendered sentence, and so is the outcome.
    assert not epic.epic_audit_posted(
        [{"body": epic.epic_audit(children, None, "en")}], children, "v0.4.0",
    )
    assert not epic.epic_audit_posted(
        [{"body": epic.epic_audit(
            [epic.EpicChild("issue", 21, epic.CHILD_DELIVERED)], None, "en",
        )}],
        children,
    )
    assert epic.epic_audit_posted(
        [{"body": epic.epic_audit(children, "v0.4.0", "zh")}],
        children, "v0.4.0",
    )


def test_the_closed_epic_evidence_names_each_outcome():
    children = [epic.EpicChild("pr", 9, epic.CHILD_DELIVERED),
                epic.EpicChild("issue", 21, epic.CHILD_BLOCKED)]
    assert epic.epic_closed_evidence(20, children) == (
        "Epic #20 closed after verification (#9 delivered; #21 blocked)"
    )


def test_verify_epic_complete_uses_native_sub_issue_shapes(monkeypatch):
    listed = {"number": 20, "blockedBy": {"nodes": []}}
    monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: {"number": "bad", "blockedBy": {"nodes": []}})
    with pytest.raises(ValueError, match="Epic number is missing"):
        epic.verify_epic_complete("o/r", listed)
    cases = [
        ({"number": "bad"}, "invalid number"),
        ({"number": 21, "repository": {}, "state": "closed"}, "repository state is malformed"),
        ({"number": 21, "repository": {"full_name": "other/r"}, "state": "closed"}, "cross-repository"),
        ({"number": 21, "repository": {"full_name": "o/r"}}, "state is malformed"),
    ]
    for child, message in cases:
        monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: listed)
        monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([[child]]))
        with pytest.raises(ValueError, match=message):
            epic.verify_epic_complete("o/r", listed)


def test_verify_epic_complete_rejects_malformed_native_blockers(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "number": 20, "body": "## Children\n- #21",
    }))
    with pytest.raises(ValueError, match="native blocker/dependency state is unavailable"):
        epic.verify_epic_complete("o/r", {"number": 20})


def test_verify_epic_complete_rejects_malformed_native_blocker_node():
    with pytest.raises(ValueError, match="native blocker/dependency state is malformed"):
        epic.verify_epic_complete("o/r", {
            "number": 20, "body": "## Children\n- #21",
            "blockedBy": {"nodes": [{"state": "OPEN"}]},
        })


def test_a_child_read_validates_the_live_issue_and_pr_shapes(monkeypatch):
    responses = {
        "repos/o/r/issues/1": json.dumps({"state": "closed",
                                          "labels": [{"name": "ai-merged"}]}),
        "repos/o/r/issues/2": json.dumps({"pull_request": {}, "state": "open"}),
        "repos/o/r/pulls/2": json.dumps({"merged": True}),
    }
    # The endpoint sits at command[2]; the adapter appends --include for the
    # response status (Issue #1760).
    monkeypatch.setattr(seam, "run_command",
                        lambda command, **kwargs: responses[command[2]])
    assert epic._epic_child("o/r", "issue", 1) == (
        epic.EpicChild("issue", 1, epic.CHILD_DELIVERED)
    )
    assert epic._epic_child("o/r", "pr", 2) == (
        epic.EpicChild("pr", 2, epic.CHILD_DELIVERED)
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(ValueError, match="not an object"):
        epic._epic_child("o/r", "issue", 3)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}))
    with pytest.raises(ValueError, match="declared as Issue"):
        epic._epic_child("o/r", "issue", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"state": "closed"}))
    with pytest.raises(ValueError, match="declared as PR"):
        epic._epic_child("o/r", "pr", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}) if "issues" in command[2] else json.dumps({"merged": False}))
    with pytest.raises(ValueError, match="not merged"):
        epic._epic_child("o/r", "pr", 2)
