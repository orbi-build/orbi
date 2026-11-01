"""Failure report scene: the blocked comment and its evidence (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: tail/stdout text cleanup, the
failure summary, the structured failure scene, the blocked comment layout
and evidence (session log, test tail, coverage fence, truncation), and the
`process_issue` scenes that carry a session scene into that comment.

`_gh_api` (the shared fake for the progress publisher's `gh api` traffic)
is owned by `tests/test_bootstrap_runner_outcome.py` and imported here.
"""

from orbi import config as config_domain
import json
import logging
import subprocess
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.pi_session as pi_session
from orbi import failure
from tests.test_progress_wiring import make_fake_gh

from seam import seam
from tests.test_bootstrap_runner_outcome import _gh_api


def test_process_issue_failure_without_session_still_carries_scene(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pi died")),
    )
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    # No session file yet: the scene still carries the full debug entry
    # (worktree, branch) with '-' session fields.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body
    assert "session log: `<unavailable>`" in failure_body


def test_tail_text_strips_raw_and_caret_sgr_sequences(tmp_path):
    log = tmp_path / "coloured.log"
    log.write_bytes(
        b"\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m10 passed\x1b[39m\n"
        b"^[[32m\xe2\x9c\x93^[[39m tests/cta-ref.test.js\n"
    )

    result = failure_report._tail_text(log)

    assert "Test Files  10 passed" in result
    assert "tests/cta-ref.test.js" in result
    assert "\x1b[" not in result
    assert "^[" not in result


def test_tail_text_preserves_plain_text_byte_for_byte(tmp_path):
    log = tmp_path / "plain.log"
    content = b"plain output  \ntrailing whitespace\t"
    log.write_bytes(content)

    assert failure_report._tail_text(log) == content.decode()


def test_tail_text_preserves_non_sgr_lookalikes(tmp_path):
    log = tmp_path / "lookalikes.log"
    content = "^[[1] [32m literal \x1b without bracket"
    log.write_text(content, encoding="utf-8")

    assert failure_report._tail_text(log) == content


def test_failure_summary_removes_command_and_stderr_duplication():
    summary = failure.failure_summary(
        "Review failed: Command ['pi', '--session', '/home/runner/session'] "
        "returned non-zero exit status 1; stderr=provider exploded", language_code="en",
    )

    assert "Command [" not in summary
    assert "stderr=" not in summary
    assert "/home/" not in summary
    assert summary.count("provider exploded") == 1
    assert "delivery command failed" in summary
    assert failure.failure_summary(
        "provider exploded stderr=provider exploded", language_code="en"
    ) == "provider exploded"


def test_failure_summary_defaults_when_reason_has_no_readable_content():
    assert failure.failure_summary(" stderr=", language_code="en") == "the delivery command failed"


def test_failure_summary_uses_concrete_final_stderr_line_and_redacts_it():
    summary = failure.failure_summary(
        "the independent review failed: Command ['pi', '--session-dir', "
        "'/home/runner/session'] returned non-zero exit status 1. "
        "stderr=mise installed 0 tools\n"
        "\n"
        "provider log: /home/runner/private/session.jsonl\n"
        "Codex error: The usage limit has been reached", language_code="en",
    )

    assert summary == (
        "the independent review failed: the delivery command failed: "
        "Codex error: The usage limit has been reached"
    )
    assert "/home/" not in summary


def test_failure_scene_is_structured_and_redacts_local_paths():
    rendered = failure_report._failure_scene(
        {"session_id": "s1", "session_file": "/home/a/.pi-session/s.jsonl",
         "phase": "review", "last_activity": "2026-09-20T18:00:00Z",
         "action": "run tests", "result": "failed"},
        run_id="abcdef12", issue="orbi-build/orbi#1221", role="review",
        branch="orbi/task", worktree="/home/a/worktree", pr_url="https://github.com/o/r/pull/1", language_code="en",
    )

    assert "- run: `abcdef12`" in rendered
    assert "- worktree: `local runner worktree`" in rendered
    assert "- session log: `local session log`" in rendered
    assert "/home/" not in rendered
    assert "|" not in rendered


def test_failure_comment_layout_keeps_raw_evidence_collapsed():
    body = failure.failure_comment_body(
        outcome="fix_needed", action="Repair the delivery.",
        reason="the provider rejected the request",
        diagnosis="provider rejected the request",
        scene="- run: `abcdef12`\n- phase: `review`",
        evidence="\n\nstderr_tail:\n```\nprovider rejected the request\n```",
        pr_url="https://github.com/o/r/pull/1", issue="o/r#1221",
        run_id="abcdef12", language_code="en",
    )

    visible, collapsed = body.split("<details>", 1)
    assert "Orbi: fix needed — the engine will retry" in visible
    assert "provider rejected the request" in visible
    assert "stderr_tail:" not in visible
    assert "https://github.com/o/r/pull/1" in visible
    assert "run_id=abcdef12" in visible
    assert "stderr_tail:" in collapsed
    assert "```" in collapsed
    assert body.endswith("</details>")

    without_diagnosis = failure.failure_comment_body(
        outcome="fix_needed", action="", reason="provider failed",
        diagnosis="", scene="- run: `abcdef12`", evidence="",
        pr_url=None, issue="o/r#1221", run_id="abcdef12", language_code="en",
    )
    assert "failure detail:" not in without_diagnosis
    assert without_diagnosis.endswith("</details>")


def test_blocked_failure_suffix_stays_inside_closed_diagnostics(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        runner.UnrecoverableDeliveryError("cannot recover"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id="abcdef12", pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_REVIEW,
        classify=True, blocked_suffix="the preserved PR needs a decision",
        current_labels=set(), language_code="en",
    )

    assert posted[0].count("<details>") == 1
    assert posted[0].count("</details>") == 1
    assert "the preserved PR needs a decision\n</details>" in posted[0]


def test_no_run_id_failure_still_preserves_action_and_evidence(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        subprocess.CalledProcessError(2, ["pi"], stderr="provider failed"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id=None, pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_IMPLEMENT,
        action="Repair provider access.", reason="delivery failed",
        diagnosis="provider failed", classify=False, evidence=True,
        current_labels=set(), language_code="en",
    )

    assert "**Action:** Repair provider access." in posted[0]
    assert "stderr_tail:" in posted[0]
    assert "provider failed" in posted[0]
    # Issue #1684: the blocked diagnostic ends with the community help
    # line, after the closed evidence block.
    assert posted[0].endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_failure_evidence_handles_binary_streams_and_unavailable_files(tmp_path):
    error = subprocess.CalledProcessError(
        2, ["pi"], output=b"binary stdout", stderr=b"binary stderr",
    )

    evidence = failure_report._failure_evidence(tmp_path / "missing", error, language_code="en")

    assert "exit_code=2" in evidence
    assert "stderr_tail:\n```" in evidence
    assert "stdout_tail:\n```" in evidence
    assert "binary stderr" in evidence
    assert "binary stdout" in evidence
    # Issue #775: raw segments are fenced; a missing file stays a
    # placeholder inside its fence instead of a raw dump.
    assert "test_log_tail:\n```\n<unavailable>\n```" in evidence
    assert "session_last_events (last 20 records; full log: <unavailable>):" \
        in evidence
    assert failure_report._tail_text(tmp_path / "missing.log") == "<unavailable>"


def test_failure_evidence_includes_streams_session_and_test_tail(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    (worktree / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:00:00Z",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "review the PR"},
            ]},
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:01:00Z",
            "message": {"role": "toolResult", "toolName": "Bash",
                        "isError": False},
        }) + "\n",
        encoding="utf-8",
    )
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        "test output\n1 failed, 2 passed\n", encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"], output="stdout tail", stderr="")

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    assert "exit_code=1" in evidence
    assert "stdout tail" in evidence
    assert "1 failed, 2 passed" in evidence
    # Issue #775: the session tail is a structural summary, not raw JSONL.
    assert "2026-09-12T10:00:00Z message role=user content=text" in evidence
    assert "2026-09-12T10:01:00Z message role=toolResult tool=Bash" in evidence
    assert "full log: local session log" in evidence


def test_failure_evidence_keeps_coverage_table_literal_inside_fence(tmp_path):
    """Issue #775: the vitest coverage table must render as monospace
    literal text on GitHub, never parsed as a markdown table."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    raw_record = json.dumps({
        "type": "message", "timestamp": "t1",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "**bold** | not | a | table"},
        ]},
    })
    (worktree / ".pi-session" / "s.jsonl").write_text(raw_record + "\n")
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        " src      | stmts |\n"
        "----------|-------\n"
        " billing  | 96%   |\n",
        encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"])

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    # Every raw segment opens its fence directly under the label.
    for label in ("stderr_tail:", "stdout_tail:", "test_log_tail:"):
        assert f"\n{label}\n```" in evidence
    # The table separator line survives verbatim inside the test-log
    # fence (label -> fence -> table row).
    assert "test_log_tail:\n```\n src      | stmts |\n----------|-------\n" \
        in evidence
    # The session record text never enters the comment: the summary
    # carries the shape, the fence carries the log path.
    assert "bold" not in evidence
    assert raw_record not in evidence


def test_failure_evidence_dedupes_repeated_session_text_block(tmp_path):
    """Issue #775: pi logs the final assistant text twice (once inside
    the full message, once standalone); the summary keeps it once."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    final_text = "审查完成。结论：verdict pass"
    duplicated = [
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking"}, {"type": "text", "text": final_text},
         ]}},
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": final_text},
         ]}},
    ]
    (worktree / ".pi-session" / "s.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in duplicated),
        encoding="utf-8",
    )

    evidence = failure_report._failure_evidence(
        worktree, subprocess.CalledProcessError(1, ["pi"]), language_code="en",
    )

    assert evidence.count("role=assistant") == 1
    assert final_text not in evidence


def test_session_summary_skips_malformed_records_and_respects_limit(tmp_path):
    """Defensive edges: malformed lines, messages and blocks are skipped,
    the toolCall kind carries its name, the limit keeps the LAST records,
    and an all-unparsable log degrades to the placeholder."""
    good = {"type": "message", "timestamp": "t", "message": {
        "role": "assistant",
        "content": [{"type": "toolCall", "name": "Bash"}, "junk", 3],
    }}
    path = tmp_path / "s.jsonl"
    path.write_text(
        "not json\n"
        "[1, 2]\n"
        + json.dumps({"type": "x", "message": "not-a-dict"}) + "\n"
        + json.dumps(good) + "\n",
        encoding="utf-8",
    )

    assert failure_report._session_summary(path, limit=1) == (
        "t message role=assistant content=toolCall:Bash"
    )

    many = tmp_path / "many.jsonl"
    many.write_text("".join(
        json.dumps({"type": f"r{i}", "timestamp": f"t{i}"}) + "\n"
        for i in range(5)
    ), encoding="utf-8")
    assert failure_report._session_summary(many, limit=2) == "t3 r3\nt4 r4"

    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n[1]\n", encoding="utf-8")
    assert failure_report._session_summary(bad) == "<unparsable>"

    # An unreadable/missing file is the placeholder (also the race path
    # where the session vanishes between the glob and this read).
    assert failure_report._session_summary(tmp_path / "missing.jsonl") == "<unavailable>"


def test_failure_evidence_truncates_oversized_segment_with_note(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    evidence = failure_report._failure_evidence(
        worktree,
        subprocess.CalledProcessError(1, ["pi"], stderr="x" * 9000), language_code="en",
    )

    assert "[truncated, 5000 chars omitted]" in evidence


def test_report_delivery_failure_caps_comment_and_names_session_log(
    monkeypatch, tmp_path,
):
    """Issue #775 + #1221: an oversized raw cause becomes a bounded
    visible summary while its bounded diagnostic evidence remains reachable."""
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *args, **kwargs: set())
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    error = subprocess.CalledProcessError(1, ["pi"])
    worktree = tmp_path / "wt"
    session_file = worktree / ".pi-session" / "s.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n", encoding="utf-8")

    outcome = failure_report.report_delivery_failure(
        error, issue={"number": 775, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=worktree, branch="b", role=runner.ROLE_IMPLEMENT,
        cause="x" * 25000,
        classify=False, evidence=True, language_code="en",
    )

    assert outcome == "blocked"
    body = posted[0]
    # Issue #1322: the hidden machine-readable block leads the readable
    # hierarchy (the run marker is prepended by `report_delivery_failure`
    # when a run_id is bound; this call has none).
    assert body.startswith("<!-- orbi:failure:v1 ")
    assert "\n\nOrbi: blocked — waiting on a human decision" in body
    assert "**Reason:** " + "x" * 500 in body
    assert len(body) < failure_report.FAILURE_COMMENT_MAX_CHARS
    assert "full log: local session log" in body
    # Issue #1684: the blocked diagnostic ends with the community help
    # line; the body stays under the cap, so it is not truncated away.
    assert body.endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_process_issue_failure_comment_includes_session_scene(monkeypatch, tmp_path):
    calls = []
    (tmp_path / "wt" / ".pi-session").mkdir(parents=True)
    (tmp_path / "wt" / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-08-25T02:30:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "assistant event"},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "wt" / ".orbi").mkdir()
    (tmp_path / "wt" / ".orbi" / "test.log").write_text(
        "coverage output\n1 failed\n", encoding="utf-8",
    )
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="boom"),
        ),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-9",
        "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
        "phase": "test",
        "last_activity": "2026-08-25T02:30:00Z",
        "action": "bash pytest tests/",
        "result": "ok",
    })
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    assert "Orbi: blocked" in failure_body
    assert "session log: `local session log`" in failure_body
    assert "phase: `test`" in failure_body
    assert "last activity: `2026-08-25T02:30:00Z`" in failure_body
    assert "action: `bash pytest tests/`" in failure_body
    assert "result: `ok`" in failure_body
    assert "exit_code=1" in failure_body
    assert "stderr=boom" not in failure_body
    assert "stderr_tail:\n```\nboom\n```" in failure_body
    # Issue #775: the session tail appears as its structured summary
    # line inside a fence, not as raw session text.
    assert "2026-08-25T02:30:00Z message role=assistant content=text" \
        in failure_body
    assert "assistant event" not in failure_body
    assert "1 failed" in failure_body
    # The reader-facing scene uses stable labels for local paths.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body


def test_process_issue_isolates_scene_lookup_failure(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The main-path resume-context read (Issue #219) succeeds; the
    # failure-scene lookup is the one that dies on the disk error.
    snapshot_calls = []

    def flaky_snapshot(session_dir):
        snapshot_calls.append(1)
        if len(snapshot_calls) == 1:
            return None
        raise OSError("disk error")

    monkeypatch.setattr(failure_report, "activity_snapshot", flaky_snapshot)
    # The main-path scene read now lives in `pi_session.resume_context`
    # (Issue #1262): patch the same flaky snapshot there too so the
    # first (resume-context) call succeeds and the runner's
    # failure-scene lookup is the one that dies.
    monkeypatch.setattr(pi_session, "activity_snapshot", flaky_snapshot)
    with caplog.at_level("ERROR"):
        # Issue #239: the failure is terminal — `process_issue` returns
        # `None` instead of re-raising; the scene-isolation assertions
        # below are unchanged.
        assert runner.process_issue({"number": 9, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "git failed" in failure_body


def test_log_format_has_no_python_timestamp():
    # journald already provides time, host and process (Issue #40): the
    # Python logger must not print a second timestamp.
    formatter = logging.Formatter(runner.log_format())
    record = logging.LogRecord(
        "orbi.bootstrap", logging.INFO, "file", 1, "message", None, None,
    )
    assert formatter.format(record) == "INFO message"
