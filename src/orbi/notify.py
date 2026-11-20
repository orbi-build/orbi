"""Telegram "needs you" notifications (Issue #1673).

A person who hands Issues to Orbi is otherwise reachable only through GitHub.
When Orbi NEWLY adds one of the three attention labels — `ai-blocked`,
`ai-needs-detail`, `ai-awaiting-merge` — this module sends ONE Telegram
message (private chat or group) naming the state, the Issue and its link, in
Chinese or English, ONE "waiting for your review" message where the
human-review checklist is built (Issue #1691) and ONE message when the engine
answers a stop question itself (Issue #2220). `orbi notify test` checks the
setup; a label a person adds by hand sends nothing, and the gate's own label
patch sends nothing. A problem message (`ai-blocked`,
`ai-needs-detail`, the human-review wait) ends with the community help line
from the `orbi.language` catalog; a normal handoff (`ai-awaiting-merge`, a
merged PR, the test message) does not.

The two problem messages are sent by the stop EXIT that posts the comment,
after that comment exists (Issue #2287), so the alert quotes the comment's own
reason and next action, says whether Orbi resumes by itself, and links the
comment: `blocked_comment.post_blocked_comment` calls `stop_alert`, and
`clarify.stop` calls `needs_detail_stop`. The label patch therefore only
sends the ordinary `ai-awaiting-merge` handoff — it lands BEFORE a stop
comment, when neither the reason nor the permalink is knowable. The sentences
and the renderers live in `orbi.notify_text`.

The adapter is Telegram-only and uses the standard library `urllib` (no
new dependency). Notifications are a PURE BYPASS, exactly like the progress
comment: every public function catches everything internally — never a
raise, a failed delivery, a label change or a blocked tick — and a failure
writes one `notify_failed` journal line.

Configuration: `telegram_chat_id` is the one `orbi.toml` key (set =
notifications on); the message language is the host-level
`message_language` (#1660), never a repository override, because it goes to
a person. The bot token is NOT a key — it lives in
`<deploy_home>/.orbi/env` as `ORBI_TELEGRAM_BOT_TOKEN` (Article 6.1); a chat
id without a token is reported (`notify_failed reason=not_configured`) but
never fails a start, since `.orbi/env` and `orbi.toml` can lag each other.

`github.py` imports this module, so the top-level imports stay acyclic
(`journal`, `delivery_labels`, the `gitops` naming leaf, the `notify_text`
sentences); no module that builds a `gh` command is imported here, and
`RunnerConfig` is annotation-only.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import language, notify_text
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    NEEDS_DETAIL_LABEL,
)
from orbi.gitops import is_delivery_branch
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

# The attention labels whose message is a problem the person must resolve,
# so it ends with the community help line (Issue #1684) and names the stop
# comment (Issue #2287). `ai-awaiting-merge` is an ordinary handoff and
# sends the plain message instead.
HELP_LABELS = frozenset({BLOCKED_LABEL, NEEDS_DETAIL_LABEL})

# The one watermark the merged-PR check keeps (one source repo, Issue
# #1674): the last handled GitHub `mergedAt`, stored beside the
# runner-health state and written with the runner-health atomic write.
WATERMARK_FILENAME = "notify-merged-since"
WATERMARK_EPOCH = "1970-01-01T00:00:00Z"
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
# Telegram's own `description` of the last failure ("" = the body carried
# none); `orbi notify test` prints it as `detail=` (Issue #1686).
_last_detail: str | None = None


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
    Orbi delivery PR (the `gitops.is_delivery_branch` head, Issue #1776)
    merged after the last handled `mergedAt`.

    Any exception escaping the check records one
    `notify_failed reason=merged_check_failed` (Issue #1696): the check
    could not RUN at all -- for example `<repo_dir>/.orbi` is not
    writable, so the watermark lock cannot be opened -- and a silent
    "code merged" message that never arrives would otherwise have no
    trace. It still never raises.
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
        _fail("merged_check_failed")


def send(text: str) -> bool:
    """POST one message to the configured chat; never raises (bypass).

    True only for HTTP 2xx with the documented `{"ok": true}` body. Every
    other outcome is classified, recorded as one `notify_failed` line and
    reported as False. The body's own `description` joins that line as
    `detail=` when it has one (Issue #1686): "Bad Request: chat not found"
    says what "rejected" only classifies. The token never leaves this
    call: nothing logged or returned carries it.
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
            return True
        detail = _description(body)
        extra = {"detail": detail} if detail else {}
        if status == 401:
            return _fail("token_invalid", **extra)
        if status == 429:
            return _fail("rate_limited", **extra)
        if 500 <= status < 600:
            return _fail("platform_error", **extra)
        return _fail("rejected", **extra)
    except Exception:
        return _fail("network_error")


def send_if_enabled(text: str) -> bool:
    """POST one rendered message when the notifier is on, silent off (#2220)."""
    return _enabled() and send(text)


def stop_alert(number: int, *, repo: str, label: str, body: str,
               comment_url: str | None, comment_language: str,
               read_title) -> None:
    """Send the ONE alert of a stop, AFTER its stop comment landed.

    `body` is that comment exactly as posted: its own `**Reason:**` /
    `**Action:**` lines are what the alert quotes (in `comment_language`, the
    run's language, which a repository may set apart from the host's), and
    its `orbi:failure:v1` record decides whether the engine resumes by
    itself. `comment_url` is the permalink the write returned, "" when `gh`
    printed none. The host `message_language` dresses only the alert's own
    labels.

    A no-op for a label that is not a stop that needs a person, for a
    notifier that is off or half-configured (`tick_start` already reported
    the gap — and then no title is read either) and for every failure.
    NEVER raises (pure bypass).
    """
    try:
        if label not in HELP_LABELS or not _enabled():
            return
        send(notify_text.stop_message(
            label, repo, number, read_title(),
            detail=notify_text.stop_detail(body, language_code=comment_language),
            comment_url=comment_url, language_code=_language(),
        ))
    except Exception:
        return


def needs_detail_stop(repo: str, number: int, title: str | None, *,
                      comment_url: str | None, ready_label: str,
                      language_code: str) -> None:
    """Send the ONE alert of an `ai-needs-detail` stop (Issues #1673, #2287).

    Sent by the clarification gate right AFTER its stop comment and label
    swap, so the message names the reason the comment records, the repair
    instruction it asks for and the comment itself, with the same headline,
    status, issue line, Issue link and help line as before. `ready_label` is
    the repository's claim label that instruction names, and `language_code`
    is the run's language — the alert's own labels follow the host
    `message_language` instead.

    A no-op for a notifier that is off or half-configured and for every
    failure. NEVER raises (pure bypass).
    """
    try:
        if not _enabled():
            return
        send(notify_text.stop_message(
            NEEDS_DETAIL_LABEL, repo, number, title,
            detail=notify_text.needs_detail_detail(
                ready_label=ready_label, language_code=language_code),
            comment_url=comment_url, ready_label=ready_label,
            language_code=_language(),
        ))
    except Exception:
        return


def human_review_waiting(repo: str, number: int,
                         title: str | None = None) -> None:
    """Send the ONE "waiting for your review" message of a delivery.

    Sent by the PR-open checklist block when its column 2 asks a person
    to confirm something (Issue #1691), BEFORE the checklist comment is
    posted -- a failed comment still leaves the person a message. The
    block runs once per delivery, so the message is sent once: fix
    rounds, gate waits and recoveries never repost it. A no-op for a
    notifier that is off or half-configured (`tick_start` already
    reported the gap) and for every failure. NEVER raises (pure bypass).
    """
    try:
        if not _enabled():
            return
        send(notify_text.human_review_message(
            repo, number, title, language_code=_language()))
    except Exception:
        return


def patch_applied(event: str, added_labels, current_labels, *,
                  repo: str, number: int, read_title) -> None:
    """Send the ONE notification a just-applied label patch warrants.

    The decision moved out of `github.apply_label_patch` (Issue #1673)
    so the oversized `github.py` gains no branch: a NEWLY added
    `ai-awaiting-merge` — an ordinary handoff — sends the "needs you"
    message, and nothing else does. The two problem labels (`ai-blocked`,
    `ai-needs-detail`) send from their stop exit instead (Issue #2287):
    this patch lands BEFORE their stop comment, so at this moment neither
    the reason the comment records nor its permalink exists. The
    human-review wait is NOT triggered here either (Issue #1691): a normal
    delivery already carries `ai-ready`, so this patch usually does not
    even run, and the message belongs to the checklist block that decides
    it.

    `read_title` is a callable, so the Issue title is read AT MOST once
    and only when a message is actually sent -- never when notifications
    are off or the patch warrants nothing. This module still never
    builds a `gh` command. Never raises (pure bypass).
    """
    try:
        if not _enabled():
            return
        current = set(current_labels)
        # The caller only reaches this point with an applied patch, and
        # every event's patch carries exactly one added label.
        label = added_labels[0]
        if label == AWAITING_MERGE_LABEL and label not in current:
            send(notify_text.attention_message(
                label, repo, number, read_title(), language_code=_language()))
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

    Prints `ok` and returns 0 on a confirmed send. Otherwise it prints
    `notify_failed reason=<reason>` with Telegram's own `description` as
    `detail=` when the error body carried one, followed by the community
    help line (Issue #1686) -- the setup mistake ("chat not found", the
    bot blocked, the bot not in the group) is readable and has a place to
    ask. Never raises (pure bypass).
    """
    global _config, _token, _last_detail
    try:
        _config = config
        _token = os.environ.get(TOKEN_ENV)
        _last_detail = None
        if not _enabled():
            print(_problem_output(
                "notify_failed reason=not_configured "
                f"missing={_missing_setting()}"
            ))
            return 1
        if send(notify_text.test_message(_language())):
            print("ok")
            return 0
        print(_problem_output(_failure_line()))
        return 1
    except Exception:
        print(_problem_output("notify_failed reason=network_error"))
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
    global _last_failure, _last_detail
    _last_failure = reason
    _last_detail = fields.get("detail")
    event("notify_failed", reason=reason, **fields)
    return False


def _failure_line() -> str:
    """The last failure as one line, with `detail=` when it was read."""
    line = f"notify_failed reason={_last_failure}"
    return f"{line} detail={_last_detail}" if _last_detail else line


def _problem_output(line: str) -> str:
    """A problem line plus the catalog's community help line.

    The same `help.community` line every problem message ends with, in
    the host `message_language` (Issue #1686).
    """
    return f"{line}\n{language.text(_language(), notify_text.HELP_KEY)}"


def _payload(body: bytes) -> dict | None:
    """The decoded JSON object of a Telegram response body, or None."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _ok(body: bytes) -> bool:
    """True only for the documented `{"ok": true}` success body."""
    payload = _payload(body)
    return payload is not None and payload.get("ok") is True


def _description(body: bytes) -> str:
    """Telegram's own `description` from an error body ("" when absent).

    The readable half of a rejected request ("Bad Request: chat not
    found", "Forbidden: bot was blocked by the user"); it never carries
    the bot token, so it is recorded as `detail=` and printed as is
    (Issue #1686). A body that is not a JSON object, or whose
    description is not a string, yields "" and the pre-#1686 line.
    """
    payload = _payload(body)
    value = payload.get("description") if payload is not None else None
    return value if isinstance(value, str) else ""


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


def _announce_merged_prs() -> None:
    """Send one message per newly merged Orbi delivery PR (Issue #1674).

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
                if _is_orbi_pr(pr, source_repo)
                and _merged_at(pr) > since
            ),
            key=_merged_at,
        )
        if not fresh:
            return
        _write_watermark(path, newest)
        for pr in fresh:
            send(notify_text.merged_message(
                source_repo, pr, language_code=_language()))
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


def _is_orbi_pr(pr: dict, source_repo: str) -> bool:
    """True for a PR whose head is `source_repo`'s delivery branch.

    The decision is the engine's one ownership check (Issue #1776,
    `orbi.gitops.is_delivery_branch`), the same call the CI-failure triage
    makes: an `orbi/` prefix a self-hosted user chose (`orbi/manual`) is
    not an Orbi delivery and is never announced.
    """
    return is_delivery_branch(source_repo, pr.get("headRefName"))


def _merged_at(pr: dict) -> str:
    """The PR's GitHub `mergedAt`, or "" when it is absent/malformed."""
    value = pr.get("mergedAt")
    return value if isinstance(value, str) else ""
