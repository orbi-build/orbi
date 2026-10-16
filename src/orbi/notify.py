"""Telegram "needs you" notifications (Issue #1673).

A person who hands Issues to Orbi is otherwise reachable only through
GitHub. When Orbi NEWLY adds one of the three attention labels —
`ai-blocked`, `ai-needs-detail`, `ai-awaiting-merge` — this module sends
ONE Telegram message (private chat or group) naming the state, the Issue
and its link, in Chinese or English. `orbi notify test` checks the setup
before a real event; a label a person adds by hand sends nothing.

The adapter is Telegram-only and uses the standard library `urllib` (no
new dependency). Notifications are a PURE BYPASS, exactly like the
progress comment: every public function catches everything internally and
never raises, never fails a delivery, never changes a label and never
blocks a tick. A failure writes one `notify_failed` journal line.

Configuration: `telegram_chat_id` is the one `orbi.toml` key (set =
notifications on). The message language is the host-level
`message_language` (#1660), read when the loaded host config carries it
and `en` otherwise — never a repository override: the message goes to a
person, not into the repository. The bot token is NOT a key — it lives in
`<deploy_home>/.orbi/env` as `ORBI_TELEGRAM_BOT_TOKEN` (Article 6.1). A
chat id without a token is reported (`notify_failed reason=not_configured`)
but never fails a start: `.orbi/env` and `orbi.toml` are separate files
that can lag each other.

`github.py` imports this module, so the top-level imports stay at the
leaves (`journal`, `delivery_labels`); `RunnerConfig` is annotation-only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    NEEDS_DETAIL_LABEL,
)
from orbi.journal import event

if TYPE_CHECKING:
    from orbi.config import RunnerConfig

# The Telegram Bot API origin, the single request budget and the env-file
# variable. The origin and timeout are module constants so a test replaces
# them (tests/fakes/telegram.py) and never waits 10 s or reaches the real
# API.
TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10
TOKEN_ENV = "ORBI_TELEGRAM_BOT_TOKEN"

# The three labels that mean "a person must act".
ATTENTION_LABELS = frozenset({
    BLOCKED_LABEL, NEEDS_DETAIL_LABEL, AWAITING_MERGE_LABEL,
})

# The fixed message texts. The `Orbi · ` prefix is part of the contract.
_HEADLINE_EN = "Orbi · Needs your attention: "
_HEADLINE_ZH = "Orbi · 需要您处理："
_STATUS_EN = {
    BLOCKED_LABEL: "Orbi is stuck and needs you",
    NEEDS_DETAIL_LABEL: "Orbi needs more detail",
    AWAITING_MERGE_LABEL: "Waiting for you to approve the merge",
}
_STATUS_ZH = {
    BLOCKED_LABEL: "Orbi 卡住了，需要您处理",
    NEEDS_DETAIL_LABEL: "需要您补充信息",
    AWAITING_MERGE_LABEL: "等您批准合并",
}
TEST_EN = (
    "Orbi test notification\n"
    "Your notification target works. Future alerts look like this."
)
TEST_ZH = (
    "Orbi 测试通知\n"
    "这条消息确认您的接收配置可用，之后的通知会是这个样子。"
)

# One tick's bound configuration and token, set by `tick_start` so the
# label call sites pass no argument of their own. Tests reset them for
# every test (the autouse fixture in tests/conftest.py).
_config: RunnerConfig | None = None
_token: str | None = None
_last_failure: str | None = None


def tick_start(config: RunnerConfig) -> None:
    """Bind one tick's config and bot token; report a half-configuration.

    Never raises (pure bypass). `telegram_chat_id` unset/empty = the
    notifier is off: nothing is reported and nothing is sent. A chat id
    with no token writes ONE `notify_failed reason=not_configured` line per
    tick naming the variable (never a value) and sends nothing.
    """
    global _config, _token
    try:
        _config = config
        _token = os.environ.get(TOKEN_ENV)
        if _chat_id() and not _token:
            _fail("not_configured", missing=TOKEN_ENV)
    except Exception:
        return


def enabled() -> bool:
    """True when this tick bound both a chat id and a token.

    The hook in `github.apply_label_patch` reads the Issue title ONLY
    when this is true, so a tick with notifications off makes no extra
    `gh` call. Never raises (pure bypass).
    """
    try:
        return _enabled()
    except Exception:
        return False


def send(text: str) -> bool:
    """POST one message to the configured chat; never raises (bypass).

    True only for HTTP 2xx with the documented `{"ok": true}` body. Every
    other outcome is classified, recorded as one `notify_failed` line and
    reported as False. The token never leaves this call: nothing logged or
    returned carries it.
    """
    try:
        if not _enabled():
            return _fail("not_configured", missing=_missing_setting())
        request = urllib.request.Request(
            f"{TELEGRAM_API_BASE}/bot{_token}/sendMessage",
            data=json.dumps({
                "chat_id": _chat_id(),
                "text": text,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=SEND_TIMEOUT_SECONDS,
            ) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            # The JSON error body is read too: 401/429/5xx and the
            # "2xx without ok" case are classified from status + body.
            status, body = exc.code, exc.read()
        except urllib.error.URLError as exc:
            # urllib wraps a socket timeout raised inside do_open into
            # URLError(reason=TimeoutError); classify it by its cause.
            if isinstance(exc.reason, TimeoutError):
                return _fail("timeout")
            raise
        except TimeoutError:
            return _fail("timeout")
        if 200 <= status < 300 and _ok(body):
            _last_failure = None
            return True
        if status == 401:
            return _fail("token_invalid")
        if status == 429:
            return _fail("rate_limited")
        if 500 <= status < 600:
            return _fail("platform_error")
        return _fail("rejected")
    except Exception:
        return _fail("network_error")


def label_added(repo: str, number: int, label: str,
                title: str | None = None) -> None:
    """Send one "needs you" message for a label that was just added.

    A no-op for a label that does not wait for a person, for a notifier
    that is off or half-configured (`tick_start` already reported the
    gap) and for every failure. NEVER raises (pure bypass).
    """
    try:
        if label not in ATTENTION_LABELS or not _enabled():
            return
        send(_attention_message(label, repo, number, title))
    except Exception:
        return


def doctor_lines(config: RunnerConfig) -> list[str]:
    """Read-only doctor finding(s) for a half-configured notifier.

    Names the missing variable and the env file, never a value — the same
    wording style as the provider-key finding. Never raises (pure bypass).
    """
    try:
        if not getattr(config, "telegram_chat_id", None):
            return []
        token = os.environ.get(TOKEN_ENV)
        if token:
            return []
        env_file = Path(config.deploy_home) / ".orbi" / "env"
        return [
            f"notify: telegram_chat_id is set but {TOKEN_ENV} "
            f"{_token_state(token)} (file: {env_file})"
        ]
    except Exception:
        return []


def test_command(config: RunnerConfig) -> int:
    """`orbi notify test`: send one test message and report the outcome.

    Prints `ok` and returns 0 on a confirmed send; prints the structured
    failure reason and returns non-zero otherwise. Never raises (pure
    bypass).
    """
    global _config, _token
    try:
        _config = config
        _token = os.environ.get(TOKEN_ENV)
        if not _enabled():
            print(
                "notify_failed reason=not_configured "
                f"missing={_missing_setting()}"
            )
            return 1
        if send(_test_message()):
            print("ok")
            return 0
        print(f"notify_failed reason={_last_failure}")
        return 1
    except Exception:
        print("notify_failed reason=network_error")
        return 1


def register_cli(subparsers, common) -> None:
    """Register `orbi notify` (two-level, like `orbi milestone set`)."""
    parser = subparsers.add_parser(
        "notify", parents=[common],
        help="send notifications and check the notification setup",
    )
    commands = parser.add_subparsers(dest="notify_command", required=True)
    commands.add_parser(
        "test", parents=[common],
        help="send one test message to the configured chat",
    )


def _fail(reason: str, **fields) -> bool:
    """Record one `notify_failed` line and report the failure."""
    global _last_failure
    _last_failure = reason
    event("notify_failed", reason=reason, **fields)
    return False


def _ok(body: bytes) -> bool:
    """True only for the documented `{"ok": true}` success body."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _chat_id() -> str:
    """The configured chat id ("" = notifications off)."""
    value = getattr(_config, "telegram_chat_id", None) if _config else None
    return value if isinstance(value, str) else ""


def _language() -> str:
    """The host message language (#1660); only `zh`/`en` exist.

    `tick_start` binds the HOST config, never a repository-derived one,
    so this reads the instance setting (default `en`) and stays correct
    when #1660 adds the field to `RunnerConfig`.
    """
    value = getattr(_config, "message_language", "en") if _config else "en"
    return "zh" if value == "zh" else "en"


def _enabled() -> bool:
    """True when a chat id and a token are both bound."""
    return _config is not None and bool(_chat_id()) and bool(_token)


def _missing_setting() -> str:
    """The name of the one missing setting (never a value)."""
    return TOKEN_ENV if _chat_id() else "telegram_chat_id"


def _token_state(token: str | None) -> str:
    """The human-readable state of the token variable (never its value)."""
    return "is set but empty" if token == "" else "is not set"


def _attention_message(label: str, repo: str, number: int,
                       title: str | None) -> str:
    """The fixed "needs you" text for one attention label."""
    language = _language()
    headline = _HEADLINE_ZH if language == "zh" else _HEADLINE_EN
    status = (_STATUS_ZH if language == "zh" else _STATUS_EN)[label]
    return (
        f"{headline}{status}\n"
        f"{_issue_line(repo, number, title)}\n"
        f"https://github.com/{repo}/issues/{number}"
    )


def _issue_line(repo: str, number: int, title: str | None) -> str:
    """`owner/repo#N [title]` — the title is omitted when it is unknown."""
    line = f"{repo}#{number}"
    return f"{line} {title}" if title else line


def _test_message() -> str:
    """The fixed test-notification text in the configured language."""
    return TEST_ZH if _language() == "zh" else TEST_EN
