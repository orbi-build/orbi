"""Issue #2303: the drafting session's validation entry.

The draft tools are TypeScript, but the cross-field rules are the engine's
own (`orbi.draft_document.parse_answer`: an `after` must name an EARLIER
Issue, a question carries at most one recommended option, ...). The tools run
this entry over the answer they are about to record, so a rule a TypeBox
schema cannot express comes back to the model as an error tool result and the
model corrects it in the same session, instead of the engine discarding a
finished draft after the session.

The entry is internal — the engine runs it as
`python -m orbi.draft_validate`; it is deliberately not an `orbi` subcommand
(it is not part of the CLI's public contract).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import orbi.draft_document as draft_document
import orbi.draft_validate as draft_validate

ROOT = Path(__file__).resolve().parent.parent

# A draft the engine creates from: one Issue with no earlier one.
VALID_DRAFT = {
    "kind": "draft",
    "reply": "Notes get tags.",
    "issues": [{"title": "Tag a note", "body": "## Outcome\n\nA user can tag."}],
}

# The production shape this Issue comes from: Issue 1 carries `after: 1`, a
# self-reference no earlier Issue answers.
SELF_REFERENCED = {
    "kind": "draft",
    "reply": "One Issue.",
    "issues": [
        {"title": "Tag a note", "body": "## Outcome\n\nA user can tag.",
         "after": 1},
    ],
}

# An ask_user answer with two recommended options.
TWO_RECOMMENDED = {
    "kind": "questions",
    "reply": "Which store?",
    "questions": [{
        "question": "Which store should the tags live in?",
        "options": [
            {"label": "A local file (Recommended)", "description": "One file."},
            {"label": "A service (Recommended)", "description": "Over the wire."},
        ],
    }],
}


def parse_reason(answer: dict) -> str:
    """The reason the ENGINE itself gives for this answer.

    Read from `parse_answer`, never restated in the test: the entry must
    carry the engine's own reason, not a copy of a rule.
    """
    with pytest.raises(draft_document.DraftError) as excinfo:
        draft_document.parse_answer(answer)
    return excinfo.value.detail


def run_entry(monkeypatch, capsys, text: str) -> tuple[int, str]:
    """One run of the entry over `text`, as the extension feeds it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = draft_validate.main()
    return code, capsys.readouterr().out


def run_module(answer: dict | str) -> subprocess.CompletedProcess:
    """The entry as the extension runs it: `<python> -m orbi.draft_validate`."""
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return subprocess.run(
        [sys.executable, "-m", "orbi.draft_validate"],
        input=text, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=False,
    )


def test_a_valid_draft_passes(monkeypatch, capsys):
    code, out = run_entry(monkeypatch, capsys, json.dumps(VALID_DRAFT))
    assert code == 0
    assert out == ""


def test_a_valid_question_answer_passes(monkeypatch, capsys):
    answer = {
        "kind": "questions",
        "reply": "Which store?",
        "questions": [{
            "question": "Which store should the tags live in?",
            "options": [
                {"label": "A local file (Recommended)",
                 "description": "Keeps everything in one file."},
                {"label": "A remote store",
                 "description": "Reads and writes over the network."},
            ],
        }],
    }
    code, out = run_entry(monkeypatch, capsys, json.dumps(answer))
    assert code == 0
    assert out == ""


def test_a_self_referenced_after_is_rejected_with_the_engine_reason(
    monkeypatch, capsys,
):
    code, out = run_entry(monkeypatch, capsys, json.dumps(SELF_REFERENCED))
    assert code == 1
    # The exact reason the engine gives when it parses the file after the
    # session: the same function, the same words.
    assert out.strip() == parse_reason(SELF_REFERENCED)
    assert "EARLIER" in out


def test_attachment_numbers_are_the_engines_own_rule(monkeypatch, capsys):
    """Issue #2364: an Issue's attachments are 1-based numbers."""
    valid = {
        "kind": "draft", "reply": "x",
        "issues": [{
            "title": "Tag a note", "body": "## Outcome\n\nA user can tag.",
            "attachments": [1, 2],
        }],
    }
    code, out = run_entry(monkeypatch, capsys, json.dumps(valid))
    assert code == 0, out
    invalid = {
        "kind": "draft", "reply": "x",
        "issues": [{
            "title": "Tag a note", "body": "## Outcome\n\nA user can tag.",
            "attachments": [0],
        }],
    }
    code, out = run_entry(monkeypatch, capsys, json.dumps(invalid))
    assert code == 1
    assert out.strip() == parse_reason(invalid)
    assert "attachments" in out


def test_two_recommended_options_are_rejected_with_the_engine_reason(
    monkeypatch, capsys,
):
    code, out = run_entry(monkeypatch, capsys, json.dumps(TWO_RECOMMENDED))
    assert code == 1
    assert out.strip() == parse_reason(TWO_RECOMMENDED)
    assert "recommended" in out


def test_an_unreadable_document_is_rejected_with_its_reason(monkeypatch, capsys):
    code, out = run_entry(monkeypatch, capsys, "{not json")
    assert code == 1
    assert out.strip()


def test_the_entry_runs_as_a_module():
    """The extension runs a command, not an imported function: the same entry
    must work as `python -m orbi.draft_validate` with the answer on stdin."""
    valid = run_module(VALID_DRAFT)
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == ""
    invalid = run_module(SELF_REFERENCED)
    assert invalid.returncode == 1
    assert invalid.stdout.strip() == parse_reason(SELF_REFERENCED)
