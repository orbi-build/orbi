"""Milestone bookkeeping and progress milestones."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from collections.abc import Callable

from orbi.delivery_labels import READY_LABEL
from orbi.gitops import acquire_base_sync_lock
from orbi.github import (
    close_milestone, list_issues, list_milestones, milestone_open_issues,
    parse_paginated_issue_array, run_gh_read_command,
)
from orbi.journal import LOGGER, event, run_command
from orbi.progress import ProgressPublisher, read_test_result

_TEST_EXIT_RE = re.compile(r"\bexit\s*[:=]\s*(-?\d+)\b", re.IGNORECASE)
_TEST_OUTCOME_COUNT_RE = re.compile(
    r"\b(\d+)\s+(?:failed|failures?|errors?)\b", re.IGNORECASE,
)
_TEST_FAILURE_EVIDENCE_RE = re.compile(
    r"^\s*(?:FAILED|ERROR)\b", re.IGNORECASE,
)

def sync_active_milestone_variable(
    repo: str, milestone: str | None, *,
    run_command: Callable[..., str] | None = None,
) -> None:
    """Keep the triage workflow's read-only milestone variable current.

    This is deliberately a bypass: a GitHub variable outage must not stop
    the Runner's issue delivery tick.  The workflow consumes this value via
    ``vars.ORBI_ACTIVE_MILESTONE`` and the triage script resolves its title.
    """
    command_runner = run_command or globals()["run_command"]
    endpoint = f"repos/{repo}/actions/variables/ORBI_ACTIVE_MILESTONE"
    try:
        # The read's 404 is the designed absent branch (a repo without the
        # variable is the normal state), so its generic command_failed line
        # stays at DEBUG — same contract as the repo_config contents read.
        raw = command_runner(
            ["gh", "api", endpoint], timeout=30,
            failure_log_level=logging.DEBUG,
        )
        current = json.loads(raw)
        if not isinstance(current, dict):
            raise ValueError("variable response is not an object")
        if milestone is None:
            command_runner(
                ["gh", "api", "-X", "DELETE", endpoint], timeout=30,
            )
            event("active_milestone_variable_removed", repo=repo)
            return
        if current.get("value") == milestone:
            event("active_milestone_variable_unchanged", repo=repo)
            return
        command_runner(
            ["gh", "api", "-X", "PATCH", endpoint, "-f", f"name=ORBI_ACTIVE_MILESTONE",
             "-f", f"value={milestone}"], timeout=30,
        )
        event("active_milestone_variable_updated", repo=repo)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1 or "404" not in (exc.stderr or ""):
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
            return
        if milestone is None:
            event("active_milestone_variable_absent", repo=repo)
            return
        try:
            command_runner(
                ["gh", "api", "-X", "POST", "repos/{}/actions/variables".format(repo),
                 "-f", "name=ORBI_ACTIVE_MILESTONE", "-f", f"value={milestone}"],
                timeout=30,
            )
            event("active_milestone_variable_created", repo=repo)
        except Exception:
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
    except Exception:
        LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)

def validate_active_milestone(repo: str, active_milestone: str | None) -> None:
    """Validate the configured Milestone before a tick can claim work.

    A missing title is an unambiguous configuration error and fails fast.
    A closed title is reported as a fact only: maintainers may intentionally
    leave it configured during release wind-down or while preparing another
    line of work (Issue #1185 correction).
    """
    if active_milestone is None:
        return

    milestones = list_milestones(repo, timeout=30)
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and milestone.get("title") == active_milestone
    ]
    if not matches:
        open_titles = [
            str(milestone.get("title"))
            for milestone in milestones
            if isinstance(milestone, dict)
            and milestone.get("state") == "open"
        ]
        event(
            "active_milestone_missing", level=logging.ERROR,
            configured=active_milestone, repo=repo, state="absent",
            open_milestones=", ".join(open_titles) or "(none)",
            fix=("set active_milestone to an exact existing title, or remove "
                 "the field"),
        )
        raise RuntimeError(
            f"active_milestone_missing configured={active_milestone!r} "
            f"repo={repo} state=absent open_milestones="
            f"{', '.join(open_titles) or '(none)'}; "
            "fix=set active_milestone to an exact existing title or remove it"
        )

    milestone = matches[0]
    if milestone.get("state") != "closed":
        return

    ready_issues = list_issues(
        repo, state="open", label=READY_LABEL,
        milestone=active_milestone, json_fields="number", limit=1000,
        timeout=30,
    )
    event(
        "active_milestone_closed", level=logging.INFO,
        configured=active_milestone, repo=repo, state="closed",
        ai_ready_count=len(ready_issues),
    )

def reconcile_release_milestones(repo: str, run_id: str) -> list[str]:
    """Close published-release Milestones that now have no open Issues.

    This is a tick-level, fail-open sweep: release publication and the exact
    Milestone title are independent GitHub facts, so a late-closing Issue is
    reconciled on a later tick without requiring a new release run.
    """
    all_milestones = list_milestones(repo)
    milestones = [m for m in all_milestones if m.get("state") == "open"]
    for milestone in all_milestones:
        if not isinstance(milestone, dict) or milestone.get("state") not in {"open", "closed"}:
            event(
                "milestone_kept_open", number=milestone.get("number")
                if isinstance(milestone, dict) else None,
                repo=repo, reason="malformed",
            )
    releases_raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/releases?per_page=100",
        "--paginate", "--slurp",
    ])
    releases = parse_paginated_issue_array(releases_raw)
    published_tags = {
        release.get("tag_name") for release in releases
        if isinstance(release.get("tag_name"), str)
        and release.get("draft") is False
    }
    evidence: list[str] = []
    for milestone in milestones:
        number = milestone.get("number")
        title = milestone.get("title")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(title, str):
            event("milestone_kept_open", number=number, repo=repo, reason="malformed")
            continue
        # Closed duplicates still make the title ambiguous; do not guess
        # which milestone a release belongs to.
        matches = [m for m in all_milestones if m.get("title") == title]
        if len(matches) != 1:
            reason = "ambiguous title" if len(matches) > 1 else "missing title"
            event("milestone_kept_open", number=number, repo=repo, reason=reason)
            continue
        if title not in published_tags:
            event("milestone_kept_open", number=number, repo=repo,
                  reason="no published release")
            continue
        open_issues = milestone_open_issues(repo, number)
        if open_issues:
            event("milestone_kept_open", number=number, repo=repo, reason="open issues")
            continue
        close_milestone(repo, int(number))
        event("milestone_closed", number=number, repo=repo)
        evidence.append(f"Milestone #{number} ({title}) closed")
    return evidence

def _parse_version_title(title: object) -> tuple[int, int, int] | None:
    """Parse a strict ``v<major>.<minor>.<patch>`` milestone title."""
    if not isinstance(title, str):
        return None
    match = re.fullmatch(
        r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", title,
    )
    return tuple(map(int, match.groups())) if match else None

def rewrite_active_milestone_line(config_path: Path, new_value: str) -> None:
    """Replace only the configured active_milestone line, byte-for-byte."""
    text = config_path.read_bytes().decode("utf-8")
    pattern = re.compile(r"(?m)^[ \t]*active_milestone[ \t]*=[ \t]*[^\r\n]+")
    if not pattern.search(text):
        raise RuntimeError(
            f"active_milestone line not found in {config_path}"
        )
    # The value is serialized, never interpolated: a Milestone title is
    # arbitrary text, and a raw f-string produced invalid TOML for `"`,
    # or a re replacement escape error for `\`. json.dumps emits a TOML-
    # compatible basic string; the lambda keeps the replacement text out
    # of the regex escape layer entirely.
    serialized = json.dumps(new_value, ensure_ascii=False)
    updated, _ = pattern.subn(
        lambda _match: f"active_milestone = {serialized}", text, count=1,
    )
    config_path.write_bytes(updated.encode("utf-8"))

def _pending_milestone_issue(
    repo: str, old: str, candidates: list[dict], repo_dir: Path,
) -> None:
    """Create one idempotent human-confirmation issue for a milestone advance.

    The fingerprint check and create share the deployment checkout's lock.
    GitHub search can lag a create, so the open-issue search is repeated and
    any duplicate is closed in favour of the lowest issue number.
    """
    titles = [str(candidate["title"]) for candidate in candidates]
    fingerprint = f"orbi-milestone-advance old={old} candidates={','.join(titles)}"
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        existing = list_issues(
            repo, state="all", search=f'in:body "{fingerprint}"',
            json_fields="number", limit=1, timeout=30,
        )
        if existing:
            return
        lines = [
            "## Milestone 自动推进待人工确认",
            "",
            fingerprint,
            "",
            f"当前 milestone `{old}` 已完成，等待确认推进到以下候选版本：",
            "",
        ]
        lines.extend(
            f"- `{candidate['title']}`：{candidate.get('open_issues', 0)} open issues"
            for candidate in candidates
        )
        lines.extend([
            "",
            "请人工运行 `orbi milestone set <目标版本>` 推进 `active_milestone`"
            "（或恢复自动推进），然后关闭本 Issue。",
        ])
        run_command([
            "gh", "issue", "create", "--repo", repo,
            "--title", f"Milestone {old} 已完成，等待确认推进到 {titles[0]}",
            "--body", "\n".join(lines),
        ], timeout=30)
        open_issues = list_issues(
            repo, state="open", search=f'in:body "{fingerprint}"',
            json_fields="number", limit=200, timeout=30,
        )
        numbered = sorted(
            issue["number"] for issue in open_issues
            if isinstance(issue, dict) and isinstance(issue.get("number"), int)
        )
        if len(numbered) <= 1:
            return
        winner = numbered[0]
        for duplicate in numbered[1:]:
            run_command([
                "gh", "issue", "close", str(duplicate), "--repo", repo,
                "--comment", f"duplicate of #{winner}",
            ], timeout=30)
        event(
            "pending_milestone_issue_deduplicated",
            issue=f"#{winner}", duplicates=",".join(
                f"#{number}" for number in numbered[1:]
            ),
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _close_stale_milestone_issues(repo: str, active_milestone: str) -> None:
    """Close manual advance notices that no longer match the config."""
    issues = list_issues(
        repo, state="open", search='in:body "orbi-milestone-advance"',
        json_fields="number,body", limit=200, timeout=30,
    )
    pattern = re.compile(r"orbi-milestone-advance old=([^ ]+)")
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        body = issue.get("body")
        match = pattern.search(body) if isinstance(body, str) else None
        if match is None or match.group(1) == active_milestone:
            continue
        run_command([
            "gh", "issue", "close", str(issue["number"]), "--repo", repo,
            "--comment", (
                f"已收敛：当前配置 active_milestone = `{active_milestone}`。"
            ),
        ], timeout=30)
        event(
            "stale_milestone_issue_closed", issue=f"#{issue['number']}",
            active=active_milestone,
        )

def advance_active_milestone_on_idle(
    repo: str, active_milestone: str, config_path: Path,
    repo_dir: Path | None = None,
    *, auto_next_milestone: bool = True,
) -> tuple[str, str | None]:
    """Check and advance a configured milestone after no_ready_issue."""
    milestones = list_milestones(repo, timeout=30)
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and milestone.get("title") == active_milestone
    ]
    if not matches:
        open_list = ", ".join(
            f"{milestone.get('title')}({milestone.get('open_issues')})"
            for milestone in milestones
            if isinstance(milestone, dict) and milestone.get("state") == "open"
        ) or "(none)"
        raise RuntimeError(
            f"active_milestone_missing current={active_milestone}; "
            f"open milestones: {open_list}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"active_milestone {active_milestone}: ambiguous exact-title "
            f"match in {repo}, refusing to guess"
        )
    if matches[0].get("state") == "open":
        try:
            _close_stale_milestone_issues(repo, active_milestone)
        except Exception:
            # Closing an obsolete confirmation is notification maintenance;
            # it must not turn an otherwise successful idle tick into a
            # delivery failure.
            LOGGER.exception(
                "stale_milestone_issue_close_failed repo=%s active=%s",
                repo, active_milestone,
            )
        return "open", None
    current = _parse_version_title(active_milestone)
    candidates = []
    for milestone in milestones:
        if not isinstance(milestone, dict) or milestone.get("state") != "open":
            continue
        version = _parse_version_title(milestone.get("title"))
        if version is not None and current is not None and version > current:
            candidates.append((version, milestone.get("title")))
    if not candidates:
        event(
            "active_milestone_advance_none", current=active_milestone,
            closed=active_milestone, repo=repo,
        )
        return "closed", None
    candidates.sort()
    candidate_details = [
        milestone for _, title in candidates
        for milestone in milestones
        if isinstance(milestone, dict) and milestone.get("title") == title
    ]
    if not auto_next_milestone:
        candidate_titles = ",".join(title for _, title in candidates)
        event(
            "active_milestone_advance_pending", level=logging.WARNING,
            old=active_milestone, candidates=candidate_titles,
            auto_next_milestone="false",
        )
        try:
            _pending_milestone_issue(
                repo, active_milestone, candidate_details,
                repo_dir if repo_dir is not None else config_path.parent,
            )
        except Exception:
            # The confirmation Issue is an idle-path notification. Its
            # failure must not turn an otherwise successful no-ready tick
            # into a delivery failure.
            LOGGER.exception(
                "pending_milestone_issue_failed repo=%s old=%s",
                repo, active_milestone,
            )
        return "closed", None
    new_value = candidates[0][1]
    rewrite_active_milestone_line(config_path, new_value)
    event(
        "active_milestone_advanced", old=active_milestone, new=new_value,
        closed=active_milestone, repo=repo,
    )
    return "closed", new_value

def _publish_plan_milestone(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the `plan ready` milestone once the worktree has the plan
    artifact."""
    if (worktree / ".orbi" / "plan.md").is_file():
        publisher.milestone("plan ready")

def _test_result_failed(result: str) -> bool:
    """Classify a test result without treating prose or zero counts as errors."""
    exits = [int(value) for value in _TEST_EXIT_RE.findall(result)]
    if any(value != 0 for value in exits):
        return True

    counts = _TEST_OUTCOME_COUNT_RE.findall(result)
    if any(int(count) > 0 for count in counts):
        return True
    return bool(_TEST_FAILURE_EVIDENCE_RE.search(result))

def _publish_test_milestone(publisher: ProgressPublisher,
                            worktree: Path) -> None:
    """Post `tests passed` / `tests failed` from the worktree's test.log."""
    result = read_test_result(worktree)
    if result is None:
        return
    if _test_result_failed(result):
        publisher.milestone(f"tests failed: {result}")
    else:
        publisher.milestone(f"tests passed: {result}")
