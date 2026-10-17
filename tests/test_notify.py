"""Telegram "needs you" notifications (Issue #1673).

The user outcome: a person who hands Issues to Orbi receives ONE Telegram
message when Orbi NEWLY puts a task into a state that waits for them
(ai-blocked, ai-needs-detail, ai-awaiting-merge), naming the state,
owner/repo#N <title> and the Issue link. "orbi notify test" checks the
setup. A label a person adds by hand sends nothing.

Evidence is the WIRE: every test points the adapter at a REAL local
http.server (tests/fakes/telegram.py) and asserts the requests it
received — the same urllib transport the person's bot endpoint sees.
Unit tests alone would not prove the real user path.

Notifications are a pure bypass: no failure here changes a label, a
comment or a delivery's exit.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbi import (
    clarify,
    config as config_domain,
    github,
    journal,
    notify,
    runner_health,
)
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    EVENT_AWAITING_MERGE,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_HUMAN_REVIEW_WAITING,
    NEEDS_DETAIL_LABEL,
)
from seam import seam

from tests.fakes.telegram import (
    TelegramEndpoint,
    bind_notify,
    point_at,
    raise_timeout_url_error,
)

# A token-shaped value (9 digits + 35 url-safe chars). It is NOT a real
# bot token: the tests always point the adapter at 127.0.0.1.
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
CHAT_ID = "-1001234567890"
RUN_ID = "a1b2c3d4"


def config_for(*, chat_id: str | None = CHAT_ID) -> config_domain.RunnerConfig:
    return config_domain.RunnerConfig(telegram_chat_id=chat_id)


@dataclass
class HostLanguageConfig:
    """The host config `message_language` shape #1660 adds to RunnerConfig.

    `RunnerConfig` does not carry the field yet (Issue #1660 owns it), so
    the adapter reads it defensively with `getattr`; this stand-in lets
    the tests pin both languages before that field lands.
    """

    telegram_chat_id: str | None = CHAT_ID
    message_language: str = "en"
    repo_dir: Path = Path(".")
    source_repos: tuple[str, ...] = ()


def bind(monkeypatch, *, config=None, token=TOKEN) -> None:
    """Bind one tick's config and token, like notify.tick_start."""
    bind_notify(
        monkeypatch, config if config is not None else config_for(), token,
    )


# The one community-help line a problem message ends with (Issue
# #1684), from the `orbi.language` catalog.
HELP_EN = (
    "Need help? Join the Orbi community: https://t.me/+kJRTTpFGBRU0M2Y9"
)
HELP_ZH = "需要帮助？加入 Orbi 用户群：https://t.me/+kJRTTpFGBRU0M2Y9"


def blocked_text(title: str = "Fix the thing", *,
                 repo: str = "o/r", number: int = 7) -> str:
    return (
        "Orbi · Needs your attention: Orbi is stuck and needs you\n"
        f"{repo}#{number} {title}\n"
        f"https://github.com/{repo}/issues/{number}\n"
        f"{HELP_EN}"
    )


# --- adapter wire contract -------------------------------------------------


def test_send_posts_the_documented_request_and_reports_success(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        assert notify.send("hello") is True
        assert endpoint.requests[0]["path"] == f"/bot{TOKEN}/sendMessage"
        assert endpoint.requests[0]["content_type"] == "application/json"
        assert endpoint.messages() == [{"chat_id": CHAT_ID, "text": "hello"}]


@pytest.mark.parametrize("status,body,reason", [
    (401, b'{"ok": false, "description": "Unauthorized"}', "token_invalid"),
    (429, b'{"ok": false, "description": "Too Many Requests"}', "rate_limited"),
    (500, b'{"ok": false, "description": "Server Error"}', "platform_error"),
    (503, b'{"ok": false, "description": "Unavailable"}', "platform_error"),
    (400, b'{"ok": false, "description": "Bad Request"}', "rejected"),
    (403, b'{"ok": false, "description": "Forbidden"}', "rejected"),
    (200, b'{"ok": false, "error_code": 400}', "rejected"),
    (200, b"not json at all", "rejected"),
])
def test_send_classifies_each_status_from_the_body(
    monkeypatch, caplog, status, body, reason,
):
    with TelegramEndpoint(status=status, body=body) as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        caplog.set_level("INFO")
        assert notify.send("hello") is False
        assert endpoint.requests  # the request really reached the endpoint
        assert f"notify_failed reason={reason}" in caplog.text
        assert TOKEN not in caplog.text


def test_send_reports_timeout_when_the_endpoint_does_not_answer_in_time(
    monkeypatch, caplog,
):
    with TelegramEndpoint(delay=0.5) as endpoint:
        endpoint.point(monkeypatch, timeout=0.15)
        bind(monkeypatch)
        caplog.set_level("INFO")
        assert notify.send("hello") is False
        assert "notify_failed reason=timeout" in caplog.text


def test_send_reports_network_error_when_the_endpoint_is_unreachable(
    monkeypatch, caplog,
):
    point_at(monkeypatch, "http://127.0.0.1:1", timeout=1.0)
    bind(monkeypatch)
    caplog.set_level("INFO")
    assert notify.send("hello") is False
    assert "notify_failed reason=network_error" in caplog.text


def test_send_classifies_a_wrapped_timeout_as_a_timeout(
    monkeypatch, caplog,
):
    """urllib reports a do_open timeout as URLError(reason=TimeoutError)."""
    bind(monkeypatch)
    raise_timeout_url_error(monkeypatch)
    caplog.set_level("INFO")
    assert notify.send("hello") is False
    assert "notify_failed reason=timeout" in caplog.text


class BoomConfig:
    """A config whose chat-id read raises: the bypass swallows it."""

    @property
    def telegram_chat_id(self):
        raise RuntimeError("broken config")

    deploy_home = Path("/deploy")


class BoomText:
    """A value whose string rendering raises while a message is built."""

    def __str__(self):
        raise RuntimeError("broken value")


def test_public_functions_swallow_a_broken_config(monkeypatch, capsys):
    bind(monkeypatch)
    notify.tick_start(BoomConfig())
    assert notify.enabled() is False
    assert notify.doctor_lines(BoomConfig()) == []
    assert notify.test_command(BoomConfig()) == 1
    assert "notify_failed reason=network_error" in capsys.readouterr().out


def test_label_added_swallows_a_message_build_failure(monkeypatch):
    bind(monkeypatch)
    notify.label_added(BoomText(), 7, BLOCKED_LABEL, "t")


def test_patch_applied_swallows_a_message_build_failure(monkeypatch):
    """The moved decision keeps the pure-bypass guard: a broken value
    while building the message never escapes (Issue #1684)."""
    bind(monkeypatch)
    notify.patch_applied(
        EVENT_BLOCKED, [BLOCKED_LABEL], {"ai-ready"},
        repo=BoomText(), number=7, read_title=lambda: "t",
    )


def test_send_is_not_configured_reports_the_missing_setting(
    monkeypatch, caplog,
):
    bind(monkeypatch, config=config_for(chat_id=None), token=None)
    caplog.set_level("INFO")
    assert notify.send("hello") is False
    assert "notify_failed reason=not_configured" in caplog.text
    assert "telegram_chat_id" in caplog.text


# --- fixed message text ----------------------------------------------------


@pytest.mark.parametrize("label,status_en,status_zh,help_line", [
    (BLOCKED_LABEL, "Orbi is stuck and needs you", "Orbi 卡住了，需要您处理",
     True),
    (NEEDS_DETAIL_LABEL, "Orbi needs more detail", "需要您补充信息", True),
    (AWAITING_MERGE_LABEL, "Waiting for you to approve the merge",
     "等您批准合并", False),
])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_needs_you_message_is_the_fixed_text(
    monkeypatch, label, status_en, status_zh, help_line, language,
):
    if language == "zh":
        expected = (
            f"Orbi · 需要您处理：{status_zh}\n"
            "o/r#7 Fix the thing\n"
            "https://github.com/o/r/issues/7"
        )
        if help_line:
            expected += f"\n{HELP_ZH}"
    else:
        expected = (
            f"Orbi · Needs your attention: {status_en}\n"
            "o/r#7 Fix the thing\n"
            "https://github.com/o/r/issues/7"
        )
        if help_line:
            expected += f"\n{HELP_EN}"
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch, config=HostLanguageConfig(
            message_language=language,
        ))
        notify.label_added("o/r", 7, label, "Fix the thing")
        assert endpoint.texts() == [expected]


def test_message_language_defaults_to_english_without_the_key(monkeypatch):
    """A host config without #1660's key sends the English text."""
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch, config=config_for())
        notify.label_added("o/r", 7, BLOCKED_LABEL, "Fix the thing")
        assert endpoint.texts() == [blocked_text()]


def test_notify_test_uses_the_host_message_language(monkeypatch, capsys):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        assert notify.test_command(HostLanguageConfig(message_language="zh")) == 0
        assert capsys.readouterr().out.strip() == "ok"
        assert endpoint.texts() == [
            "Orbi 测试通知\n"
            "这条消息确认您的接收配置可用，之后的通知会是这个样子。"
        ]


def test_label_added_is_silent_for_a_non_attention_label(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        notify.label_added("o/r", 7, "ai-in-progress", "t")
        assert endpoint.requests == []


def test_label_added_is_silent_when_notifications_are_off(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch, config=config_for(chat_id=None), token=None)
        notify.label_added("o/r", 7, BLOCKED_LABEL, "t")
        assert endpoint.requests == []


# --- github.apply_label_patch hook -----------------------------------------


def test_apply_label_patch_sends_once_for_a_new_attention_label(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        monkeypatch.setattr(
            seam, "issue_view", lambda *a, **k: {"title": "Fix the thing"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_BLOCKED,
            current_labels={"ai-ready"},
        )
        assert endpoint.texts() == [blocked_text()]
        # Applying the same patch while ai-blocked is already present is
        # silent: label_patch still returns the add label.
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_BLOCKED,
            current_labels={"ai-blocked"},
        )
        assert endpoint.texts() == [blocked_text()]


def test_apply_label_patch_notifies_awaiting_merge(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        monkeypatch.setattr(
            seam, "issue_view", lambda *a, **k: {"title": "Ready to merge"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_AWAITING_MERGE,
            current_labels={"ai-ready", "ai-pr-opened"},
        )
        assert endpoint.texts() == [
            "Orbi · Needs your attention: "
            "Waiting for you to approve the merge\n"
            "o/r#7 Ready to merge\n"
            "https://github.com/o/r/issues/7"
        ]


def test_apply_label_patch_notifies_human_review_waiting_once(monkeypatch):
    """Issue #1684: the human-review hold sends ONE message with the wait
    status and the help line; the next tick (ai-ready already present)
    sends nothing."""
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        monkeypatch.setattr(
            seam, "issue_view", lambda *a, **k: {"title": "Review me"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_HUMAN_REVIEW_WAITING,
            current_labels={"ai-pr-opened"},
        )
        assert endpoint.texts() == [
            "Orbi · Needs your attention: Waiting for your review\n"
            "o/r#7 Review me\n"
            "https://github.com/o/r/issues/7\n"
            f"{HELP_EN}"
        ]
        # The first wait already added ai-ready; the next tick sees it and
        # the caller skips the patch, so nothing is sent again.
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_HUMAN_REVIEW_WAITING,
            current_labels={"ai-pr-opened", "ai-ready"},
        )
        assert len(endpoint.texts()) == 1


def test_human_review_waiting_message_follows_the_language(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch, config=HostLanguageConfig(message_language="zh"))
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        monkeypatch.setattr(
            seam, "issue_view", lambda *a, **k: {"title": "复核我"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_HUMAN_REVIEW_WAITING,
            current_labels={"ai-pr-opened"},
        )
        assert endpoint.texts() == [
            "Orbi · 需要您处理：等您人工验收\n"
            "o/r#7 复核我\n"
            "https://github.com/o/r/issues/7\n"
            f"{HELP_ZH}"
        ]


def test_apply_label_patch_does_not_notify_a_non_attention_add(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        reads = []
        monkeypatch.setattr(
            seam, "issue_view",
            lambda *a, **k: reads.append(a) or {"title": "t"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_CLAIM, current_labels=(),
        )
        assert endpoint.requests == []
        assert reads == []


def test_apply_label_patch_sends_nothing_when_the_label_write_fails(
    monkeypatch,
):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("gh write failed")

        monkeypatch.setattr(seam, "edit_issue", boom)
        with pytest.raises(RuntimeError, match="gh write failed"):
            github.apply_label_patch(
                7, repo="o/r", event=EVENT_BLOCKED,
                current_labels={"ai-ready"},
            )
        assert endpoint.requests == []


def test_apply_label_patch_sends_without_a_title_when_the_read_fails(
    monkeypatch,
):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)

        def boom(*args, **kwargs):
            raise RuntimeError("gh issue view failed")

        monkeypatch.setattr(seam, "issue_view", boom)
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_BLOCKED,
            current_labels={"ai-ready"},
        )
        assert endpoint.texts() == [
            "Orbi · Needs your attention: Orbi is stuck and needs you\n"
            "o/r#7\n"
            "https://github.com/o/r/issues/7\n"
            f"{HELP_EN}"
        ]


def test_apply_label_patch_makes_no_github_read_when_notifications_are_off(
    monkeypatch,
):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch, config=config_for(chat_id=None), token=None)
        monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
        reads = []
        monkeypatch.setattr(
            seam, "issue_view",
            lambda *a, **k: reads.append(a) or {"title": "t"},
        )
        github.apply_label_patch(
            7, repo="o/r", event=EVENT_BLOCKED,
            current_labels={"ai-ready"},
        )
        assert reads == []
        assert endpoint.requests == []


# --- clarify.enforce hook --------------------------------------------------


def _thin_verdict() -> clarify.ClarifyVerdict:
    return clarify.ClarifyVerdict(
        passed=False, missing=("observable_result",),
    )


def _record_writes(monkeypatch):
    comments: list = []
    edits: list = []
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)),
    )
    monkeypatch.setattr(
        seam, "edit_issue",
        lambda number, *, repo, add=None, remove=None: edits.append(
            (number, repo, add, remove)),
    )
    return comments, edits


def test_clarify_stop_sends_one_needs_detail_message(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(
            seam, "judge_issue_body", lambda *a, **k: _thin_verdict(),
        )
        comments, edits = _record_writes(monkeypatch)
        assert clarify.enforce(
            {"number": 7, "title": "Thin ticket"}, config_for(),
            "o/r", RUN_ID,
        ) is False
        assert len(comments) == 1 and len(edits) == 1
        assert endpoint.texts() == [
            "Orbi · Needs your attention: Orbi needs more detail\n"
            "o/r#7 Thin ticket\n"
            "https://github.com/o/r/issues/7\n"
            f"{HELP_EN}"
        ]


def test_clarify_stop_still_stops_when_the_endpoint_is_down(
    monkeypatch, caplog,
):
    point_at(monkeypatch, "http://127.0.0.1:1", timeout=1.0)
    bind(monkeypatch)
    monkeypatch.setattr(
        seam, "judge_issue_body", lambda *a, **k: _thin_verdict(),
    )
    comments, edits = _record_writes(monkeypatch)
    caplog.set_level("INFO")
    assert clarify.enforce(
        {"number": 7, "title": "Thin ticket"}, config_for(),
        "o/r", RUN_ID,
    ) is False
    assert len(comments) == 1 and len(edits) == 1
    assert "notify_failed reason=network_error" in caplog.text


def test_clarify_stop_sends_nothing_when_the_write_fails(monkeypatch):
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        bind(monkeypatch)
        monkeypatch.setattr(
            seam, "judge_issue_body", lambda *a, **k: _thin_verdict(),
        )
        _record_writes(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("gh write failed")

        monkeypatch.setattr(seam, "edit_issue", boom)
        assert clarify.enforce(
            {"number": 7, "title": "Thin ticket"}, config_for(),
            "o/r", RUN_ID,
        ) is True
        assert endpoint.requests == []


# --- tick_start / half-configuration ---------------------------------------


def test_tick_start_reports_a_missing_token_and_sends_nothing(
    monkeypatch, caplog,
):
    monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        caplog.set_level("INFO")
        notify.tick_start(config_for())
        assert "notify_failed reason=not_configured" in caplog.text
        assert notify.TOKEN_ENV in caplog.text
        assert endpoint.requests == []


def test_tick_start_reports_an_empty_token(monkeypatch, caplog):
    monkeypatch.setenv(notify.TOKEN_ENV, "")
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        caplog.set_level("INFO")
        notify.tick_start(config_for())
        assert "notify_failed reason=not_configured" in caplog.text
        assert endpoint.requests == []


def test_tick_start_without_a_chat_id_is_silent(monkeypatch, caplog):
    monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
    caplog.set_level("INFO")
    notify.tick_start(config_for(chat_id=None))
    assert "notify_failed" not in caplog.text


def test_tick_start_binds_the_token_for_the_label_hook(monkeypatch):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config_for())
        assert notify.enabled() is True
        notify.label_added("o/r", 7, BLOCKED_LABEL, "t")
        assert len(endpoint.texts()) == 1


def test_the_tick_binds_the_notifier_at_preflight(monkeypatch, tmp_path):
    """The real tick wires the notifier: runner.main -> _preflight.

    Without notify.tick_start(config) in the tick's preflight the label
    hook never sees a chat id, so no message is ever sent: the user
    outcome ("Orbi blocks Issue N: the endpoint gets one message")
    depends on this one call site.
    """
    from orbi import claim, runner

    (tmp_path / "prompts").mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / "prompts" / name).write_text("prompt", encoding="utf-8")
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["o/r"]\n'
        f'telegram_chat_id = "{CHAT_ID}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency,
               active_milestone=None, **_kw: None,
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        assert runner.main(["--config", str(config_path)]) == 0
        # Bound by the tick's own preflight, never by this test: the
        # label hook sends with no argument of its own.
        notify.label_added("o/r", 7, BLOCKED_LABEL, "t")
        assert endpoint.texts() == [blocked_text("t", repo="o/r", number=7)]


# --- doctor and "orbi notify test" -----------------------------------------


def test_doctor_lines_name_the_missing_variable_and_env_file(monkeypatch):
    monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
    config = config_domain.RunnerConfig(
        telegram_chat_id=CHAT_ID, deploy_home=Path("/deploy"),
    )
    lines = notify.doctor_lines(config)
    assert len(lines) == 1
    assert notify.TOKEN_ENV in lines[0]
    assert "is not set" in lines[0]
    assert "/deploy/.orbi/env" in lines[0]
    assert TOKEN not in lines[0]


def test_doctor_lines_are_empty_when_configured_or_off(monkeypatch):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    assert notify.doctor_lines(config_for()) == []
    monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
    assert notify.doctor_lines(config_for(chat_id=None)) == []


def test_notify_test_command_prints_ok_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        assert notify.test_command(config_for()) == 0
        assert capsys.readouterr().out.strip() == "ok"
        assert endpoint.texts() == [
            "Orbi test notification\n"
            "Your notification target works. Future alerts look like this."
        ]


def test_notify_test_command_prints_the_bad_token_reason(
    monkeypatch, capsys,
):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint(
        status=401, body=b'{"ok": false, "description": "Unauthorized"}',
    ) as endpoint:
        endpoint.point(monkeypatch)
        assert notify.test_command(config_for()) == 1
        assert capsys.readouterr().out.strip() == (
            "notify_failed reason=token_invalid"
        )


def test_notify_test_command_without_a_chat_id_names_it(
    monkeypatch, capsys,
):
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        assert notify.test_command(config_for(chat_id=None)) == 1
        out = capsys.readouterr().out
        assert "telegram_chat_id" in out
        assert endpoint.requests == []


def test_orbi_notify_test_runs_through_the_cli(
    monkeypatch, tmp_path, capsys,
):
    """The real user entry point: `orbi notify test` sends and prints ok."""
    from orbi import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "prompt.md").write_text("p", encoding="utf-8")
    (tmp_path / "prompts" / "prompt_review.md").write_text(
        "r", encoding="utf-8")
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["o/r"]\n'
        f'repo_dir = "{repo}"\n'
        f'telegram_chat_id = "{CHAT_ID}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        assert cli.main(
            ["notify", "test", "--config", str(config_path)],
        ) == 0
        assert capsys.readouterr().out.strip() == "ok"
        assert len(endpoint.texts()) == 1


# --- redaction -------------------------------------------------------------


def test_redaction_hides_the_telegram_token():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    redacted = journal.redact_secrets(url)
    assert TOKEN not in redacted
    assert "bot<redacted>" in redacted
    assert TOKEN not in journal.redact_secrets(f"token={TOKEN}")


# --- merged-PR announcements (Issue #1674) ---------------------------------


def merged_config(tmp_path, *, chat_id=CHAT_ID, source_repo="owner/repo"):
    """A config the merged-PR scan reads: one repo, a tmp watermark dir."""
    return config_domain.RunnerConfig(
        telegram_chat_id=chat_id,
        repo_dir=tmp_path,
        source_repos=(source_repo,),
    )


def orbi_pr(number, title, *, merged_at="2026-10-16T19:00:00Z",
            head="orbi/owner-repo-issue-7", repo="owner/repo"):
    """One merged-PR node as the GraphQL read returns it."""
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{repo}/pull/{number}",
        "headRefName": head,
        "mergedAt": merged_at,
    }


def merged_text(number=7, title="Fix the thing", *,
                repo="owner/repo", language="en"):
    """The fixed code-merged message for one PR."""
    headline = (
        "Orbi · 代码已合并：" if language == "zh" else "Orbi · Code merged: "
    )
    return (
        f"{headline}{repo}#{number} {title}\n"
        f"https://github.com/{repo}/pull/{number}"
    )


class FakeMergedRead:
    """A stubbed gh read returning the scripted merged-PR nodes."""

    def __init__(self, prs=()):
        self.prs = list(prs)
        self.calls = []
        self.failure = None

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.failure is not None:
            raise self.failure
        return json.dumps({
            "data": {"repository": {"pullRequests": {"nodes": self.prs}}},
        })


def test_a_newly_merged_orbi_pr_is_announced_exactly_once(
    monkeypatch, tmp_path,
):
    """Acceptance 1: an orbi/ PR merged after the watermark sends ONE
    message naming the title and link; the next tick sends nothing."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead()
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        assert endpoint.requests == []
        fake.prs = [orbi_pr(7, "Fix the thing")]
        notify.tick_start(config)
        assert endpoint.texts() == [merged_text(7, "Fix the thing")]
        notify.tick_start(config)
        assert len(endpoint.texts()) == 1


def test_a_non_orbi_pr_is_not_announced(monkeypatch, tmp_path):
    """Acceptance 2: a merged PR whose head branch is not orbi/ is not
    one of Orbi's deliveries."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead([orbi_pr(1, "Old")])
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        fake.prs = [orbi_pr(
            2, "Another delivery", head="feature/other",
            merged_at="2026-10-16T20:00:00Z",
        )]
        notify.tick_start(config)
        assert endpoint.requests == []


def test_first_enable_skips_a_merge_that_happened_before(monkeypatch, tmp_path):
    """Acceptance 3: turning notifications on sends nothing for PRs merged
    earlier; a PR merged afterwards is announced once."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    old = orbi_pr(1, "Already merged", merged_at="2026-01-01T00:00:00Z")
    fresh = orbi_pr(2, "Merged after", merged_at="2026-02-01T00:00:00Z")
    fake = FakeMergedRead([old])
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        assert endpoint.requests == []
        fake.prs = [fresh, old]
        notify.tick_start(config)
        assert endpoint.texts() == [merged_text(2, "Merged after")]


def test_off_deletes_the_watermark_and_reenabling_does_not_replay(
    monkeypatch, tmp_path,
):
    """Acceptance 4: notifications off sends nothing and drops the
    watermark; turning them back on does not replay what merged between."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead([orbi_pr(1, "Old", merged_at="2026-01-01T00:00:00Z")])
    monkeypatch.setattr(seam, "run_command", fake)
    path = tmp_path / ".orbi" / "notify-merged-since"
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(merged_config(tmp_path))
        assert path.exists()
        fake.calls.clear()
        monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
        notify.tick_start(merged_config(tmp_path, chat_id=None))
        assert not path.exists()
        assert fake.calls == []
        monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
        fake.prs = [orbi_pr(
            2, "Merged while off", merged_at="2026-03-01T00:00:00Z",
        )]
        notify.tick_start(merged_config(tmp_path))
        assert endpoint.requests == []


def test_a_missing_token_drops_the_watermark_and_a_later_fix_does_not_replay(
    monkeypatch, tmp_path,
):
    """Acceptance 4: when notifications stay on but the token is missing,
    the watermark is dropped too, so restoring the token does not replay
    the gap."""
    fake = FakeMergedRead([orbi_pr(1, "Old", merged_at="2026-01-01T00:00:00Z")])
    monkeypatch.setattr(seam, "run_command", fake)
    path = tmp_path / ".orbi" / "notify-merged-since"
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
        notify.tick_start(merged_config(tmp_path))
        assert path.exists()
        monkeypatch.delenv(notify.TOKEN_ENV, raising=False)
        fake.calls.clear()
        notify.tick_start(merged_config(tmp_path))
        assert not path.exists()
        assert fake.calls == []
        monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
        fake.prs = [orbi_pr(
            2, "Merged during the gap", merged_at="2026-04-01T00:00:00Z",
        )]
        notify.tick_start(merged_config(tmp_path))
        assert endpoint.requests == []


def test_a_read_failure_is_logged_and_retried_next_tick(
    monkeypatch, tmp_path, caplog,
):
    """Acceptance 5: a failed read sends nothing, leaves the watermark
    unchanged and records one notify_failed; the next tick announces."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead()
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    path = tmp_path / ".orbi" / "notify-merged-since"
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        before = path.read_text(encoding="utf-8")
        fake.failure = RuntimeError("gh exploded")
        caplog.set_level("INFO")
        notify.tick_start(config)
        assert endpoint.requests == []
        assert path.read_text(encoding="utf-8") == before
        assert "notify_failed reason=merged_check_failed" in caplog.text
        fake.failure = None
        fake.prs = [orbi_pr(7, "Fix the thing")]
        notify.tick_start(config)
        assert endpoint.texts() == [merged_text(7, "Fix the thing")]


def test_a_check_that_cannot_run_is_logged_and_the_tick_completes(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1696: when the merged-PR check cannot run at all -- here
    `<repo_dir>/.orbi` is not writable, so the watermark lock cannot be
    opened -- the tick still completes (pure bypass) and the journal says
    `notify_failed reason=merged_check_failed` instead of the check
    silently never running."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead([orbi_pr(7, "Fix the thing")])
    monkeypatch.setattr(seam, "run_command", fake)
    run_dir = tmp_path / ".orbi"
    run_dir.mkdir()
    run_dir.chmod(0o500)
    try:
        caplog.set_level("INFO")
        notify.tick_start(merged_config(tmp_path))
    finally:
        run_dir.chmod(0o700)
    assert fake.calls == []
    assert caplog.text.count(
        "notify_failed reason=merged_check_failed",
    ) == 1


@pytest.mark.parametrize("garbage", [
    "not json",                  # unparseable
    "[]", '{"a": 1}',            # parseable, wrong JSON type
    '"0"', '"zzz"',              # parseable string, not a mergedAt
])
def test_a_garbage_watermark_starts_fresh_and_is_rewritten(
    monkeypatch, tmp_path, garbage,
):
    """Acceptance 6: a watermark file with garbage replays nothing and is
    rewritten from this tick's read.

    The parseable-but-not-a-timestamp strings are the trap: compared as
    plain strings, a LOW one ("0") matches every real mergedAt and
    replays the backlog, a HIGH one ("zzz") matches none and silences
    the scan forever."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead([orbi_pr(1, "Already merged")])
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    path = tmp_path / ".orbi" / "notify-merged-since"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(garbage, encoding="utf-8")
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        assert endpoint.requests == []
    assert json.loads(path.read_text(encoding="utf-8")) == (
        "2026-10-16T19:00:00Z"
    )


def test_two_overlapping_ticks_send_only_one(monkeypatch, tmp_path):
    """Acceptance 7: two ticks racing the same merge send exactly one."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead()
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    with TelegramEndpoint(delay=0.3) as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)  # establish the watermark
        fake.prs = [orbi_pr(7, "Fix the thing")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(notify.tick_start, config) for _ in range(2)
            ]
            for future in futures:
                future.result(timeout=30)
    assert endpoint.texts() == [merged_text(7, "Fix the thing")]


def test_a_busy_watermark_lock_skips_the_tick(monkeypatch, tmp_path):
    """The lock is the cross-tick guard: a held lock skips this tick
    instead of waiting or double-sending."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead([orbi_pr(7, "Fix the thing")])
    monkeypatch.setattr(seam, "run_command", fake)
    config = merged_config(tmp_path)
    path = tmp_path / ".orbi" / "notify-merged-since"
    # Make the bounded non-blocking wait expire at once (Issue #95 guard):
    # the lock stays held for the whole test, so no 5 s real wait.
    monkeypatch.setitem(runner_health.__dict__, "HEALTH_LOCK_TIMEOUT_S", -1.0)
    holder = runner_health._acquire_health_lock(path, blocking=True)
    try:
        with TelegramEndpoint() as endpoint:
            endpoint.point(monkeypatch)
            notify.tick_start(config)
            assert endpoint.requests == []
            assert fake.calls == []
    finally:
        os.close(holder)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_merged_message_uses_the_host_language(monkeypatch, tmp_path, language):
    """The message follows the host message_language (#1660)."""
    monkeypatch.setenv(notify.TOKEN_ENV, TOKEN)
    fake = FakeMergedRead()
    monkeypatch.setattr(seam, "run_command", fake)
    config = HostLanguageConfig(
        message_language=language,
        repo_dir=tmp_path,
        source_repos=("owner/repo",),
    )
    with TelegramEndpoint() as endpoint:
        endpoint.point(monkeypatch)
        notify.tick_start(config)
        fake.prs = [orbi_pr(7, "Fix the thing")]
        notify.tick_start(config)
        assert endpoint.texts() == [
            merged_text(7, "Fix the thing", language=language)
        ]
