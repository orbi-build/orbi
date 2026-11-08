"""orbi draft (Issue #2041): one sentence into deliverable Issues, checked
before they are shown, split when the request is too big, and never bounced
by the pre-work check at claim time.

Pi and gh are stubbed through tests/seam.py: the drafting session and the
fidelity pass are replaced by queued answers (the engine's own parsing,
ordering and checking are what the tests drive), and gh is the in-memory
FakeGh, so the created Issues, their relations, labels and order are the
observable result.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

import orbi.clarify as clarify
import orbi.cli_commands as cli_commands
import orbi.draft as draft
import orbi.draft_cli as draft_cli
import orbi.draft_marker as draft_marker
from orbi.config import RunnerConfig
from orbi.delivery_labels import EPIC_LABEL, NEEDS_DETAIL_LABEL, READY_LABEL

from tests.fakes import draft as draft_fakes
from tests.fakes.github import FakeGh

REPO = "acme/app"
RUN_ID = "a1b2c3d4"
KEY = "draft-abc123"

OUTCOME = "## Outcome\n\nA user can tag a note."
ACCEPTANCE = "## Acceptance\n\n- Adding a tag makes the note findable."


def issue(title: str, outcome: str = OUTCOME, after: int | None = None) -> dict:
    body = f"{outcome}\n\n{ACCEPTANCE}"
    entry: dict = {"title": title, "body": body}
    if after is not None:
        entry["after"] = after
    return entry


TWO_ISSUES = [
    issue("Tag a note"),
    issue("Filter notes by tag", outcome="## Outcome\n\nA user can filter.",
          after=1),
]
THREE_ISSUES = [
    issue("Tag a note"),
    issue("Filter notes by tag", outcome="## Outcome\n\nA user can filter.",
          after=1),
    issue("Export a note", outcome="## Outcome\n\nA user can export.",
          after=2),
]

FIDELITY_RULES = "Fidelity: remove what the person did not ask for."

# Two options in the shape a question carries (Issue #2136), the recommended
# one first and marked the way the hosted page keys on it.
TWO_OPTIONS = [
    {"label": "A local store (Recommended)",
     "description": "Keeps everything in one file."},
    {"label": "A remote store",
     "description": "Reads and writes through a server."},
]


# --- the fixtures --------------------------------------------------------

@pytest.fixture
def gh() -> FakeGh:
    return FakeGh(REPO)


@pytest.fixture
def recorder(monkeypatch, gh):
    return draft_fakes.install_gh(monkeypatch, gh)


@pytest.fixture
def sessions(monkeypatch):
    return draft_fakes.install_sessions(monkeypatch)


@pytest.fixture
def config(tmp_path, recorder) -> RunnerConfig:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    deploy = tmp_path / "deploy"
    rules = deploy / draft.FIDELITY_REL
    rules.parent.mkdir(parents=True)
    rules.write_text(FIDELITY_RULES, encoding="utf-8")
    return RunnerConfig(
        repo_dir=checkout, deploy_home=deploy, workspace_root=tmp_path,
        source_repos=(REPO,),
    )


def run_round_cli(config, payload: dict, session_dir: Path):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, session_dir=session_dir,
        stdin=io.StringIO(json.dumps(payload)),
        stdout=out, stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def run_create(config, payload: dict):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, create=True,
        stdin=io.StringIO(json.dumps(payload)),
        stdout=out, stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_the_answer_tools_the_allowlist_names_are_the_shipped_ones():
    """The allowlist may only name tools the shipped extension registers."""
    source = draft.DRAFT_EXTENSION.read_text(encoding="utf-8")
    for name in (draft.WRITE_DRAFT_TOOL, draft.ASK_USER_TOOL):
        assert f'"{name}"' in source
        assert name in draft.draft_system_prompt()


# --- 6 + 10: one criteria text, the check's prompt unchanged --------------

def test_the_draft_prompt_carries_the_checks_own_criteria_text():
    prompt = draft.draft_system_prompt()
    assert clarify.TICKET_CHECK_CRITERIA in prompt
    assert clarify.TICKET_CHECK_CRITERIA in clarify.CLARIFY_SYSTEM_PROMPT
    # The three identifiers the check answers with travel with the text.
    for identifier in ("observable_result", "acceptance_condition",
                       "single_outcome"):
        assert identifier in prompt


def test_the_pre_work_check_prompt_is_byte_identical_after_sharing():
    """Sharing the criteria must not reword the gate (Issue #2041)."""
    digest = hashlib.sha256(
        clarify.CLARIFY_SYSTEM_PROMPT.encode("utf-8"),
    ).hexdigest()
    assert digest == (
        "755f4a3be1b4209c73ea95ab29448de915ee859f04534700a39960a979933646"
    )


# --- 1 + 2: the round ----------------------------------------------------

def test_a_round_that_splits_a_request_returns_several_issues(
    config, sessions, monkeypatch, tmp_path,
):
    judged = draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config, {"message": "notes need tags"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert [entry["title"] for entry in answer["issues"]] == [
        "Tag a note", "Filter notes by tag",
    ]
    for entry in answer["issues"]:
        assert "## Outcome" in entry["body"]
        assert "## Acceptance" in entry["body"]
    assert answer["issues"][1]["after"] == 1
    assert answer["check"] == "passed"
    assert len(judged) == 2


def test_a_forward_after_reference_is_refused(config, sessions, monkeypatch,
                                             tmp_path):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(issue("First", after=2), issue("Second"))
    code, out, err = run_round_cli(
        config, {"message": "x"}, tmp_path / "session",
    )
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "invalid_draft"
    assert "EARLIER" in failure["detail"]


def test_a_round_that_only_asks_questions_returns_no_issues(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions("Which store?", "Which language?")
    code, out, err = run_round_cli(
        config, {"message": "add tags"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["issues"] == []
    assert [
        question["question"] for question in answer["questions"]
    ] == ["Which store?", "Which language?"]
    assert answer["questions"][0]["header"] == "Choice"
    assert "check" not in answer


# --- 3: the session resumes; a replay still drafts -----------------------

def test_the_second_round_resumes_the_same_session(config, sessions,
                                                   monkeypatch, tmp_path):
    draft_fakes.judge_passing(monkeypatch)
    session_dir = tmp_path / "session"
    sessions.queue_questions("Which store?")
    run_round_cli(config, {"message": "add tags"}, session_dir)
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config,
        {
            "message": "sqlite",
            "conversation": [
                {"role": "person", "text": "add tags"},
                {"role": "orbi", "text": "Which store?"},
            ],
        },
        session_dir,
    )
    assert code == 0, err
    first, second = sessions.calls
    assert first.session_root == second.session_root == session_dir.resolve()
    assert first.resume is False
    assert second.resume is True
    # The replayed conversation reaches the session, not just the prompt.
    assert "Which store?" in second.context
    assert "sqlite" in second.context


def test_a_lost_session_still_drafts_from_the_replayed_conversation(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    payload = {
        "message": "sqlite",
        "conversation": [
            {"role": "person", "text": "add tags"},
            {"role": "orbi", "text": "Which store?"},
        ],
        "draft": {"issues": TWO_ISSUES},
    }
    code, out, err = run_round_cli(config, payload, tmp_path / "fresh")
    assert code == 0, err
    call = sessions.calls[0]
    assert call.resume is False
    assert json.loads(out)["issues"][0]["title"] == "Tag a note"
    assert "add tags" in call.context


# --- 10b: the check, exactly one rewrite ---------------------------------

def test_a_failing_draft_gets_exactly_one_rewrite(config, sessions,
                                                  monkeypatch, tmp_path):
    draft_fakes.judge_sequence(monkeypatch, [
        (False, ("single_outcome",)),
        (True, ()),
        (True, ()),
    ])
    sessions.queue_draft(issue("One big thing"))
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config, {"message": "everything"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["check"] == "passed"
    assert len(answer["issues"]) == 2
    # Exactly two drafting sessions: the first draft and its ONE rewrite.
    assert len(sessions.calls) == 2
    assert "single_outcome" in sessions.calls[1].context


def test_the_reply_describes_the_rewritten_draft(config, sessions,
                                                 monkeypatch, tmp_path):
    """The one sentence the person reads is the rewrite's own."""
    draft_fakes.judge_sequence(monkeypatch, [
        (False, ("single_outcome",)),
        (True, ()),
        (True, ()),
    ])
    sessions.queue_draft(
        issue("One big thing"), reply="One Issue will change X.",
    )
    sessions.queue_draft(*TWO_ISSUES, reply="Two Issues will change X.")
    code, out, err = run_round_cli(
        config, {"message": "everything"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["issues"][0]["title"] == "Tag a note"
    assert answer["reply"] == "Two Issues will change X."


def test_a_draft_that_still_fails_reports_the_failed_checks(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_failing(monkeypatch, "single_outcome")
    sessions.queue_draft(issue("One big thing"))
    sessions.queue_draft(issue("Still one big thing"))
    code, out, err = run_round_cli(
        config, {"message": "everything"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["check"] == "single_outcome"
    assert answer["issues"][0]["title"] == "Still one big thing"
    assert len(sessions.calls) == 2


def test_an_unavailable_check_fails_open(config, sessions, monkeypatch,
                                         tmp_path):
    draft_fakes.judge_unavailable(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    assert json.loads(out)["check"] == "passed"


# --- the fidelity pass ---------------------------------------------------

def test_an_empty_fidelity_pass_returns_the_draft_with_a_warning(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append("no JSON here")
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["warning"] == "fidelity_pass_empty"
    assert len(answer["issues"]) == 2


# Issue #2085: the pass only revises titles and bodies; the number of
# Issues and every `after` link stay exactly as the drafter wrote them.
def test_a_fidelity_answer_that_moves_after_still_revises_the_draft(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append(json.dumps({"issues": [
        issue("Tag a note, revised", after=1),
        issue("Filter notes, revised"),
    ]}))
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert "warning" not in answer
    assert [entry["title"] for entry in answer["issues"]] == [
        "Tag a note, revised", "Filter notes, revised",
    ]
    # The drafter's order survives the pass's copy of `after`.
    assert [entry.get("after") for entry in answer["issues"]] == [None, 1]


def test_a_fidelity_answer_with_another_issue_count_keeps_the_draft(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append(json.dumps({
        "issues": [issue("Tag a note, revised")],
    }))
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["warning"] == "fidelity_pass_empty"
    assert answer["issues"] == TWO_ISSUES


def test_a_failed_fidelity_pass_never_fails_the_round(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append(RuntimeError("boom"))
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    assert json.loads(out)["warning"] == "fidelity_pass_failed: RuntimeError"


# Issue #2050: the pass removes what the person did not ask for, so it
# must hear everything the person asked for, not only the latest message.
EARLIER_ROUND = (
    "add one line at the end of the `summary` output with the total "
    "record count"
)
ANSWER_ROUND = "1. Which records count? → payments only"
ANSWER_CONVERSATION = [
    {"role": "person", "text": EARLIER_ROUND},
    {"role": "orbi", "text": "Shall I count every record, or only payments?"},
]
EARLIER_ROUND_ISSUE = {
    "title": "Count the records",
    "body": (
        "## Outcome\n\n" + EARLIER_ROUND + "\n\n## Acceptance\n\n"
        "- payments only\n- the existing output lines stay where they are"
    ),
}


def test_the_fidelity_pass_hears_every_person_turn(
    config, sessions, monkeypatch, tmp_path,
):
    """The answer alone is not the request: the pass gets the earlier turn."""
    draft_fakes.judge_passing(monkeypatch)
    draft_fakes.fidelity_echoes_request(monkeypatch, sessions)
    sessions.queue_draft(issue("Count the records"))
    code, out, err = run_round_cli(
        config,
        {"message": ANSWER_ROUND, "conversation": ANSWER_CONVERSATION},
        tmp_path / "session",
    )
    assert code == 0, err
    request = json.loads(out)["issues"][0]["body"]
    assert EARLIER_ROUND in request
    assert "payments only" in request
    # Orbi's own question is not the person's words.
    assert "count every record" not in request


def test_an_outcome_from_an_earlier_round_survives_the_fidelity_pass(
    config, sessions, monkeypatch, tmp_path,
):
    """The pass strips lines the request does not name; the earlier person
    turn is part of the request, so its outcome stays in the draft."""
    draft_fakes.judge_passing(monkeypatch)
    draft_fakes.fidelity_removes_unmentioned_lines(monkeypatch, sessions)
    sessions.queue_draft(EARLIER_ROUND_ISSUE)
    code, out, err = run_round_cli(
        config,
        {"message": ANSWER_ROUND, "conversation": ANSWER_CONVERSATION},
        tmp_path / "session",
    )
    assert code == 0, err
    body = json.loads(out)["issues"][0]["body"]
    assert EARLIER_ROUND in body
    assert "payments only" in body
    # What the person never asked for is still removed.
    assert "stay where they are" not in body


def test_a_first_round_gives_the_fidelity_pass_the_message_alone(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    run_round_cli(config, {"message": "notes"}, tmp_path / "session")
    payload = json.loads(sessions.fidelity_calls[0].context)
    assert payload["request"] == "notes"


@pytest.mark.parametrize("conversation", [
    None,
    "junk",
    [],
    ["junk", {"role": "orbi", "text": "Which store?"},
     {"role": "person", "text": "   "}],
])
def test_a_conversation_without_person_words_passes_the_message(
    config, sessions, monkeypatch, tmp_path, conversation,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    run_round_cli(
        config,
        {"message": "notes", "conversation": conversation},
        tmp_path / "session",
    )
    payload = json.loads(sessions.fidelity_calls[0].context)
    assert payload["request"] == "notes"


def test_a_person_turn_without_a_role_is_still_the_person(
    config, sessions, monkeypatch, tmp_path,
):
    """The turn shape `render_context` already treats as the person's."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    run_round_cli(
        config,
        {"message": "sqlite", "conversation": [{"text": "add tags"}]},
        tmp_path / "session",
    )
    request = json.loads(sessions.fidelity_calls[0].context)["request"]
    assert "add tags" in request
    assert request.endswith("sqlite")


def test_the_fidelity_pass_uses_the_shipped_rules(config, sessions,
                                                  monkeypatch, tmp_path):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    run_round_cli(config, {"message": "notes"}, tmp_path / "session")
    call = sessions.fidelity_calls[0]
    assert call.system_prompt.startswith(FIDELITY_RULES)
    assert call.session_root == (
        (tmp_path / "session").resolve() / draft.FIDELITY_DIR
    )


# --- 9: failures ---------------------------------------------------------

def test_a_failed_round_prints_code_step_detail(config, sessions, tmp_path):
    sessions.queue((None, subprocess.TimeoutExpired(cmd=["pi"], timeout=1)))
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 1
    assert out == ""
    failure = json.loads(err)
    assert failure["code"] == "timeout"
    assert failure["step"] == "run the drafting session"
    assert "TimeoutExpired" in failure["detail"]


def test_a_round_that_timed_out_after_writing_the_draft_returns_it(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue((
        {"kind": "draft", "reply": "It will change X.",
         "issues": TWO_ISSUES},
        subprocess.TimeoutExpired(cmd=["pi"], timeout=1),
    ))
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    assert len(json.loads(out)["issues"]) == 2


def test_broken_stdin_fails_fast(config, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, session_dir=tmp_path / "s",
        stdin=io.StringIO("not json"), stdout=out, stderr=err,
    )
    assert code == 1
    assert json.loads(err.getvalue())["code"] == "invalid_input"


# --- 4: creation ---------------------------------------------------------

def test_create_makes_three_issues_one_edge_and_labels_last(
    config, recorder, gh,
):
    code, out, err = run_create(
        config, {"key": KEY, "issues": THREE_ISSUES},
    )
    assert code == 0, err
    created = json.loads(out)["created"]
    assert len(created) == 3
    assert [gh.issues[n]["title"] for n in created] == [
        "Tag a note", "Filter notes by tag", "Export a note",
    ]
    # Exactly one blockedBy edge, and it is the third waiting for the second.
    assert gh.issues[created[2]]["blocked_by"] == [
        {"number": created[1], "state": "OPEN"},
    ]
    assert gh.issues[created[0]]["blocked_by"] == []
    for number in created:
        assert gh.issues[number]["labels"] == [READY_LABEL]
    commands = [" ".join(c) for c in recorder.issues_commands()]
    for number in created:
        label_at = next(
            index for index, command in enumerate(commands)
            if command.startswith(
                f"gh issue edit {number} --repo {REPO} --add-label",
            )
        )
        create_at = next(
            index for index, command in enumerate(commands)
            if command.startswith(
                f"gh issue create --repo {REPO} --title "
                f"{gh.issues[number]['title']}",
            )
        )
        assert create_at < label_at
    third = created[2]
    edge_at = next(
        index for index, command in enumerate(commands)
        if command == (
            f"gh issue edit {third} --repo {REPO} --add-blocked-by "
            f"{created[1]}"
        )
    )
    label_at = next(
        index for index, command in enumerate(commands)
        if command.startswith(
            f"gh issue edit {third} --repo {REPO} --add-label",
        )
    )
    assert edge_at < label_at


def test_create_twice_creates_nothing_new(config, recorder, gh):
    payload = {"key": KEY, "issues": THREE_ISSUES}
    first = json.loads(run_create(config, payload)[1])["created"]
    second = json.loads(run_create(config, payload)[1])["created"]
    assert second == first
    assert len(gh.issues) == 3


def test_a_different_draft_key_creates_its_own_issues(config, recorder, gh):
    run_create(config, {"key": "one", "issues": TWO_ISSUES})
    run_create(config, {"key": "two", "issues": TWO_ISSUES})
    assert len(gh.issues) == 4


def test_a_marker_from_another_account_is_never_adopted(config, recorder, gh):
    run_create(config, {"key": KEY, "issues": TWO_ISSUES})
    for entry in gh.issues.values():
        entry["author"] = {"login": "app/someone-else"}
    created = json.loads(
        run_create(config, {"key": KEY, "issues": TWO_ISSUES})[1],
    )["created"]
    assert len(created) == 2
    assert len(gh.issues) == 4


# --- 5: adding detail to an existing Issue -------------------------------

def test_create_with_issue_appends_once_and_queues_the_ticket(
    config, recorder, gh,
):
    gh.add_issue(7, title="Existing", body="## Request\n\nAdd tags.",
                 labels=(NEEDS_DETAIL_LABEL,))
    payload = {"key": KEY, "issue": 7, "issues": [TWO_ISSUES[0]]}
    code, out, err = run_create(config, payload)
    assert code == 0, err
    assert json.loads(out)["created"] == [7]
    body_after_first = gh.issues[7]["body"]
    assert TWO_ISSUES[0]["body"] in body_after_first
    assert gh.issues[7]["labels"] == [READY_LABEL]
    assert json.loads(run_create(config, payload)[1])["created"] == [7]
    assert gh.issues[7]["body"] == body_after_first


def test_create_with_issue_and_several_issues_builds_the_split(
    config, recorder, gh,
):
    gh.add_issue(9, title="Everything", body="## Request\n\nEverything.",
                 labels=(NEEDS_DETAIL_LABEL,))
    payload = {"key": KEY, "issue": 9, "issues": THREE_ISSUES}
    code, out, err = run_create(config, payload)
    assert code == 0, err
    created = json.loads(out)["created"]
    assert len(created) == 3
    for number in created:
        assert gh.issues[number]["parent"] == 9
        assert READY_LABEL in gh.issues[number]["labels"]
    assert gh.issues[created[2]]["blocked_by"] == [
        {"number": created[1], "state": "OPEN"},
    ]
    assert EPIC_LABEL in gh.issues[9]["labels"]
    assert NEEDS_DETAIL_LABEL not in gh.issues[9]["labels"]
    assert json.loads(run_create(config, payload)[1])["created"] == created
    assert len(gh.issues) == 4


# --- 8: the milestone the repository policy selects ----------------------

def test_created_issues_get_the_policy_milestone(config, recorder, gh):
    gh.add_milestone(3, title="v1.2.3")
    gh.set_repo_config('active_milestone = "v1.2.3"\n')
    run_create(config, {"key": KEY, "issues": TWO_ISSUES})
    assert {entry["milestone"] for entry in gh.issues.values()} == {3}


def test_a_closed_policy_milestone_is_not_attached(config, recorder, gh):
    gh.add_milestone(3, title="v1.2.3", state="closed")
    gh.set_repo_config('active_milestone = "v1.2.3"\n')
    run_create(config, {"key": KEY, "issues": TWO_ISSUES})
    assert {entry["milestone"] for entry in gh.issues.values()} == {None}


# --- 7: interactive ------------------------------------------------------

def numbers(text: str) -> list[str]:
    return re.findall(r"^#(\d+)$", text, re.MULTILINE)


def test_interactive_answers_edits_and_confirms(config, sessions, monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions("Which store?")
    sessions.queue_draft(*TWO_ISSUES)
    sessions.queue_draft(*TWO_ISSUES)
    stdin = io.StringIO("sqlite\nmake the tag optional\n\n")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags to notes",
        stdin=stdin, stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    text = out.getvalue()
    assert "Which store?" in text
    assert "1. Tag a note" in text
    assert len(numbers(text)) == 2
    # answer, edit, confirm: the edit reached the drafter as a message.
    assert "make the tag optional" in sessions.calls[2].context


def test_interactive_declining_creates_nothing(config, sessions, monkeypatch,
                                               gh):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO("n\n"),
        stdout=out, stderr=err,
    )
    assert code == 0
    assert "Nothing was created." in out.getvalue()
    assert gh.issues == {}


def test_interactive_leaves_no_session_behind(config, sessions, monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    before = set(Path(tempfile.gettempdir()).glob("orbi-draft-*"))
    draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO("\n"),
        stdout=io.StringIO(), stderr=io.StringIO(),
    )
    after = set(Path(tempfile.gettempdir()).glob("orbi-draft-*"))
    assert after - before == set()
    # The session it ran in no longer exists.
    assert not sessions.calls[0].session_root.exists()


# --- the marker ----------------------------------------------------------

def test_the_marker_fingerprint_tracks_the_created_body():
    body = "## Outcome\n\nx"
    created = draft_marker.with_marker(body, key=KEY, position=2)
    mark = draft_marker.parse_draft_marker(created)
    assert mark is not None
    assert (mark.key, mark.position) == (KEY, 2)
    assert mark.fingerprint == draft_marker.draft_fingerprint(created)
    assert draft_marker.draft_fingerprint(created) == (
        draft_marker.draft_fingerprint(body)
    )
    assert draft_marker.draft_fingerprint(
        created + "\nmore",
    ) != mark.fingerprint


# --- 10c: the claim-time skip --------------------------------------------

def _draft_issue(body: str, author: str = "app/orbi-bot") -> dict:
    return {
        "number": 7, "title": "Drafted", "body": body,
        "author": {"login": author},
    }


def test_an_unchanged_draft_is_not_stopped(config, monkeypatch):
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_failing(monkeypatch, "single_outcome")
    body = draft_marker.with_marker(TWO_ISSUES[0]["body"], key=KEY, position=1)
    assert clarify.enforce(_draft_issue(body), config, REPO, RUN_ID) is True
    assert judged == []


def test_an_edited_draft_is_checked(config, monkeypatch):
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_sequence(monkeypatch)
    body = draft_marker.with_marker(TWO_ISSUES[0]["body"], key=KEY, position=1)
    assert clarify.enforce(
        _draft_issue(body + "\n\nA person added a line."), config, REPO,
        RUN_ID,
    ) is True
    assert len(judged) == 1


def test_a_marker_on_another_accounts_issue_is_checked(config, monkeypatch):
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_sequence(monkeypatch)
    body = draft_marker.with_marker(TWO_ISSUES[0]["body"], key=KEY, position=1)
    assert clarify.enforce(
        _draft_issue(body, author="app/someone-else"), config, REPO, RUN_ID,
    ) is True
    assert len(judged) == 1


# --- the invalid-input contract ------------------------------------------

@pytest.mark.parametrize(
    "payload, code, fragment",
    [
        ([1, 2], "invalid_input", "JSON object"),
        ({}, "invalid_input", "message"),
        ({"message": "   "}, "invalid_input", "message"),
    ],
)
def test_a_round_refuses_an_unusable_document(
    config, sessions, tmp_path, payload, code, fragment,
):
    code_value, out, err = run_round_cli(config, payload, tmp_path / "s")
    assert code_value == 1
    assert out == ""
    failure = json.loads(err)
    assert failure["code"] == code
    assert fragment in failure["detail"]


@pytest.mark.parametrize(
    "issues, fragment",
    [
        ([], "no Issues"),
        ("nope", "no Issues"),
        ([{"body": "b"}], "no title"),
        ([{"title": "  ", "body": "b"}], "no title"),
        ([{"title": "a\nb", "body": "b"}], "multi-line"),
        ([{"title": "a", "body": " "}], "no body"),
        ([{"title": "a", "body": "b", "after": 0}], "EARLIER"),
        ([{"title": "a", "body": "b", "after": True}], "EARLIER"),
        ([{"title": "a", "body": "b"}, "junk"], "not an object"),
    ],
)
def test_a_create_payload_is_validated_before_anything_is_written(
    config, recorder, issues, fragment,
):
    code, out, err = run_create(config, {"key": KEY, "issues": issues})
    assert code == 1
    assert out == ""
    failure = json.loads(err)
    assert failure["code"] == "invalid_draft"
    assert fragment in failure["detail"]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"issues": TWO_ISSUES}, "key must match"),
        ({"key": "bad key!", "issues": TWO_ISSUES}, "key must match"),
        ({"key": KEY, "issue": 0, "issues": TWO_ISSUES},
         "issue must be an Issue number"),
        ({"key": KEY, "issue": "7", "issues": TWO_ISSUES},
         "issue must be an Issue number"),
    ],
)
def test_a_create_payload_names_its_own_bad_fields(
    config, recorder, payload, fragment,
):
    code, out, err = run_create(config, payload)
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "invalid_input"
    assert fragment in failure["detail"]


@pytest.mark.parametrize(
    "answer, fragment",
    [
        (123, "no answer"),
        ({"kind": "draft", "issues": TWO_ISSUES}, "no reply"),
        ({"kind": "questions", "reply": "x", "questions": "a"}, "no questions"),
        ({"kind": "questions", "reply": "x", "questions": []},
         "no questions"),
        ({"kind": "questions", "reply": "x", "questions": ["Which store?"]},
         "question 1 is not an object"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "options": TWO_OPTIONS,
        }]}, "question 1 has no header"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": " ", "header": "Store", "options": TWO_OPTIONS,
        }]}, "question 1 has no question"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": TWO_OPTIONS[:1],
        }]}, "needs 2 to 4 options"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": TWO_OPTIONS * 3,
        }]}, "needs 2 to 4 options"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store", "options": "both",
        }]}, "needs 2 to 4 options"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": ["Local", TWO_OPTIONS[1]],
        }]}, "option 1 is not an object"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": [{"description": "Keeps it local."}, TWO_OPTIONS[1]],
        }]}, "option 1 has no label"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": [{"label": "Local"}, TWO_OPTIONS[1]],
        }]}, "option 1 has no description"),
        ({"kind": "questions", "reply": "x", "questions": [{
            "question": "Which store?", "header": "Store",
            "options": [TWO_OPTIONS[1], TWO_OPTIONS[0]],
        }]}, "recommended option must come first"),
        ({"kind": "weird", "reply": "x"}, "unknown answer kind"),
    ],
)
def test_an_unusable_session_answer_is_reported(
    config, sessions, monkeypatch, tmp_path, answer, fragment,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue(answer)
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "no_answer"
    assert fragment in failure["detail"]


def test_a_broken_answer_file_is_a_failed_round(tmp_path):
    path = tmp_path / "answer.json"
    with pytest.raises(draft.DraftError) as excinfo:
        draft.read_answer(path)
    assert excinfo.value.code == "no_answer"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(draft.DraftError):
        draft.read_answer(path)


def test_empty_stdin_fails_fast(config, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, session_dir=tmp_path / "s",
        stdin=io.StringIO("   \n"), stdout=out, stderr=err,
    )
    assert code == 1
    assert json.loads(err.getvalue())["detail"] == "stdin is empty"


def test_a_conversation_without_text_is_rendered_as_its_role(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    run_round_cli(
        config,
        {
            "message": "notes",
            "conversation": ["junk", {"role": "orbi", "text": "  "}],
        },
        tmp_path / "session",
    )
    assert "- orbi: " not in sessions.calls[0].context


def test_two_issues_failing_the_same_check_report_it_once(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_failing(monkeypatch, "acceptance_condition")
    sessions.queue_draft(*TWO_ISSUES)
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    assert json.loads(out)["check"] == "acceptance_condition"


# --- the real session runners -------------------------------------------

def test_the_drafting_session_is_read_only_and_resumable(
    config, monkeypatch, tmp_path,
):
    calls = draft_fakes.stub_stream_pi(monkeypatch)
    root = tmp_path / "session"
    out_path = root / "answer.json"
    draft.run_draft_session(
        config, REPO, session_root=root, system_prompt="prompt",
        context="context", run_id=RUN_ID, out_path=out_path, resume=True,
    )
    command = calls[0]["command"]
    assert command[0] == "pi"
    assert command[1] == "--tools"
    assert command[2] == draft.DRAFT_TOOLS
    # Pi's --tools REPLACES the whole selection and applies to extension
    # tools too (pi --help), so the two answer tools must be named in the
    # allowlist: without them the session has no tool to answer with and
    # every round fails "no_answer" on the real path.
    assert draft.WRITE_DRAFT_TOOL in command[2].split(",")
    assert draft.ASK_USER_TOOL in command[2].split(",")
    assert "--no-extensions" in command
    assert str(draft.DRAFT_EXTENSION) in command
    assert "--continue" in command
    assert calls[0]["pi_env"][draft.DRAFT_OUT_ENV] == str(out_path)
    assert (root / draft.DRAFT_SESSION_DIR / "system-prompt.md").is_file()
    # A first round does not resume anything.
    draft.run_draft_session(
        config, REPO, session_root=root, system_prompt="prompt",
        context="context", run_id=RUN_ID, out_path=out_path,
    )
    assert "--continue" not in calls[1]["command"]


def test_the_drafting_session_uses_the_configured_agent_dir(
    config, monkeypatch, tmp_path,
):
    calls = draft_fakes.stub_stream_pi(monkeypatch)
    agent = tmp_path / "agent"
    draft_fakes.stub_agent_dir(monkeypatch, agent)
    draft.run_draft_session(
        config, REPO, session_root=tmp_path / "s", system_prompt="p",
        context="c", run_id=RUN_ID, out_path=tmp_path / "a.json",
    )
    assert calls[0]["pi_env"]["PI_CODING_AGENT_DIR"] == str(agent)


def test_the_fidelity_session_is_tool_less(config, monkeypatch, tmp_path):
    calls = draft_fakes.stub_stream_pi(monkeypatch)
    output = draft.run_fidelity_session(
        config, REPO, session_root=tmp_path / "fidelity",
        system_prompt="rules", context="{}", run_id=RUN_ID,
    )
    assert output == ""
    command = calls[0]["command"]
    assert "--no-tools" in command
    assert "--no-extensions" in command
    assert (
        tmp_path / "fidelity" / draft.DRAFT_SESSION_DIR / "system-prompt.md"
    ).is_file()


def test_the_fidelity_session_uses_the_configured_agent_dir(
    config, monkeypatch, tmp_path,
):
    calls = draft_fakes.stub_stream_pi(monkeypatch)
    agent = tmp_path / "agent"
    draft_fakes.stub_agent_dir(monkeypatch, agent)
    draft.run_fidelity_session(
        config, REPO, session_root=tmp_path / "fidelity",
        system_prompt="rules", context="{}", run_id=RUN_ID,
    )
    assert calls[0]["pi_env"]["PI_CODING_AGENT_DIR"] == str(agent)


# --- the fidelity pass never fails the round ----------------------------

@pytest.mark.parametrize(
    "output, warning",
    [
        ("no json at all", "fidelity_pass_empty"),
        ("{broken", "fidelity_pass_empty"),
        ('{"issues": []}', "fidelity_pass_empty"),
        ("{}", "fidelity_pass_empty"),
        ('{"issues": "junk"}', "fidelity_pass_empty"),
        ('{"issues": ["junk"]}', "fidelity_pass_empty"),
    ],
)
def test_an_unusable_fidelity_answer_keeps_the_draft(
    config, sessions, monkeypatch, tmp_path, output, warning,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append(output)
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["warning"] == warning
    assert len(answer["issues"]) == 2


def test_a_fidelity_answer_may_be_wrapped_in_text(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    revised = json.dumps({"issues": TWO_ISSUES})
    sessions.fidelity.append(f"Here is the revised draft:\n{revised}\nDone.")
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert "warning" not in answer
    assert answer["issues"] == TWO_ISSUES


def test_a_missing_fidelity_file_is_only_a_warning(
    tmp_path, recorder, sessions, monkeypatch,
):
    draft_fakes.judge_passing(monkeypatch)
    config = RunnerConfig(
        repo_dir=tmp_path / "checkout", deploy_home=tmp_path / "deploy",
        workspace_root=tmp_path, source_repos=(REPO,),
    )
    sessions.queue_draft(*TWO_ISSUES)
    code, out, err = run_round_cli(
        config, {"message": "notes"}, tmp_path / "session",
    )
    assert code == 0, err
    assert json.loads(out)["warning"].startswith(
        "fidelity_pass_unavailable:",
    )
    assert sessions.fidelity_calls == []


# --- a rewrite that does not answer keeps the judged draft --------------

def test_a_failed_rewrite_session_keeps_the_first_draft(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_failing(monkeypatch, "single_outcome")
    sessions.queue_draft(issue("One big thing"))
    sessions.queue((None, RuntimeError("boom")))
    code, out, err = run_round_cli(
        config, {"message": "everything"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["issues"][0]["title"] == "One big thing"
    assert answer["warning"].startswith("rewrite_failed:")


def test_a_rewrite_that_asks_a_question_keeps_the_first_draft(
    config, sessions, monkeypatch, tmp_path,
):
    draft_fakes.judge_failing(monkeypatch, "single_outcome")
    sessions.queue_draft(issue("One big thing"))
    sessions.queue_questions("Which part first?")
    code, out, err = run_round_cli(
        config, {"message": "everything"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["warning"] == "rewrite_failed: no_draft"
    assert answer["issues"][0]["title"] == "One big thing"


# --- creation refuses what it cannot adopt ------------------------------

def test_a_marker_for_another_key_is_not_adopted(config, recorder, gh):
    body = draft_marker.with_marker(
        TWO_ISSUES[0]["body"], key="other-key", position=1,
    )
    gh.add_issue(50, title="Tag a note", body=body, author="app/orbi-bot")
    created = json.loads(
        run_create(config, {"key": KEY, "issues": TWO_ISSUES})[1],
    )["created"]
    assert 50 not in created
    assert len(created) == 2


# --- the command boundary ------------------------------------------------

def test_creation_failures_are_reported_with_their_step(
    config, sessions, monkeypatch,
):
    from orbi.ticket_command import TicketCommandError

    draft_fakes.stub_create_error(
        monkeypatch, TicketCommandError("create the drafted Issue", "boom"),
    )
    code, out, err = run_create(config, {"key": KEY, "issues": TWO_ISSUES})
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "create_failed"
    assert failure["step"] == "create the drafted Issue"


def test_an_unexpected_error_is_reported_not_swallowed(
    config, sessions, monkeypatch,
):
    draft_fakes.stub_create_error(monkeypatch, RuntimeError("boom"))
    code, out, err = run_create(config, {"key": KEY, "issues": TWO_ISSUES})
    assert code == 1
    failure = json.loads(err)
    assert failure["code"] == "draft_failed"
    assert "RuntimeError: boom" in failure["detail"]


# --- a default answer is an assumption, not a question (Issue #2133) ----

DRAFT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "draft"


def model_answer(name: str) -> dict:
    """One model answer, as the shipped extension hands it to the engine.

    `font-professional.en.json` is the issue author's own real model run.
    `empty-request.zh.json` keeps that run's three questions and carries the
    option shape Issue #2136 requires, written by hand: the pre-#2136 model
    could not emit options, and the tool's typed parameters now enforce the
    shape on the model. The extension adds the `kind` the engine reads, and
    the round document's `check` field is not a tool argument.
    """
    fixture = json.loads((DRAFT_FIXTURES / name).read_text(encoding="utf-8"))
    kind = "draft" if fixture["issues"] else "questions"
    return {
        "kind": kind,
        "reply": fixture["reply"],
        "questions": fixture.get("questions", []),
        "issues": fixture["issues"],
    }


def assumptions_of(body: str) -> list[str]:
    """The bullet lines of the Issue's `Assumptions` section."""
    section = re.search(r"^## Assumptions\n(.*?)(?=^## |\Z)", body,
                        re.MULTILINE | re.DOTALL)
    assert section, f"the Issue body carries no Assumptions section: {body}"
    return [
        line.strip() for line in section.group(1).splitlines()
        if line.strip().startswith("-")
    ]


def test_a_real_answer_with_a_default_choice_drafts_with_assumptions(
    config, sessions, monkeypatch, tmp_path,
):
    """The 2026-11-04 Cloud request ("make the font more professional") is
    drafted without a question: the chosen typeface and its reason land in
    the Issue's Assumptions section."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue(model_answer("font-professional.en.json"))
    code, out, err = run_round_cli(
        config, {"message": "Can you make the font more professional"},
        tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["questions"] == []
    assert len(answer["issues"]) == 1
    body = answer["issues"][0]["body"]
    lines = assumptions_of(body)
    assert "Inter" in lines[0]
    assert "recommended" not in body.lower()


def test_a_real_answer_to_an_empty_request_asks_with_an_option_shape(
    config, sessions, monkeypatch, tmp_path,
):
    """A request that says too little is the one case that asks: three
    questions, each carrying a header and 2–4 options the person can pick,
    a recommended one first — the object shape the hosted page reads
    (Issue #2136)."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue(model_answer("empty-request.zh.json"))
    code, out, err = run_round_cli(
        config, {"message": "帮我改一下"}, tmp_path / "session",
    )
    assert code == 0, err
    answer = json.loads(out)
    assert answer["issues"] == []
    assert len(answer["questions"]) == 3
    for question in answer["questions"]:
        assert set(question) == {"question", "header", "options"}
        assert question["question"].strip()
        assert question["header"].strip()
        options = question["options"]
        assert 2 <= len(options) <= 4
        for option in options:
            assert set(option) == {"label", "description"}
            assert option["label"].strip()
            assert option["description"].strip()
        marked = [
            position for position, option in enumerate(options)
            if option["label"].endswith(draft.RECOMMENDED_MARK)
        ]
        assert marked, "the fixture marks a recommended option"
        assert marked == [0], "the recommended option comes first"


def test_the_drafting_rules_teach_the_option_shape():
    """The prompt teaches the shape the hosted page renders: a header,
    2–4 options and a recommended one first. The orbi#2133 "how" (a bare
    question with no suggested answer) is replaced; its "when" stays, with
    every other disagreement recorded under Assumptions."""
    prompt = draft.draft_system_prompt()
    assert draft.RECOMMENDED_MARK in prompt
    assert "header" in prompt
    assert "2 to 4 options" in prompt
    assert "the person's own answer" in prompt
    assert "no suggested answer" not in prompt
    assert "Assumptions" in prompt
    # The marker is data the model writes, never a string the CLI hardcodes.
    cli_source = Path(draft_cli.__file__).read_text(encoding="utf-8")
    assert "recommended" not in cli_source.lower()


# --- one rule in both places: Assumptions only with a decision (Issue #2148) -

# The engine's drafting rules and the write-ai-ready-issue skill state the
# same rule: a decision taken instead of asking is one line under
# Assumptions, and an Issue body with no decision carries no Assumptions
# section.
ASSUMPTIONS_ONLY_WITH_A_DECISION = (
    "An Assumptions section appears only when you took at least one decision"
)


def test_the_drafting_rules_write_assumptions_only_when_a_decision_was_taken():
    """Issue #2148: the prompt no longer asks for an Assumptions section in
    every Issue body, matching the skill's non-empty rule."""
    prompt = draft.draft_system_prompt()
    assert ASSUMPTIONS_ONLY_WITH_A_DECISION in prompt
    assert "Every Issue body carries an Assumptions section" not in prompt


# --- the interactive loop ------------------------------------------------

class Tty(io.StringIO):
    """A stdin that reports itself as a terminal (it echoes its own input)."""

    def isatty(self) -> bool:
        return True


def test_interactive_without_a_request_asks_for_one(config):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, stdin=io.StringIO(""), stdout=out, stderr=err,
    )
    assert code == 1
    assert "no request was given" in err.getvalue()


def test_interactive_reads_the_request_from_a_terminal(config, sessions,
                                                       monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, stdin=Tty("add tags\nn\n"), stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    assert "What should Orbi build?" in out.getvalue()
    assert "add tags" in sessions.calls[0].context


def test_interactive_stops_when_the_answers_end(config, sessions,
                                                monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions("Which store?")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO(""),
        stdout=out, stderr=err,
    )
    assert code == 0
    assert "Nothing was created." in out.getvalue()


def test_interactive_stops_when_the_confirmation_ends(config, sessions,
                                                      monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(*TWO_ISSUES)
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO(""),
        stdout=out, stderr=err,
    )
    assert code == 0
    assert "Nothing was created." in out.getvalue()


def test_interactive_sends_the_typed_answers_to_the_next_round(
    config, sessions, monkeypatch,
):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions("Which store?")
    sessions.queue_draft(*TWO_ISSUES)
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags",
        stdin=io.StringIO("sqlite, and yes to the rest\nn\n"),
        stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    assert "sqlite, and yes to the rest" in sessions.calls[1].context


def test_interactive_creates_nothing_when_the_answers_are_empty(
    config, sessions, monkeypatch,
):
    """No recommended answers exist to accept with a bare Enter (Issue #2133):
    an empty answer is no answer, so no round is drafted from it."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions("Which store?")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO("\n"),
        stdout=out, stderr=err,
    )
    assert code == 0
    assert "Nothing was created." in out.getvalue()
    assert len(sessions.calls) == 1


def test_interactive_shows_the_check_and_the_warning(
    config, sessions, monkeypatch,
):
    draft_fakes.judge_failing(monkeypatch, "single_outcome")
    sessions.queue_draft(*TWO_ISSUES)
    sessions.queue_draft(*TWO_ISSUES)
    sessions.fidelity.append("no json")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO("n\n"),
        stdout=out, stderr=err,
    )
    assert code == 0, err.getvalue()
    text = out.getvalue()
    assert "check: single_outcome" in text
    assert "warning: fidelity_pass_empty" in text


def test_interactive_fails_fast_when_the_draft_never_settles(
    config, sessions, monkeypatch,
):
    draft_fakes.judge_passing(monkeypatch)
    for _ in range(draft.MAX_ROUNDS):
        sessions.queue_questions("Which store?")
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags",
        stdin=io.StringIO("sqlite\n" * draft.MAX_ROUNDS),
        stdout=out, stderr=err,
    )
    assert code == 1
    assert "too_many_rounds" in err.getvalue()


# --- the claim-time skip needs a readable login -------------------------

def test_an_unreadable_login_still_judges_the_ticket(config, monkeypatch):
    draft_fakes.stub_login_error(monkeypatch)
    judged = draft_fakes.judge_sequence(monkeypatch)
    body = draft_marker.with_marker(TWO_ISSUES[0]["body"], key=KEY, position=1)
    assert clarify.enforce(_draft_issue(body), config, REPO, RUN_ID) is True
    assert len(judged) == 1


def test_an_empty_login_still_judges_the_ticket(config, monkeypatch):
    draft_fakes.stub_login(monkeypatch, "")
    judged = draft_fakes.judge_sequence(monkeypatch)
    body = draft_marker.with_marker(TWO_ISSUES[0]["body"], key=KEY, position=1)
    assert clarify.enforce(_draft_issue(body), config, REPO, RUN_ID) is True
    assert len(judged) == 1


def test_the_marker_needs_text_and_a_marker():
    """No body text and no marker line both mean "not a drafted body"."""
    assert draft_marker.parse_draft_marker(None) is None
    assert draft_marker.parse_draft_marker("## Outcome\n\nx") is None


def test_a_body_quoting_the_phrase_is_not_this_drafts_issue(
    config, recorder, gh,
):
    quoted = ("A note quoting orbi:draft:v1:" + KEY + " as prose."
              "\n\n" + draft_marker.with_marker(
                  TWO_ISSUES[0]["body"], key="other-key", position=1,
              ))
    gh.add_issue(50, title="Tag a note", body=quoted, author="app/orbi-bot")
    created = json.loads(
        run_create(config, {"key": KEY, "issues": TWO_ISSUES})[1],
    )["created"]
    assert 50 not in created
    assert len(created) == 2


def test_interactive_prints_no_empty_reply(config, sessions, monkeypatch):
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue({
        "kind": "questions", "reply": "",
        "questions": [draft_fakes.structured_question("Q?")],
    })
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="add tags", stdin=io.StringIO(""),
        stdout=out, stderr=err,
    )
    assert code == 0
    assert out.getvalue().startswith("Q1")


def test_interactive_prints_each_option_under_its_question(
    config, sessions, monkeypatch,
):
    """The person sees the choices, recommended one first, under the
    question they answer in typed text (Issue #2136)."""
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_questions({
        "question": "What should the other person get?",
        "header": "Sharing",
        "options": [
            {"label": "A read-only link (Recommended)",
             "description": "Opens the list, cannot change it."},
            {"label": "An editable link",
             "description": "Lets whoever holds it change the list."},
        ],
    })
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, request="share my notes", stdin=io.StringIO(""),
        stdout=out, stderr=err,
    )
    assert code == 0
    text = out.getvalue()
    assert "Q1 — What should the other person get?" in text
    question_at = text.index("Q1 —")
    first_at = text.index(
        "A read-only link (Recommended): Opens the list, cannot change it.",
    )
    second_at = text.index(
        "An editable link: Lets whoever holds it change the list.",
    )
    assert question_at < first_at < second_at
    assert "Nothing was created." in text


def test_the_cli_handler_dispatches_to_the_command(
    config, capsys, tmp_path,
):
    """The dispatch table entry is the command, and it reports a failure
    through the same document (the CLI boundary, not the library)."""
    import argparse
    from types import SimpleNamespace

    args = SimpleNamespace(
        request=None, session_dir=tmp_path / "s", create=False, repo=None,
    )
    code = cli_commands.command_draft(
        config, args, argparse.ArgumentParser(),
    )
    assert code == 1
    assert json.loads(capsys.readouterr().err)["code"]

def test_an_issue_without_a_marker_is_checked(config, monkeypatch):
    draft_fakes.stub_login(monkeypatch)
    judged = draft_fakes.judge_sequence(monkeypatch)
    assert clarify.enforce(
        _draft_issue("## Outcome\n\nno marker here"), config, REPO, RUN_ID,
    ) is True
    assert len(judged) == 1