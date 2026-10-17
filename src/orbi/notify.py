"""Telegram "needs you" notifications (Issue #1673).

A person who hands Issues to Orbi is otherwise reachable only through
GitHub. When Orbi NEWLY adds one of the three attention labels —
`ai-blocked`, `ai-needs-detail`, `ai-awaiting-merge` — this module sends
ONE Telegram message (private chat or group) naming the state, the Issue
and its link, in Chinese or English. It also sends ONE message when
the human-review gate starts holding a delivery for a person (Issue
#1684). `orbi notify test` checks the setup before a real event; a
label a person adds by hand sends nothing.

A message that is a problem the person must resolve (`ai-blocked`,
`ai-needs-detail`, the human-review wait) ends with the community help
line from the `orbi.language` catalog; a normal handoff
(`ai-awaiting-merge`, a merged PR, the test message) does not.

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
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import language
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    EVENT_HUMAN_REVIEW_WAITING,
    NEEDS_DETAIL_LABEL,
    READY_LABEL,
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

# The attention labels whose message is a problem the person must
# resolve, so it ends with the community help line (Issue #1684).
# `ai-awaiting-merge` is an ordinary handoff and does not.
HELP_LABELS = frozenset({BLOCKED_LABEL, NEEDS_DETAIL_LABEL})
# The one catalog key of that line (`orbi.language`).
HELP_KEY = "help.community"

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
# The human-review wait status (Issue #1684): the gate holds the
# delivery until a person confirms the acceptance checklist.
HUMAN_REVIEW_STATUS_EN = "Waiting for your review"
HUMAN_REVIEW_STATUS_ZH = "等您人工验收"
TEST_EN = (
    "Orbi test notification\n"
    "Your notification target works. Future alerts look like this."
)
TEST_ZH = (
    "Orbi 测试通知\n"
    "这条消息确认您的接收配置可用，之后的通知会是这个样子。"
)

# The fixed code-merged texts (Issue #1674): the same `Orbi · ` prefix
# and the `owner/repo#N <title>` line, followed by the PR link.
_MERGED_HEADLINE_EN = "Orbi · Code merged: "
_MERGED_HEADLINE_ZH = "Orbi · 代码已合并："

# The one watermark the merged-PR check keeps (one source repo, Issue
# #1674): the last handled GitHub `mergedAt`, stored beside the
# runner-health state and written with the runner-health atomic write.
WATERMARK_FILENAME = "notify-merged-since"
WATERMARK_EPOCH = "1970-01-01T00:00:00Z"
ORBI_HEAD_PREFIX = "orbi/"
# The shape of a stored watermark: what this module writes is a GitHub
# `mergedAt` (ISO-8601 UTC, prefix-matched so a fractional second or an
# explicit offset still counts). Any other value is unreadable state and
# starts fresh, exactly like an unparseable file: compared as plain
# strings, a garbage value that sorts below real timestamps (say "0")
# would replay the backlog, and one that sorts above them (say "zzz")
# would silence the scan forever.
WATERMARK_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# One tick's bound configuration and token, set by `tick_start` so the
# label call sites pass no argument of their own. Tests reset them for
# every test (the autouse fixture in tests/conftest.py).
_config: RunnerConfig | None = None
_token: str | None = None
_last_failure: str | None = None


def tick_start(config: RunnerConfig) -> None:
    """Bind one tick's config and bot token; run the merged-PR check.

    Never raises (pure bypass). `telegram_chat_id` unset/empty = the
    notifier is off: nothing is reported and nothing is sent. A chat id
    with no token writes ONE `notify_failed reason=not_configured` line per
    tick naming the variable (never a value) and sends nothing.

    When notifications are off or half-configured the merged-PR
    watermark is deleted: fixing a broken setup later must not replay
    what merged meanwhile. When they are on and configured,
    `_announce_merged_prs` (Issue #1674) sends one message per
    `orbi/*` PR merged after the last handled `mergedAt`.
    """
    global _config, _token
    try:
        _config = config
        _token = os.environ.get(TOKEN_ENV)
        if not _enabled():
            if _chat_id() and not _token:
                _fail("not_configured", missing=TOKEN_ENV)
            _clear_watermark()
            return
        _announce_merged_prs()
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


def patch_applied(event: str, added_labels, current_labels, *,
                  repo: str, number: int, read_title) -> None:
    """Send the ONE notification a just-applied label patch warrants.

    The decision moved out of `github.apply_label_patch` (Issue #1684)
    so the oversized `github.py` gains no branch:

    - `EVENT_HUMAN_REVIEW_WAITING` (the human acceptance gate starts
      holding a delivery): ONE "needs you" message with the wait status
      and the community help line. The caller applies this patch once
      per wait (`ai-ready` is added only when absent), so a later tick
      -- where `ai-ready` is already set -- sends nothing.
    - a NEWLY added attention label (`ai-blocked`, `ai-needs-detail`,
      `ai-awaiting-merge`): the ordinary "needs you" message; the two
      problem labels end with the help line.

    `read_title` is a callable, so the Issue title is read AT MOST once
    and only when a message is actually sent -- never when notifications
    are off or the patch warrants nothing. This module still never
    builds a `gh` command. Never raises (pure bypass).
    """
    try:
        if not _enabled():
            return
        current = set(current_labels)
        if event == EVENT_HUMAN_REVIEW_WAITING:
            if READY_LABEL in current:
                return
            send(_human_review_message(repo, number, read_title()))
            return
        # The caller only reaches this point with an applied patch, and
        # every event's patch carries exactly one added label.
        label = added_labels[0]
        if label in ATTENTION_LABELS and label not in current:
            send(_attention_message(label, repo, number, read_title()))
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
    language_code = _language()
    status = (_STATUS_ZH if language_code == "zh" else _STATUS_EN)[label]
    return _needs_you_message(
        status, repo, number, title, help_line=label in HELP_LABELS,
    )


def _human_review_message(repo: str, number: int,
                          title: str | None) -> str:
    """The fixed "waiting for your review" text (Issue #1684)."""
    language_code = _language()
    status = (
        HUMAN_REVIEW_STATUS_ZH if language_code == "zh"
        else HUMAN_REVIEW_STATUS_EN
    )
    return _needs_you_message(
        status, repo, number, title, help_line=True,
    )


def _needs_you_message(status: str, repo: str, number: int,
                       title: str | None, *, help_line: bool) -> str:
    """One "needs you" message; a problem message ends with the help line."""
    language_code = _language()
    headline = _HEADLINE_ZH if language_code == "zh" else _HEADLINE_EN
    body = (
        f"{headline}{status}\n"
        f"{_issue_line(repo, number, title)}\n"
        f"https://github.com/{repo}/issues/{number}"
    )
    if not help_line:
        return body
    return f"{body}\n{language.text(language_code, HELP_KEY)}"


def _issue_line(repo: str, number: int, title: str | None) -> str:
    """`owner/repo#N [title]` — the title is omitted when it is unknown."""
    line = f"{repo}#{number}"
    return f"{line} {title}" if title else line


def _test_message() -> str:
    """The fixed test-notification text in the configured language."""
    return TEST_ZH if _language() == "zh" else TEST_EN


def _announce_merged_prs() -> None:
    """Send one message per newly merged `orbi/*` PR (Issue #1674).

    The watermark read-modify-write is serialized with the runner-health
    leaf lock (non-blocking: a busy lock skips this tick rather than
    waiting) and the watermark is written BEFORE the messages, so two
    overlapping ticks can never announce the same merge twice. A read
    failure records one `notify_failed reason=merged_check_failed` and
    leaves the watermark unchanged. Never raises (pure bypass).
    """
    source_repo = _source_repo()
    if not source_repo:
        return
    path = _watermark_path()
    from orbi import branch_reclaim, runner_health
    lock_fd = runner_health._acquire_health_lock(path, blocking=False)
    if lock_fd is None:
        return
    try:
        since = _read_watermark(path)
        try:
            merged = branch_reclaim.read_merged_pull_requests(source_repo)
        except Exception:
            _fail("merged_check_failed")
            return
        newest = max((_merged_at(pr) for pr in merged), default="")
        if not since:
            # First enable, a re-enable, or an unreadable watermark: the
            # backlog is not replayed, the newest merge becomes the mark.
            _write_watermark(path, newest or WATERMARK_EPOCH)
            return
        fresh = sorted(
            (
                pr for pr in merged
                if _is_orbi_pr(pr) and _merged_at(pr) > since
            ),
            key=_merged_at,
        )
        if not fresh:
            return
        _write_watermark(path, newest)
        for pr in fresh:
            send(_merged_message(source_repo, pr))
    finally:
        os.close(lock_fd)


def _source_repo() -> str:
    """The one configured source repository ("" when unset)."""
    repos = getattr(_config, "source_repos", ()) or ()
    return repos[0] if repos else ""


def _watermark_path() -> Path:
    """The merged-PR watermark, beside the runner-health state."""
    return Path(_config.repo_dir) / ".orbi" / WATERMARK_FILENAME


def _read_watermark(path: Path) -> str:
    """The stored `mergedAt`; "" for a missing or unreadable watermark.

    The same "unreadable state starts fresh" rule as runner health: a
    corrupt watermark replays nothing and is rewritten on the next scan.
    Unreadable covers the parseable cases too — a wrong JSON type or a
    string that is not a `mergedAt` (`WATERMARK_RE`) — because a value
    the code cannot interpret must never be compared against real
    timestamps.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(value, str) or not WATERMARK_RE.match(value):
        return ""
    return value


def _write_watermark(path: Path, value: str) -> None:
    """Atomically store the last handled `mergedAt` (runner-health write)."""
    from orbi import runner_health
    runner_health.save_health_state(path, value)


def _clear_watermark() -> None:
    """Delete the watermark (notifications off or half-configured)."""
    _watermark_path().unlink(missing_ok=True)


def _is_orbi_pr(pr: dict) -> bool:
    """True for a PR whose head branch Orbi itself created."""
    head = pr.get("headRefName")
    return isinstance(head, str) and head.startswith(ORBI_HEAD_PREFIX)


def _merged_at(pr: dict) -> str:
    """The PR's GitHub `mergedAt`, or "" when it is absent/malformed."""
    value = pr.get("mergedAt")
    return value if isinstance(value, str) else ""


def _merged_message(repo: str, pr: dict) -> str:
    """The fixed "code merged" text for one merged PR."""
    headline = (
        _MERGED_HEADLINE_ZH if _language() == "zh" else _MERGED_HEADLINE_EN
    )
    return f"{headline}{repo}#{pr['number']} {pr['title']}\n{pr['url']}"
