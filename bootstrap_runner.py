#!/usr/bin/env python3
"""One-shot bootstrap runner for Muyan Pilot.

This is intentionally small. It claims one ready GitHub Issue, gives it to
Pi in an isolated worktree, and accepts success only when one open PR exists.
After the implementer opens the PR, the Runner closes the loop itself: it
freezes the exact PR base/head SHA, runs one independent review session that
reviews the diff AND fixes Blocker/Major findings in the same session
(modify code, run tests, push the task branch — Issue #82: no cold-start
fixer, no third review), re-freezes the head after a clean verdict,
re-checks the merge gate against the latest remote base, and merges via
`gh pr merge --match-head-commit`. Pi never pushes the protected branch;
the Runner is the only merge actor. Any command failure is logged and
raised. There is no fallback, queue, daemon, or multi-agent framework.

Throughout the whole lifecycle the Runner publishes live progress
automatically (Issue #18): one per-run GitHub progress comment carrying a
hidden run marker is PATCHed in place on every activity change and at most
every 30 seconds while any Pi session (implementer or reviewer) runs, and
short milestone comments (started, plan ready, tests passed/failed, review
findings, PR opened, merged, blocked) notify GitHub Mobile. No human
command, poll or status check is part of the normal workflow.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import select
import signal
import subprocess
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path

from git_transport import TransportError, check_transport
from pilot_slots import acquire_slot, slot_dir_for, slot_occupancy
from pi_recovery import (
    clk_tck,
    find_idle_descendants,
    pid_alive,
    process_start_monotonic,
    signal_pid,
    timeout_duration,
    upstream_alive,
)
from pi_activity import (
    SessionWatcher,
    activity_snapshot,
    format_duration,
    format_end_scene,
    format_run_scene,
    quote_value,
    sanitize,
)
from progress import ProgressPublisher, format_elapsed, progress_body
from systemd_deploy import (
    UnitDriftError,
    check_unit_drift,
    sync_drifted_units,
)


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (see review-fix-loop skill: max 5 rounds).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_REVIEW_ROUNDS = 5

# Live activity polling while Pi runs (Issue #24): every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0
# Automatic observability (Issue #18): the GitHub progress comment is
# PATCHed on every activity change and at most every 30 seconds while a
# Pi session runs, so a mobile user sees live progress without any
# command. The journal cadence is the poll interval above.
PI_HEARTBEAT_SECONDS = 30.0
# Idle warning (Issue #18): no model/session activity for 5 minutes
# (and the model is not expected to reply next) logs one `pi_idle`
# warning; the first new session event after it logs `pi_resumed`.
# A slow active model (model_wait, Issue #40) is never reported idle.
PI_IDLE_WARN_SECONDS = 300.0
# Upstream-dead detection (Issue #75): while the newest session event
# is a tool result (model_wait) the model is expected to reply next.
# A real (slow) model keeps producing session events; a dead upstream
# (llama/proxy timeout, connection drop) freezes the session JSONL
# while Pi sits in epoll_wait. Once the silence in model_wait reaches
# this threshold the upstream is declared dead: Pi is killed and the
# run fails fast through the normal failure path (the slot is released
# by the kernel when the Runner exits). This is NOT a business timeout:
# it only fires while the session file is frozen (stale seconds), never
# while events keep arriving — a slow generation survives.
PI_MODEL_WAIT_DEAD_SECONDS = 600.0
# Idle-stall recovery (Issue #94): a stalled (non-model_wait) session
# is recovered automatically instead of only warning. Measured in idle
# windows of `idle_warn_seconds`: at the first window the pre-idle
# descendants (the hung tools) get SIGTERM (the failure signal reaches
# the model), at the second window a target that survived gets
# SIGKILL, and after `PI_IDLE_RECOVERY_CYCLES` consecutive idle windows
# the Pi session itself is killed and the run fails fast through the
# normal `ai-blocked` path — the slot is never held forever.
PI_IDLE_RECOVERY_CYCLES = 3

# The bootstrap runner streams every Pi session of a run through the same
# live activity pipeline (Issue #24/#40); implement/review share the same
# line format and carry their role (Issue #41: one run_id end to end, the
# roles are steps of the same run). Issue #82 removed the cold-start fixer
# role: the review session fixes findings in the same session, so a run
# has at most two Pi sessions (implement, then review).
ROLE_IMPLEMENT = "implement"
ROLE_REVIEW = "review"

# Run correlation (Issue #41): one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_CURRENT_RUN_ID: str | None = None

# GitHub labels are the only state store (Issue #45). After a PR is
# opened the Issue is in a recoverable review state: `ai-pr-opened`
# means awaiting review, and the explicit `ai-fix-needed` state (a review
# finding the reviewer could not fix in-session, or a base conflict)
# means awaiting the next review session — Issue #82: the review session
# itself fixes findings in the same session, so both labels resume into
# the same independent review (no cold-start fixer). The next tick
# resumes that same run on the same branch, worktree and PR instead of
# claiming a new Issue. `ai-merged` is the success terminal state the
# Runner sets after it merges the PR itself (Issue #34).
IN_PROGRESS_LABEL = "ai-in-progress"
PR_OPENED_LABEL = "ai-pr-opened"
FIX_NEEDED_LABEL = "ai-fix-needed"
MERGED_LABEL = "ai-merged"
BLOCKED_LABEL = "ai-blocked"
# P0 urgent priority (Issue #101): a plain GitHub label, not a delivery
# state. It only orders the ready pickup (`ai-ready`+`p0` first); it
# never changes the delivery states, the blockedBy semantics or the
# terminal states. A failed P0 run enters `ai-blocked` like every other
# failed run — the ready scans exclude `ai-blocked`, so the `ai-ready`
# residue never re-enters the queue (no infinite retry).
P0_LABEL = "p0"
# Epic marker (Issue #93): the plain `ai-epic` label marks a
# coordination Issue (a release checklist or a multi-task grouping).
# An Epic is NOT an executable task: the ready claim scan skips it
# (structured `epic_not_claimed` line — no claim, no label change, no
# worktree, no run, no slot) and the restart-resume scan excludes it
# (a legacy Epic left behind with `ai-in-progress` must never be
# resumed into a run). The actual work lives in independent `ai-ready`
# sub-Issues (one runtime outcome, one PR each); the Epic's completion
# is judged from GitHub evidence (sub-Issues done, PRs merged, release
# tag on the remote, no leftover `ai-in-progress`) and closed by a
# human or a release task — never by the claim path.
EPIC_LABEL = "ai-epic"

# Only comments posted by a repo maintainer are trusted to carry the
# recovery scene: a public comment (authorAssociation=NONE) must never
# steer the runner into an arbitrary local worktree, branch or PR
# (Issue #45 review, BLOCKER). A missing association is never trusted.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({
    "OWNER", "MAINTAINER", "MEMBER", "COLLABORATOR",
})


class RunIdFilter(logging.Filter):
    """Prefix every log message with the current `[run_id]`, if bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT_RUN_ID is not None:
            record.msg = f"[{_CURRENT_RUN_ID}] {record.msg}"
        return True


LOGGER.addFilter(RunIdFilter())


def validate_run_id(run_id: object) -> str:
    """Fail fast unless `run_id` is the 8-hex id of one task attempt."""
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


def set_run_id(run_id: str) -> None:
    """Bind one task attempt: every later journal line carries `[run_id]`."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = validate_run_id(run_id)


def current_run_id() -> str | None:
    """Return the run id bound to this tick, or None before the claim."""
    return _CURRENT_RUN_ID


def run_marker(run_id: str) -> str:
    """Return the stable machine-readable run marker for GitHub text."""
    return f"<!-- muyan-pilot:run={validate_run_id(run_id)} -->"


def _config_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict:
    """Load the human-maintained TOML config and resolve its paths."""
    base = path.resolve().parent
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    source_repos = data.get("source_repos")
    if not isinstance(source_repos, list) or not source_repos:
        raise ValueError("source_repos must be a non-empty list")
    if not all(isinstance(repo, str) and repo for repo in source_repos):
        raise ValueError("source_repos must contain non-empty strings")
    base_branch = data.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("base_branch must be a non-empty string")
    # Claim scope (Issue #139): the active Milestone is an EXPLICIT
    # version scope for the fresh-claim scans — it is never guessed
    # from the repo's Milestone list. Absent (None) keeps the current
    # behavior exactly (compat); present it must be a non-empty
    # string, otherwise the config is a misconfiguration and the start
    # fails fast.
    active_milestone = data.get("active_milestone")
    if active_milestone is not None and (
        not isinstance(active_milestone, str) or not active_milestone
    ):
        raise ValueError("active_milestone must be a non-empty string")
    # Concurrency cap (Issue #39): the local machine can only serve a
    # limited number of concurrent tasks, so the default is 1. Any other
    # value must be a positive integer; fail fast on anything else.
    max_concurrency = data.get("max_concurrency", 1)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency < 1
    ):
        raise ValueError("max_concurrency must be a positive integer")
    # Optional Pi model selection (Issue #119): each key is absent -> None
    # (the Pi flag is not passed, Pi keeps its own default) or a non-empty
    # string passed to Pi verbatim. Anything else fails fast.
    pi_provider = _optional_pi_string(data, "pi_provider")
    pi_model = _optional_pi_string(data, "pi_model")
    pi_thinking = _optional_pi_string(data, "pi_thinking")
    # Optional Pi provider file (Issue #157): the provider metadata
    # (baseUrl / api / apiKey / models) lives in a separate JSON file in
    # Pi's own `models.json` shape; `muyan-pilot.toml` only selects the
    # provider/model/thinking used at runtime. Absent key -> None (Pi
    # keeps using its own agent dir, the exact pre-#157 behavior).
    pi_providers = _optional_pi_string(data, "pi_providers")
    pi_providers_path = (
        _config_path(pi_providers, base) if pi_providers is not None
        else None
    )
    pi_providers_data = (
        _load_pi_providers(pi_providers_path, pi_provider, pi_model)
        if pi_providers_path is not None else None
    )
    repo_dir = _config_path(data.get("repo_dir", "."), base)
    return {
        "source_repos": source_repos,
        "repo_dir": repo_dir,
        "workspace_root": _config_path(data.get("workspace_root", ".."), base),
        "prompt": _config_path(data.get("prompt", "prompt.md"), base),
        "prompt_review": _config_path(
            data.get("prompt_review", "prompt_review.md"), base,
        ),
        "skills": [_config_path(item, base) for item in data.get("skills", [])],
        "context_files": [
            _config_path(item, base) for item in data.get("context_files", [])
        ],
        "base_branch": base_branch,
        "active_milestone": active_milestone,
        "max_concurrency": max_concurrency,
        "slot_dir": slot_dir_for(repo_dir),
        "pi_provider": pi_provider,
        "pi_model": pi_model,
        "pi_thinking": pi_thinking,
        "pi_providers": pi_providers_path,
        "pi_providers_data": pi_providers_data,
        # Multi-repo registry (Issue #134): the explicit per-repo entries
        # (name, path, github, base_branch). Absent section -> [] so the
        # single-repo config keeps its exact shape and flow.
        "repositories": parse_repositories(data.get("repositories", []), base),
    }


def _optional_pi_string(data: dict, key: str) -> str | None:
    """Read one optional Pi model key (Issue #119).

    Absent -> None (the corresponding `pi --provider/--model/--thinking`
    flag is not passed and Pi keeps its own default). Present -> must be a
    non-empty string, passed to Pi verbatim; anything else fails fast.
    """
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _load_pi_providers(path: Path, pi_provider: str | None,
                       pi_model: str | None) -> dict:
    """Load and validate the Pi provider file (Issue #157).

    The file uses Pi's own `models.json` shape (`{"providers": {id:
    {baseUrl, api, apiKey, models: [...]}}}`) — verified against the
    installed Pi 0.84.3 docs (`docs/models.md`). Fail fast with a
    specific message: file missing, invalid JSON, missing `providers`
    object, a provider entry with `models` but no `baseUrl` or no
    `api` (provider- or model-level — Pi's own schema requirement),
    the selected provider/model not defined in the file, or an
    `apiKey` env-var reference (`$VAR` / `${VAR}`) whose variable is
    missing or empty. The key value itself is never logged; only the
    variable name is named in the error.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"pi_providers file {path} is not valid JSON: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    for provider_id, entry in providers.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"pi_providers file {path}: provider {provider_id!r} "
                "must be an object"
            )
        models = entry.get("models")
        if models is not None:
            if not isinstance(models, list) or not models:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "models must be a non-empty list"
                )
            if not isinstance(entry.get("baseUrl"), str) or not entry["baseUrl"]:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "is missing baseUrl"
                )
            if not isinstance(entry.get("api"), str) or not entry["api"]:
                if not all(
                    isinstance(model, dict)
                    and isinstance(model.get("api"), str)
                    and model["api"]
                    for model in models
                ):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} is missing api"
                    )
            for model in models:
                if not isinstance(model, dict) or not model.get("id"):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} has a model without an id"
                    )
    if pi_provider is not None:
        if pi_provider not in providers:
            raise ValueError(
                f"pi_provider {pi_provider!r} is not defined in "
                f"pi_providers file {path}"
            )
        entry = providers[pi_provider]
        # Only the SELECTED provider's key must resolve: an unselected
        # provider with a missing key just stays unavailable in Pi
        # (verified against real Pi 0.84.3), it never breaks the run.
        _check_pi_provider_api_key(path, pi_provider, entry)
        if pi_model is not None:
            model_ids = [
                model["id"] for model in entry.get("models", [])
            ] if isinstance(entry.get("models"), list) else []
            if pi_model not in model_ids:
                raise ValueError(
                    f"pi_model {pi_model!r} is not defined for provider "
                    f"{pi_provider!r} in pi_providers file {path}"
                )
    return data


def _check_pi_provider_api_key(path: Path, provider_id: str,
                               entry: dict) -> None:
    """Fail fast when an `apiKey` env-var reference is unresolved.

    Pi's value-resolution syntax (`docs/models.md`): `$VAR` or
    `${VAR}` interpolates the named environment variable; a missing
    variable leaves the value unresolved and Pi would only fail at
    request time. The Runner fails at config load instead (before any
    slot or claim). Only the variable NAME is reported — never a key
    value.
    """
    api_key = entry.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        return
    for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", api_key):
        name = match.group(1) or match.group(2)
        value = os.environ.get(name)
        if not value:
            raise ValueError(
                f"API key for provider {provider_id!r} references "
                f"missing environment variable {name} "
                f"(pi_providers file {path})"
            )


def prepare_pi_agent_dir(worktree: Path, config: dict) -> Path | None:
    """Materialize the per-run Pi agent dir (Issue #157).

    Returns None when no provider file is configured — the Pi command
    and environment keep their exact pre-#157 shape (Pi uses its own
    agent dir). Otherwise creates `<worktree>/.muyan-pilot/pi-agent/`
    (gitignored, per-run) and returns it:

    - `models.json`: the user agent dir's providers merged with the
      configured file's providers (the file wins on id collision) —
      the user's existing providers keep working, the file adds or
      overrides; the merged catalog is what Pi loads via
      `PI_CODING_AGENT_DIR` (verified against real Pi 0.84.3);
    - `settings.json` / `auth.json`: SYMLINKS to the user agent dir's
      files when they exist, so Pi's other behavior (settings, stored
      auth) is unchanged apart from the provider catalog.

    The dir holds no secrets: the API key stays an env-var reference in
    the file and never a materialized value.
    """
    providers_data = config.get("pi_providers_data")
    if providers_data is None:
        return None
    agent_dir = worktree / ".muyan-pilot" / "pi-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    user_agent = Path(os.path.expanduser("~")) / ".pi" / "agent"
    merged_providers: dict = {}
    user_models = user_agent / "models.json"
    if user_models.is_file():
        try:
            user_data = json.loads(user_models.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"user agent dir models.json {user_models} is not valid "
                f"JSON: {exc}"
            ) from None
        user_providers = user_data.get("providers") if isinstance(
            user_data, dict
        ) else None
        if not isinstance(user_providers, dict):
            user_providers = {}
        merged_providers.update(user_providers)
    merged_providers.update(providers_data["providers"])
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": merged_providers}, indent=2),
        encoding="utf-8",
    )
    # Idempotent for a resumed run in the same worktree: a stale
    # symlink from an earlier attempt is replaced, never kept.
    for name in ("settings.json", "auth.json"):
        source = user_agent / name
        link = agent_dir / name
        if link.is_symlink():
            link.unlink()
        if source.is_file():
            link.symlink_to(source)
    return agent_dir


def _pi_model_args(config: dict) -> list[str]:
    """Return the configured Pi model flags (Issue #119).

    One `--flag value` pair per configured key, in the fixed order
    provider, model, thinking; an unset key contributes nothing, so a
    config without any of the three keys returns [] and the Pi command
    keeps its exact pre-#119 shape. The values are non-sensitive model
    identifiers (never keys or tokens) and are part of the redacted
    `log_command`, so the journal run scene records what was launched.
    """
    args: list[str] = []
    for flag, key in (
        ("--provider", "pi_provider"),
        ("--model", "pi_model"),
        ("--thinking", "pi_thinking"),
    ):
        value = config.get(key)
        if value is not None:
            args.extend((flag, value))
    return args


def parse_repositories(entries: object, base: Path) -> list[dict]:
    """Parse the explicit multi-repo registry (Issue #134).

    Each entry is a TOML table with the required string fields `name`,
    `path`, `github` and `base_branch`; `path` is resolved relative to
    the config file's directory. A missing/empty/non-string field, a
    non-table entry or a duplicate `name` fails fast — the existence and
    Git-checkout checks happen in `validate_config`.
    """
    if not isinstance(entries, list):
        raise ValueError("repositories must be a list of tables")
    repos: list[dict] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"repositories[{index}] must be a table")
        missing = [
            field for field in ("name", "path", "github", "base_branch")
            if not isinstance(entry.get(field), str) or not entry.get(field)
        ]
        if missing:
            raise ValueError(
                f"repositories[{index}] is missing required field(s): "
                + ", ".join(missing)
            )
        if any(repo["name"] == entry["name"] for repo in repos):
            raise ValueError(
                f"duplicate repositories name: {entry['name']!r}"
            )
        repos.append({
            "name": entry["name"],
            "path": _config_path(entry["path"], base),
            "github": entry["github"],
            "base_branch": entry["base_branch"],
        })
    return repos


def render_prompt(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def validate_config(config: dict) -> None:
    if not config["repo_dir"].is_dir():
        raise FileNotFoundError(config["repo_dir"])
    for path in [
        config["prompt"], config["prompt_review"],
        *config["skills"], *config["context_files"],
    ]:
        if not path.is_file():
            raise FileNotFoundError(path)
    # Multi-repo registry (Issue #134): every registered path must exist
    # and be a Git checkout — a `.git` directory (a plain checkout) or a
    # `.git` file (a linked worktree). Absent key -> no registry, so a
    # single-repo config keeps its exact flow.
    for repo in config.get("repositories", []):
        path = repo["path"]
        if not path.is_dir():
            raise FileNotFoundError(path)
        if not (path / ".git").exists():
            raise ValueError(
                f"repositories entry {repo['name']!r}: {path} "
                "is not a git checkout"
            )


def single_line(value: str) -> str:
    """Flatten a log value to one journal line (Issue #143).

    A command argument may carry line breaks (the multi-line progress
    comment body behind `gh api ... --field body=...`); emitted verbatim,
    they split one `command=` log into several systemd journal lines with
    the same timestamp and PID. Escape each line break to the visible
    two-character sequence `\\n` so the field content stays readable on
    one line. This only changes the log display — the real command is
    never modified.
    """
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int | None = None,
                log_command: list[str] | None = None,
                log_stdout: bool = False) -> str:
    """Run one external command; log context and fail fast on any error."""
    LOGGER.info(
        "command=%s cwd=%s",
        single_line(" ".join(log_command or command)), cwd or Path.cwd(),
    )
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "command_failed returncode=%s stdout=%s stderr=%s",
            exc.returncode, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        LOGGER.error(
            "command_timeout timeout=%s stdout=%s stderr=%s",
            timeout, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except OSError as exc:
        LOGGER.error("command_spawn_failed error=%s", exc)
        raise
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    if log_stdout and result.stdout:
        LOGGER.info("stdout=%s", result.stdout.rstrip())
    return result.stdout.strip()


def parse_issue_array(raw: str) -> list[dict]:
    """Return the issue array from gh's JSON output."""
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("issue list must be a JSON array")
    return issues


def parse_issue_list(raw: str) -> dict | None:
    """Return the first issue from gh's JSON array, or None when idle."""
    issues = parse_issue_array(raw)
    return issues[0] if issues else None


def open_blocker_numbers(issue: dict) -> list[int]:
    """Return the numbers of the issue's OPEN native GitHub blockers.

    `gh issue list --json blockedBy` (gh 2.94+) carries the native
    dependency relation as `{"nodes": [...], "totalCount": N}`. GitHub
    keeps a relation listed after its blocker closes (the node then
    carries `state: "CLOSED"` and is inert — verified against the live
    API, Issue #54), so only OPEN blockers actually block: a closed
    blocker clears the dependency without any runner-side bookkeeping,
    and the next tick claims the Issue. A node without an explicit
    `state` counts as open (claiming a possibly-blocked Issue costs a
    full run; waiting one tick does not). A missing or malformed field
    means "no known blockers" (fail open): an API shape change must
    never deadlock the queue.
    """
    blocked_by = issue.get("blockedBy")
    if not isinstance(blocked_by, dict):
        return []
    nodes = blocked_by.get("nodes")
    if not isinstance(nodes, list):
        return []
    numbers: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        number = node.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if node.get("state", "OPEN") != "OPEN":
            continue
        numbers.append(number)
    return numbers


# Ready scans (Issue #71/#101): P0 urgent Issues are claimed before
# bugs, bugs before new features — if the delivery loop is broken,
# claiming enhancements only piles up unreviewed PRs, and a production
# outage (P0) must not wait behind ordinary work. The P0 scan runs
# first, then the bug scan, then the plain ready scan — each with the
# exact same exclusions. No priority numbers, no separate queue, no
# new state machine: three `gh issue list` searches with the same
# blockedBy semantics (Issue #54). `p0` is a plain label, not a
# delivery state: it only orders the pickup (Issue #101).
READY_SCAN_EXCLUSIONS = (
    f"-label:{IN_PROGRESS_LABEL} -label:{PR_OPENED_LABEL} "
    f"-label:{FIX_NEEDED_LABEL} -label:{MERGED_LABEL} "
    f"-label:{BLOCKED_LABEL}"
)


def ready_searches(active_milestone: str | None = None) -> tuple[str, str, str]:
    """Return the three ready scans (p0, bug, plain) in pickup order.

    With a configured `active_milestone` (Issue #139) every scan
    carries the `milestone:"<title>"` qualifier — the quoted form is
    the contract because milestone titles may contain spaces or
    special characters (verified against the live API). The scope is
    part of the QUERY, so an Issue of another Milestone (or of no
    Milestone) never enters the result set, and a `v0.2.0` Issue
    without `ai-ready` never does either: the `label:ai-ready`
    qualifier stays. The Milestone is a version scope, not a
    replacement for the `ai-ready` execution switch. P0 does NOT
    cross milestones (Issue #139 decision): the active Milestone is
    the claim scope of EVERY fresh claim, and `p0` only orders the
    pickup inside it — one uniform rule, no special case. Without a
    configured Milestone the searches are byte-identical to the
    pre-#139 scans (compat).
    """
    scope = (
        f' milestone:"{active_milestone}"' if active_milestone else ""
    )
    return (
        f"label:ai-ready label:{P0_LABEL}{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:ai-ready label:bug{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:ai-ready{scope} {READY_SCAN_EXCLUSIONS}",
    )


def issue_priority(issue: dict) -> str:
    """Return the pickup priority of one issue (Issue #101).

    `p0` when the issue carries the `p0` label, `normal` otherwise.
    The ready/in-flight/resumable scans fetch `labels` (verified
    against `gh issue list --help`: `labels` is a supported JSON
    field, an array of `{name, ...}` nodes), so this is a pure
    function of the scanned issue — no extra gh call. A missing or
    malformed `labels` field fails to `normal` (like the blockedBy
    field fails open): a P0 misread as normal only loses its ordering
    for one run, never the delivery.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return "normal"
    for label in labels:
        if isinstance(label, dict) and label.get("name") == P0_LABEL:
            return "p0"
    return "normal"


def is_epic(issue: dict) -> bool:
    """Return True when one issue carries the `ai-epic` label (Issue #93).

    A pure function of the issue's `labels` (the scans fetch `labels`,
    so no extra gh call), the same style as `issue_priority`. A
    missing or malformed `labels` field fails open to "not an epic":
    the scan always requests `labels`, so a shape change only loses
    the Epic guard for one run — it must never deadlock the queue.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == EPIC_LABEL:
            return True
    return False


def pick_issue(repo: str, active_milestone: str | None = None) -> dict | None:
    # A merged delivery keeps `ai-ready` + `ai-merged` on the (still
    # open) Issue; `ai-merged` is the success terminal state, so it is
    # excluded from the ready scan like every other delivery state.
    # The scan fetches the ready queue (not just the first Issue) and
    # reads the native GitHub dependency per Issue (Issue #54): an
    # Issue with open blockers is skipped — no claim, no label change,
    # no worktree — and the next ready Issue is considered instead.
    # Issue #93: an `ai-epic` Issue is skipped too — no claim, no
    # label change, no worktree, no run, no slot — with the structured
    # `epic_not_claimed` line (the Epic check precedes the blockedBy
    # check). Issue #71/#101: the P0 scan runs first, then the bug
    # scan; a P0 or bug with open blockers is skipped there and the
    # next scan still decides. The scan also fetches `labels` so the
    # picked issue's priority (and Epic-ness) is visible without an
    # extra gh call. Issue #139: with a configured `active_milestone`
    # all three scans are scoped to that Milestone in the query itself
    # (see `ready_searches`) — the Epic skip and the blockedBy skip
    # above are the unchanged second (code) layer, and a failed scan
    # still fails open (never a silent claim of the wrong version).
    for search in ready_searches(active_milestone):
        try:
            raw = run_command([
                "gh", "issue", "list", "--repo", repo, "--state", "open",
                "--search", search,
                "--json", "number,title,body,labels,blockedBy",
                "--limit", "200",
            ])
            issues = parse_issue_array(raw)
        except Exception as exc:
            # Fail open (Issue #54): a failed blockedBy query must
            # never deadlock the queue. This tick claims nothing from
            # this repo and the next tick retries the query; the error
            # is logged, never raised, and no label is touched.
            LOGGER.error(
                "blocked_by_check_failed repo=%s error=%s",
                repo, exc,
            )
            return None
        for issue in issues:
            # Epic skip (Issue #93): an `ai-epic` Issue is never
            # claimed — no label change, no worktree, no run, no slot.
            # The check precedes the blockedBy check: "it is an Epic"
            # is the more fundamental reason, so the structured
            # `epic_not_claimed` line (not a `blocked_by` line) is the
            # recorded cause.
            if is_epic(issue):
                LOGGER.info(
                    "epic_not_claimed issue=%s repo=%s",
                    issue.get("number"), repo,
                )
                continue
            blockers = open_blocker_numbers(issue)
            if blockers:
                LOGGER.info(
                    "blocked_by issue=%s repo=%s blockers=%s",
                    issue.get("number"), repo,
                    ",".join(str(number) for number in blockers),
                )
                continue
            # The pickup log carries the explicit priority field
            # (Issue #101): `p0` for urgent Issues, `normal` otherwise.
            LOGGER.info(
                "picked issue=%s repo=%s priority=%s",
                issue.get("number"), repo, issue_priority(issue),
            )
            return issue
    return None


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> dict | None:
    """Return one in-flight Issue a killed runner left behind.

    A SIGKILLed runner leaves the task worktree and the `ai-in-progress`
    claim label behind (the failure path never ran); the Issue keeps
    `ai-ready` too (a claim never removes it). The ready scan excludes
    `ai-in-progress`, so without this scan the run is never resumed and
    the Issue is stuck forever — the Issue #18 acceptance "restart finds
    the same progress comment by run marker" would be unreachable in the
    production flow. `process_issue`'s resume block (newest worktree's
    run id) then reuses the run instead of starting a second one. Every
    other delivery state is excluded: those Issues are owned by the
    resumable-PR scan (`ai-fix-needed`) or are terminal. `ai-epic` is
    excluded too (Issue #93): a legacy Epic left behind with
    `ai-in-progress` (the #80 scene, before the Epic mechanism existed)
    must never be resumed into a run — an Epic is coordination, not an
    executable task.

    The scan runs only when no OTHER runner is live: a slot held by
    another process proves a live runner is working (on this or another
    Issue), so the `ai-in-progress` label is in flight, not orphaned —
    resuming it here would start a second Pi for a run that is alive
    (Issue #39 slot semantics: the flock lock is the source of truth).
    This runner's own slot is excluded: `main` took it before the claim
    scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        "label:ai-ready label:ai-in-progress "
        f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
        f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL} "
        f"-label:{EPIC_LABEL}",
        # `labels` (Issue #101): a P0 a killed runner left behind
        # keeps its priority in the progress comment on resume.
        "--json", "number,title,body,labels", "--limit", "1",
    ])
    return parse_issue_list(raw)


def pick_next_issue(
    repos: list[str], active_milestone: str | None = None,
) -> tuple[str, dict] | None:
    """Scan sources in order; return the first ready issue and its source."""
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue
    return None


def edit_issue(number: int, *, repo: str, add: str | None = None,
               remove: str | None = None) -> None:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if add:
        command += ["--add-label", add]
    if remove:
        command += ["--remove-label", remove]
    run_command(command)


def comment_issue(number: int, *, repo: str, body: str) -> None:
    run_command(["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", body])


def issue_comments(number: int, *, repo: str) -> list[dict]:
    """Return the Issue's comment history (oldest first) from GitHub.

    ``gh issue view --json comments`` returns a top-level object with a
    ``comments`` array; each comment carries the author and the
    ``authorAssociation`` of the viewer, which is how the runner tells
    its own trusted comments apart from public ones (Issue #45).
    """
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("issue comments must be a JSON array")
    return comments


def new_run_id() -> str:
    """Return a unique short run identifier for one task attempt."""
    return uuid.uuid4().hex[:8]


def issue_context(source_repo: str, number: int) -> str:
    """Issue reference used on every journal line: `owner/repo#number`."""
    return f"{source_repo}#{number}"


def log_format() -> str:
    """Journal log format without a Python timestamp (Issue #40).

    systemd journal already provides time, host and process on every
    line; printing `%(asctime)s` again only duplicates information.
    """
    return "%(levelname)s %(message)s"


def freeze_base(repo_dir: Path, base_branch: str) -> str:
    """Fetch the remote and freeze the exact SHA of origin/<base_branch>."""
    run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
    return run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )


def task_branch(source_repo: str, number: int, run_id: str) -> str:
    return (
        f"muyan-pilot/{source_repo.replace('/', '-')}-issue-{number}-{run_id}"
    )


def started_pi_comment_body(run_id: str, run_info: str, branch: str,
                            worktree: Path) -> str:
    """The start comment doubles as the recoverable run scene (Issue #45)."""
    return (
        f"{run_marker(run_id)}\n"
        f"Muyan Pilot started Pi: {run_info} branch={branch} "
        f"worktree={worktree}"
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR (Issue #45). The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id — a comment must never be able to name a local path.
    """
    return (
        f"{run_marker(run_id)}\n"
        f"Muyan Pilot opened PR: {pr_url} ({run_info})"
    )


OPENED_PR_PREFIX = "Muyan Pilot opened PR: "


def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Muyan Pilot opened PR:` comment into a resume scene.

    Returns None when the body is not an opened-PR comment. Fails fast
    when the comment is malformed: resuming must recover the exact run
    (run id, base, PR URL), never a guess (Issue #45). Branch and
    worktree are not parsed: the runner derives them from its own
    config, the Issue number and the run id, so a comment can never
    name an arbitrary local path.
    """
    if not isinstance(body, str) or OPENED_PR_PREFIX not in body:
        return None
    head = body.split(OPENED_PR_PREFIX, 1)[1]
    pr_url, _, fields_part = head.partition(" (")
    fields: dict[str, str] = {}
    for part in fields_part.rstrip(")").split():
        key, _, value = part.partition("=")
        if key:
            fields[key] = value
    scene = {
        "pr_url": pr_url.strip(),
        "base_branch": fields.get("base_branch", ""),
        "base_sha": fields.get("base_sha", ""),
        "run_id": fields.get("run_id", ""),
    }
    for key, value in scene.items():
        if not value:
            raise ValueError(f"opened PR comment is missing {key}")
    scene["run_id"] = validate_run_id(scene["run_id"])
    return scene


def _comment_is_trusted(comment: object) -> bool:
    """True only when the comment carries a trusted maintainer association."""
    if not isinstance(comment, dict):
        return False
    return comment.get("authorAssociation") in TRUSTED_COMMENT_ASSOCIATIONS


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only comments posted by a trusted maintainer (OWNER, MAINTAINER,
    MEMBER or COLLABORATOR) are considered: a public comment can never
    become the recovery scene (Issue #45 review, BLOCKER). Fails fast
    when no trusted comment carries the scene: such an Issue cannot be
    resumed and must not be guessed at.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        scene = parse_pr_comment(comment.get("body"))
        if scene is not None:
            return scene
    raise ValueError(
        "no 'Muyan Pilot opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )


def pick_resumable_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> tuple[dict, dict] | None:
    """Return the newest opened-PR delivery and its resume scene.

    Both opened-PR states are scanned (Issue #70): `ai-fix-needed`
    (awaiting the next review session after a finding or a base
    conflict — Issue #82: the review session fixes findings in the same
    session, so the next tick runs the same independent review on the
    same branch, worktree and PR) and `ai-pr-opened` (awaiting review —
    the next tick runs the independent review). The `ai-pr-opened`
    scan exists because the delivery that opened the PR can be gone: the
    progress-publishing failure behind Issue #70 used to label the
    delivered Issue `ai-blocked` before the review started, and a killed
    runner can die inside the delivery wait loop, leaving a valid
    MERGEABLE PR with no owner. Without the scan such a delivery is
    picked up by no other scan (`pick_issue` excludes `ai-pr-opened`)
    and is stranded forever. `ai-blocked` Issues are excluded (they need
    a human decision first), as are merged and in-flight Issues and
    closed Issues. A scene that cannot
    be recovered is an unresolvable state: the Issue is marked
    `ai-blocked` with the concrete reason and the error re-raised, so
    the tick stops instead of silently skipping the delivery while a
    fresh task starts ahead of it.

    The scan runs only when no OTHER runner is live (the same guard as
    `pick_in_progress_issue`, Issue #39 slot semantics): a slot held by
    another process proves a live runner is working, so an opened-PR
    delivery is in flight, not stranded — resuming it here would start
    a second review Pi in the same worktree/branch/run, and the second
    `gh pr merge --match-head-commit` on the already-merged PR would
    fail and mark the merged Issue `ai-blocked` (Issue #70 review
    round 1). This runner's own slot is excluded: `main` took it
    before the claim scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        # `label:a,b` is GitHub's OR within one label qualifier
        # (verified live: repeating the qualifier matches only the
        # first label).
        f"label:{FIX_NEEDED_LABEL},{PR_OPENED_LABEL} "
        f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL} "
        f"-label:{IN_PROGRESS_LABEL}",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        "--json", "number,title,state,url,labels", "--limit", "1",
    ])
    issues = parse_issue_array(raw)
    if not issues:
        return None
    issue = issues[0]
    if issue.get("state") != "OPEN":
        return None
    comments = issue_comments(int(issue["number"]), repo=repo)
    try:
        scene = resume_scene(comments)
    except ValueError as exc:
        block_scene_failure(issue, exc, repo, comments)
    return issue, scene


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an `ai-fix-needed` Issue `ai-blocked` when its scene is
    malformed, then re-raise so the tick stops (Issue #45).

    The failure comment carries the run marker recovered from a trusted
    comment when it is present — the same run id, never a new or
    guessed one. The PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    LOGGER.error(
        "issue=%s resume scene is malformed: %s", number, error,
    )
    marker = ""
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = re.search(r"<!-- muyan-pilot:run=([0-9a-f]{8}) -->", body)
        if match:
            marker = run_marker(match.group(1))
            break
    try:
        edit_issue(
            number, repo=repo, add=BLOCKED_LABEL, remove=FIX_NEEDED_LABEL,
        )
        comment_issue(
            number, repo=repo,
            body=(
                f"{marker}\n" if marker else ""
            ) + f"Muyan Pilot failed: {error}",
        )
    except Exception:
        LOGGER.exception("issue=%s failure reporting failed", number)
    raise error


def pick_next_delivery(
    repos: list[str], slot_dir: Path, max_concurrency: int,
    active_milestone: str | None = None,
) -> tuple[str, dict, dict | None] | None:
    """Scan sources in order: resumable PRs, in-flight restarts, ready.

    Returns `(source_repo, issue, scene)` where `scene` is None for a
    fresh claim or a restart resume. Resuming an open PR keeps the
    single concurrency slot occupied by the same run (implement →
    review → fix → merge), so a second Pi is never started for a run
    that already has a PR. An in-flight Issue (a killed runner left
    `ai-in-progress` behind, Issue #18) is recovered before the ready
    scan: `process_issue`'s resume block reuses the newest worktree's
    run id, so the same progress comment is kept instead of a second
    run being started on an Issue that is already in flight.

    Issue #139: `active_milestone` scopes only the FRESH claim
    (`pick_issue`) — the resumable-PR and in-flight restart scans are
    resume states, and running an in-flight or opened-PR delivery to
    completion is never gated by a Milestone change.
    """
    for repo in repos:
        selected = pick_resumable_delivery(
            repo, slot_dir, max_concurrency,
        )
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        issue = pick_in_progress_issue(
            repo, slot_dir, max_concurrency,
        )
        if issue is not None:
            return repo, issue, None
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue, None
    return None


def worktree_path(repo_dir: Path, source_repo: str, number: int,
                  run_id: str) -> Path:
    """Task worktrees live in the configured repo's .worktrees/ directory."""
    slug = source_repo.replace("/", "-")
    return (
        repo_dir / ".worktrees"
        / f"muyan-pilot-{slug}-issue-{number}-{run_id}"
    )


def create_worktree(repo_dir: Path, source_repo: str, number: int,
                    run_id: str, base_sha: str) -> Path:
    """Create the task worktree from the frozen base SHA, never HEAD.

    An existing path is reused: only a resumed run (same run id after a
    process restart) reaches that state, and its worktree is the scene
    the run continues in (Issue #18).
    """
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        return path
    branch = task_branch(source_repo, number, run_id)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), base_sha,
    ], cwd=repo_dir)
    return path


def latest_run_id(repo_dir: Path, source_repo: str, number: int) -> str | None:
    """Return the run id of the newest task worktree for the issue.

    The worktree directory name carries the run id — the only state
    needed to resume the same GitHub progress comment (hidden run
    marker) instead of creating a second one (Issue #18).
    """
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/muyan-pilot-{slug}-issue-{number}-*"
    candidates = [
        path for path in repo_dir.glob(pattern) if path.is_dir()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return newest.name.rsplit("-", 1)[-1]


def has_in_progress_label(number: int, repo: str) -> bool:
    """True when the Issue still carries `ai-in-progress`.

    The label is added at claim time and removed only by the success or
    failure path, so it is the marker of a run that is (or was, when
    the runner died) in flight — as opposed to the preserved worktrees
    of completed runs (Issue #18).
    """
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "all",
        "--search", f"label:{IN_PROGRESS_LABEL}",
        "--json", "number", "--limit", "50",
    ])
    issues = parse_issue_array(raw)
    return any(int(issue.get("number", -1)) == number for issue in issues)


def _drain_stream(stream, chunks: list[bytes]) -> None:
    """Read a pipe to EOF, appending chunks (process must be finished)."""
    while True:
        data = os.read(stream.fileno(), 65536)
        if not data:
            return
        chunks.append(data)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")


def _log_activity(activity: dict, *, issue_ref: str,
                  role: str, state: str | None = None) -> None:
    """Log one short activity line with the changed fields only.

    No `run=` field (Issue #57): the `[run_id]` prefix added by
    `RunIdFilter` (Issue #41) is the single run-id carrier on the
    high-frequency lines, so the id appears exactly once per line.
    """
    LOGGER.info(
        "activity issue=%s role=%s phase=%s action=%s result=%s "
        "state=%s idle=%s",
        issue_ref, role, activity["phase"],
        quote_value(activity["action"] or "-"),
        activity["result"] or "-",
        state or "-",
        format_duration(activity["stale_seconds"]),
    )


def _log_heartbeat(activity: dict, *, issue_ref: str,
                   role: str, elapsed: float,
                   state: str | None = None) -> None:
    """Log one heartbeat line when nothing changed since the last poll.

    `state` carries the model_wait flag while the model is expected to
    reply next, so a slow active model is not reported as idle (Issue
    #40). No `run=` field (Issue #57): the `[run_id]` prefix is the
    single run-id carrier on the high-frequency lines.
    """
    LOGGER.info(
        "heartbeat issue=%s role=%s phase=%s state=%s elapsed=%s "
        "idle=%s",
        issue_ref, role, activity["phase"], state or "-",
        format_duration(elapsed), format_duration(activity["stale_seconds"]),
    )


def _pending_timeout_targets(targets: list[dict]) -> list[tuple[dict, float]]:
    """The pre-idle descendants still INSIDE an explicit `timeout`
    deadline (Issue #169): `[(target, deadline_epoch), ...]`.

    A descendant whose command line carries a coreutils
    `timeout <seconds>` wrapper and whose deadline is still in the
    future is a legitimately running tool — the runner waits for the
    deadline instead of signaling it. The age is measured CLOCK
    CONSISTENTLY (Issue #169): the process's monotonic start offset
    (stat field 22) against `time.monotonic()`, never the
    realtime-flavoured `process_start_epoch` — a realtime step after
    boot (NTP) must not make a tool look older than it is. A
    descendant without a clear timeout, whose start time is unreadable,
    or whose deadline already passed is not pending: the existing
    escalation applies to it.
    """
    if not targets:
        return []
    hz = clk_tck()
    now_mono = time.monotonic()
    now = time.time()
    pending: list[tuple[dict, float]] = []
    for target in targets:
        start_mono = process_start_monotonic(target["pid"], hz=hz)
        if start_mono is None:
            continue
        duration = timeout_duration(target["cmdline"] or "")
        if duration is None:
            continue
        remaining = duration - (now_mono - start_mono)
        if remaining > 0:
            pending.append((target, now + remaining))
    return pending


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS,
    model_wait_dead_seconds: float = PI_MODEL_WAIT_DEAD_SECONDS,
    run_id: str,
    issue: int,
    source_repo: str,
    branch: str,
    role: str = ROLE_IMPLEMENT,
    log_command: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
    pi_env: dict[str, str] | None = None,
) -> str:
    """Run Pi and stream concise live activity into the journal (Issue #40).

    The full invariant scene (branch, worktree, session file) is logged
    once as `run_start`. While Pi runs, only short changed fields are
    logged: `activity` when phase/action/result change, `heartbeat` at
    the poll interval otherwise (the idle time rides on the line). A slow
    active model is not reported as idle: when the newest session event
    is a tool result the state is `model_wait` (one transition line on
    entry, one `resumed` line when the next session event arrives, and
    only configured-interval heartbeats while waiting — no warning
    spam). On a non-zero exit or timeout a `run_failed` line carries the
    full scene again as the debug entry. The caller logs `run_end` once
    the PR and commit are known. The session JSONL stays in the worktree
    as the complete local record; the full prompt and Issue body are
    never logged.

    `progress` (Issue #18) is invoked on EVERY poll — an activity change
    or a heartbeat — with the current activity state, while the Pi
    process is still running: the caller renders the live GitHub
    progress comment and PATCHes the same run-marker comment in place,
    so mobile users never see a static starting comment for the whole
    run. A callback error is logged and never interrupts the task
    (observability is best-effort, the delivery is not).

    Idle warning (Issue #18): when no model/session event arrives for
    `idle_warn_seconds` (default 5 minutes) and the state is NOT
    model_wait, ONE `pi_idle` WARNING carries `stale_seconds`; the
    first new session event after it logs `pi_resumed`. A slow active
    model (model_wait) is never reported idle (Issue #40).

    Idle-stall recovery (Issue #94): the warning is no longer the end
    of the story. While the session stays stalled (no new activity,
    not model_wait) the runner recovers it, one step per idle window
    of `idle_warn_seconds` since the stall was first seen:
    window 1 SIGTERMs the Pi descendants that already existed before
    the window (the hung tools — found by the ppid chain in
    `/proc/<pid>/stat` plus their start time, never a name guess) so
    the tool gets a non-zero exit and the failure signal reaches the
    model; window 2 SIGKILLs a target that survived; after
    `PI_IDLE_RECOVERY_CYCLES` (default 3) consecutive idle windows the
    Pi session itself is killed and the run fails fast through the
    normal failure path (`ai-blocked`, the slot released) — the slot
    is never held forever. Every step logs a `pi_idle_term` /
    `pi_idle_kill` line (run id, pid, cmdline, result) and the live
    progress comment shows the recovery state via the `recovery`
    activity field. The first new session event resets the whole
    recovery state (`pi_resumed`).
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    issue_ref = issue_context(source_repo, issue)
    session_dir = cwd / ".pi-session"
    # Session files that already exist before this Pi process starts are
    # never followed: a resumed run (same worktree) creates a NEW JSONL,
    # and the journal must report the session of the current invocation,
    # not the previous run's (Issue #45 round-5 review, Major 3).
    known_files = (
        {path for path in session_dir.glob("*.jsonl") if path.is_file()}
        if session_dir.is_dir() else set()
    )
    watcher = SessionWatcher(session_dir, known_files=known_files)
    start = time.monotonic()
    # The initial state is what run_start already reported; activity lines
    # are only emitted when the visible fields actually change.
    initial = watcher.poll()
    last_visible = (initial["phase"], initial["action"], initial["result"])
    # Issue #157: `pi_env` carries the per-run Pi agent dir
    # (`PI_CODING_AGENT_DIR`, verified against real Pi 0.84.3) so the
    # configured provider file is visible to Pi. Absent -> the process
    # inherits the Runner's environment unchanged (pre-#157 shape).
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=None if pi_env is None else {**os.environ, **pi_env},
    )
    LOGGER.info(
        "run_start %s",
        format_run_scene(
            initial, run_id=run_id, issue=issue_ref,
            role=role, branch=branch, worktree=str(cwd),
        ),
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    activity = watcher.poll()
    timed_out = False
    upstream_dead = False
    # model_wait transitions (Issue #40): one line when the state is
    # entered and one when it is left; unchanged polls are heartbeats
    # that carry the state, so a slow model never looks idle and no
    # warning is ever escalated from a slow response.
    last_model_wait = activity["model_wait"]
    # Idle warning state (Issue #18): at most one `pi_idle` warning per
    # stall; the first new session event after it logs `pi_resumed`.
    idle_warned = False
    # Idle-stall recovery state (Issue #94): `idle_start_epoch` marks
    # the start of the current idle window (only descendants that
    # started no later than it are targets — a process spawned after
    # the window began is a new tool call, never a target); `recovery`
    # is the live state shown in the GitHub progress comment (None /
    # `term` / `kill`); `recovery_targets` are the TERMed descendants
    # tracked for the KILL escalation; `recovery_step` is the highest
    # escalation step already executed (0 none, 1 TERM, 2 KILL).
    idle_start_epoch: float | None = None
    # The monotonic moment the idle window opened: escalation is
    # measured in idle windows of NEW silence since the runner first
    # saw the stall (never in absolute stale seconds — a session that
    # is already stale when the window opens, e.g. old record
    # timestamps, starts at step one, not at the session kill).
    idle_start_monotonic: float | None = None
    recovery: str | None = None
    recovery_targets: list[dict] = []
    recovery_step = 0
    # The `pi_idle_wait` decision is logged once per stall (Issue #169):
    # the escalation re-evaluates every window while the tool is inside
    # its `timeout` deadline, but the journal carries one decision line.
    idle_wait_logged = False
    idle_recovery_failed = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                process.kill()
                timed_out = True
                break
            ready, _, _ = select.select(
                [process.stdout, process.stderr], [], [], poll_interval,
            )
            for stream in ready:
                data = os.read(stream.fileno(), 65536)
                if data:
                    if stream is process.stdout:
                        stdout_chunks.append(data)
                    else:
                        stderr_chunks.append(data)
            activity = watcher.poll()
            visible = (
                activity["phase"], activity["action"], activity["result"],
            )
            # The wait state rides on the activity/heartbeat lines
            # (Issue #40). Once the model_wait silence crosses the dead
            # threshold the wait is SLOW, not dead (Issue #169): the
            # state is `model_wait_slow` — visible, but only the missing
            # upstream connection (checked below) is a kill.
            if activity["model_wait"]:
                wait_state = (
                    "model_wait_slow"
                    if activity["stale_seconds"] >= model_wait_dead_seconds
                    else "model_wait"
                )
            else:
                wait_state = None
            if visible != last_visible:
                # Only changed fields are repeated; an unchanged poll is a
                # heartbeat (Issue #40).
                _log_activity(
                    activity, issue_ref=issue_ref,
                    role=role,
                    state=wait_state,
                )
                last_visible = visible
            else:
                _log_heartbeat(
                    activity, issue_ref=issue_ref,
                    role=role, elapsed=time.monotonic() - start,
                    state=wait_state,
                )
            if activity["model_wait"] != last_model_wait:
                # One transition line per state change: entering model_wait
                # (the model is expected to reply next) or leaving it
                # (the next session event arrived: resumed). No `run=`
                # field: the `[run_id]` prefix carries the run id
                # (Issue #57).
                LOGGER.info(
                    "%s issue=%s role=%s phase=%s state=%s",
                    "model_wait" if activity["model_wait"] else "resumed",
                    issue_ref, role,
                    activity["phase"],
                    "model_wait" if activity["model_wait"] else "resumed",
                )
                last_model_wait = activity["model_wait"]
            # Idle warning (Issue #18): a stalled session (no model/
            # session event for `idle_warn_seconds`, and the model is
            # not expected to reply next) logs ONE `pi_idle` warning
            # with the stale time; the first new session event after it
            # logs `pi_resumed`. A slow active model (model_wait) never
            # warns (Issue #40).
            if idle_warned and activity["changed"]:
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.info(
                    "pi_resumed issue=%s role=%s phase=%s",
                    issue_ref, role, activity["phase"],
                )
                idle_warned = False
                # The stall is over: the whole recovery state resets
                # (Issue #94) — a later stall starts a fresh window.
                idle_start_epoch = None
                idle_start_monotonic = None
                recovery = None
                recovery_targets = []
                recovery_step = 0
                idle_wait_logged = False
            elif (
                not activity["model_wait"]
                and not idle_warned
                and activity["stale_seconds"] >= idle_warn_seconds
            ):
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.warning(
                    "pi_idle issue=%s role=%s phase=%s "
                    "stale_seconds=%s",
                    issue_ref, role, activity["phase"],
                    format_duration(activity["stale_seconds"]),
                )
                idle_warned = True
                # The idle window starts now (Issue #94): only
                # descendants that already existed before this moment
                # are recovery targets.
                idle_start_epoch = time.time()
                idle_start_monotonic = time.monotonic()
            # Idle-stall recovery (Issue #94): a stalled session (no
            # model/session activity for idle windows, and the model is
            # NOT expected to reply next) is recovered instead of only
            # warning. Escalation, one step per idle window:
            #   window 1: SIGTERM the pre-idle descendants (the hung
            #             tools) — the tool gets a non-zero exit, the
            #             failure signal reaches the model, the session
            #             continues on its own;
            #   window 2: SIGKILL a TERMed target that is still alive;
            #   window N (PI_IDLE_RECOVERY_CYCLES, default 3): kill the
            #             Pi session itself and fail fast through the
            #             normal `ai-blocked` path (the slot is never
            #             held forever). Only pi descendants (ppid
            #             chain) that started no later than the idle
            #             start are ever signaled — never other system
            #             processes, never a process spawned after the
            #             window began. The progress comment is synced
            #             via the `recovery` activity field.
            if (
                idle_warned
                and not activity["model_wait"]
                and activity["stale_seconds"] >= idle_warn_seconds
                and idle_start_epoch is not None
            ):
                # Escalation is measured in idle windows of NEW silence
                # since the window opened (never in absolute stale
                # seconds): a session that is already stale when the
                # window opens (e.g. old record timestamps) starts at
                # step one, not at the session kill.
                silence = time.monotonic() - idle_start_monotonic
                cycle = int(silence // idle_warn_seconds) + 1
                if cycle >= 1 and recovery_step == 0:
                    targets = find_idle_descendants(
                        process.pid, idle_start_epoch,
                    )
                    # Evidence-based wait (Issue #169, the #105
                    # regression): a pre-idle descendant that runs a
                    # coreutils `timeout <seconds> ...` wrapper INSIDE
                    # its deadline is a legitimately running tool, not a
                    # hung one — the runner waits for the deadline
                    # instead of TERMed it. The wait decision is logged
                    # once and the escalation pauses (recovery_step stays
                    # 0): every later window re-evaluates, and when the
                    # deadline passes with the descendant still alive the
                    # evidence flips and the TERM → KILL → session-kill
                    # escalation runs unchanged (the slot is never held
                    # forever).
                    pending = _pending_timeout_targets(targets)
                    if pending:
                        if not idle_wait_logged:
                            for target, deadline in pending:
                                LOGGER.warning(
                                    "pi_idle_wait run=%s issue=%s role=%s "
                                    "pid=%s cmdline=%s deadline=%s",
                                    run_id, issue_ref, role, target["pid"],
                                    quote_value(target["cmdline"] or "-"),
                                    time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(deadline),
                                    ),
                                )
                            idle_wait_logged = True
                        recovery = "wait"
                    elif targets:
                        recovery_targets = targets
                        for target in targets:
                            result = signal_pid(
                                target["pid"], signal.SIGTERM,
                            )
                            LOGGER.warning(
                                "pi_idle_term run=%s issue=%s role=%s "
                                "pid=%s cmdline=%s result=%s",
                                run_id, issue_ref, role, target["pid"],
                                quote_value(target["cmdline"] or "-"),
                                result,
                            )
                        recovery = "term"
                    else:
                        # No hung tool found (Pi itself is stuck): the
                        # escalation continues, nothing is signaled.
                        LOGGER.warning(
                            "pi_idle_term run=%s issue=%s role=%s "
                            "result=no_target",
                            run_id, issue_ref, role,
                        )
                        # The pre-idle descendants are gone (a waited
                        # tool reached its own deadline): the wait state
                        # is stale — clear it so the progress comment
                        # does not keep showing `recovery: wait` while
                        # the escalation runs (Issue #169).
                        recovery = None
                    if not pending:
                        recovery_step = 1
                elif cycle >= 2 and recovery_step == 1:
                    for target in recovery_targets:
                        if not pid_alive(target["pid"]):
                            # The TERM worked between polls: record it,
                            # signal nothing.
                            LOGGER.warning(
                                "pi_idle_kill run=%s issue=%s role=%s "
                                "pid=%s cmdline=%s result=already_dead",
                                run_id, issue_ref, role, target["pid"],
                                quote_value(target["cmdline"] or "-"),
                            )
                            continue
                        result = signal_pid(target["pid"], signal.SIGKILL)
                        LOGGER.warning(
                            "pi_idle_kill run=%s issue=%s role=%s "
                            "pid=%s cmdline=%s result=%s",
                            run_id, issue_ref, role, target["pid"],
                            quote_value(target["cmdline"] or "-"),
                            result,
                        )
                    recovery = "kill"
                    recovery_step = 2
                if cycle >= PI_IDLE_RECOVERY_CYCLES:
                    process.kill()
                    idle_recovery_failed = True
                    break
            # The live progress comment shows the recovery state while
            # it is active (Issue #94); the watcher state is a fresh
            # dict per poll, so the field never leaks into other polls.
            activity["recovery"] = recovery
            if progress is not None:
                try:
                    progress(activity)
                except Exception:
                    LOGGER.exception(
                        "progress_publish_failed run=%s issue=%s role=%s",
                        run_id, issue_ref, role,
                    )
            # Upstream-dead detection (Issue #75, evidence-based since
            # Issue #169): the model is expected to reply next
            # (model_wait) and the session file has been frozen for the
            # dead threshold — but the bare freeze is NOT evidence the
            # upstream is dead: a slow local model generating for
            # minutes keeps its connection open (a live TCP socket in
            # the live states ESTABLISHED/SYN_SENT/SYN_RECV). Only the
            # combination of the long silence AND no live upstream
            # connection kills Pi and fails fast (the #158 regression:
            # a healthy long generation must not be killed): the
            # normal failure path releases the slot so the next tick
            # can resume or claim the next Issue. Never fires while
            # events keep arriving (a slow generation is not a dead
            # upstream).
            if (
                activity["model_wait"]
                and activity["stale_seconds"] >= model_wait_dead_seconds
            ):
                if upstream_alive(process.pid):
                    # The connection to the upstream is still alive:
                    # the model is generating slowly, not dead
                    # (Issue #169). The wait is visible as
                    # `state=model_wait_slow` on the heartbeats — no
                    # kill, the run continues.
                    pass
                else:
                    process.kill()
                    upstream_dead = True
                    break
            if process.poll() is not None:
                break
    finally:
        _drain_stream(process.stdout, stdout_chunks)
        _drain_stream(process.stderr, stderr_chunks)
    stdout = _decode_chunks(stdout_chunks)
    stderr = _decode_chunks(stderr_chunks)
    if idle_recovery_failed:
        stale = format_duration(activity["stale_seconds"])
        LOGGER.error(
            "run_failed %s reason=idle_recovery_stale_%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            stale,
        )
        raise RuntimeError(
            f"Pi session stayed idle for {stale} after idle recovery "
            f"(TERM/KILL of pre-idle descendants); Pi was killed "
            "(Issue #94)"
        )
    if upstream_dead:
        stale = format_duration(activity["stale_seconds"])
        LOGGER.error(
            "run_failed %s reason=upstream_dead_stale_%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            stale,
        )
        raise RuntimeError(
            f"Pi is stuck in model_wait with a frozen session for {stale}: "
            "the upstream (llama/proxy) is dead; Pi was killed (Issue #75)"
        )
    if timed_out:
        reason = f"timeout_{format_duration(timeout)}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.TimeoutExpired(
            safe_command, timeout, output=stdout, stderr=stderr,
        )
    if process.returncode != 0:
        reason = f"pi_exit_{process.returncode}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.CalledProcessError(
            process.returncode, safe_command, output=stdout, stderr=stderr,
        )
    if stderr:
        LOGGER.info("stderr=%s", stderr.rstrip())
    LOGGER.info("stdout=%s", stdout.rstrip())
    return stdout.strip()


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           *, timeout: int | None = None, branch: str | None = None,
           progress: Callable[[dict], None] | None = None) -> str:
    """Run the implementer Pi session for a freshly claimed Issue.

    Issue #82 removed the fixer reuse of this function: findings are
    fixed by the review session in the same session, so the implementer
    is the only user of `prompt.md` now.
    """
    system_prompt = render_prompt(
        config["prompt"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "SOURCE_REPOS": ", ".join(config["source_repos"]),
            "ISSUE_NUMBER": str(issue["number"]),
            "ISSUE_TITLE": issue["title"],
            "ISSUE_BODY": issue.get("body", ""),
            "WORKSPACE_ROOT": str(config["workspace_root"]),
            "CONTEXT_FILES": "\n".join(str(path) for path in config["context_files"]),
            "SKILLS": "\n".join(
                str(path)
                for path in _skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)
            ),
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": config["base_sha"],
            "RUN_ID": config["run_id"],
        },
    )
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
    )
    context += "Complete the delivery process in the system prompt."
    command = [
        "pi", *_skill_args(_skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)),
        *_pi_model_args(config),
        "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt, context,
    ]
    # Issue #157: the provider file (baseUrl / api / apiKey / models)
    # reaches Pi through the materialized per-run agent dir, never
    # through the command line or the log (the redacted command keeps
    # only the #119 provider/model/thinking identifiers). Unconfigured
    # -> the stream_pi call keeps its exact pre-#157 shape.
    agent_dir = prepare_pi_agent_dir(worktree, config)
    extra = {}
    if agent_dir is not None:
        extra["pi_env"] = {"PI_CODING_AGENT_DIR": str(agent_dir)}
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_model_args(config), "--print", "--session-dir",
            str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=int(issue["number"]),
        source_repo=source_repo,
        branch=branch,
        progress=progress,
        **extra,
    )


def verify_pr(worktree: Path, branch: str, base_branch: str,
              run_id: str, *, issue: int,
              pr_repo: str | None = None,
              expected_url: str | None = None,
              require_latest_base: bool = True) -> str:
    """Verify that exactly one open PR of the task branch is the delivery.

    Checks, in order: current branch, latest remote base ancestry (unless
    `require_latest_base` is False — the resume pre-validation runs
    before the base merge, when being behind is the expected state),
    exactly one open PR for the head branch, PR base, PR head == local
    HEAD, the run marker in the PR body, and the `Fixes #<issue>`
    keyword in the PR body (Issue #53: GitHub closes the source Issue
    natively only when the body carries the keyword, so a PR without it
    would leave the Issue open after the merge). When `pr_repo` is
    given (resume path), the PR's head repo must be that repo; when
    `expected_url` is given, the verified PR URL must exactly equal the
    recovered original PR URL (Issue #45 review: the resume must keep
    the same PR number).
    """
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    if require_latest_base:
        # Re-fetch before judging: the delivery must contain the latest
        # remote base, otherwise it is behind and the PR is rejected
        # (fail fast).
        run_command(
            ["git", "fetch", "origin", base_branch], cwd=worktree,
        )
        try:
            run_command(
                ["git", "merge-base", "--is-ancestor",
                 f"origin/{base_branch}", "HEAD"],
                cwd=worktree,
            )
        except subprocess.CalledProcessError:
            LOGGER.error(
                "delivery_behind_base base_branch=%s branch=%s",
                base_branch, branch,
            )
            raise RuntimeError(
                f"delivery HEAD is behind latest remote base "
                f"origin/{base_branch}; merge the latest base, rerun full "
                "tests and review, then retry"
            ) from None
    local_head = run_command(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
    )
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json",
        "url,baseRefName,headRefName,headRefOid,"
        "headRepository,headRepositoryOwner,body",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    url = prs[0].get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    if pr_repo is not None:
        head_repo = _pr_head_repo(prs[0])
        if head_repo != pr_repo:
            LOGGER.error(
                "pr_repo_mismatch expected=%s actual=%s branch=%s",
                pr_repo, head_repo, branch,
            )
            raise RuntimeError(
                f"PR head repo is {head_repo}, expected {pr_repo}; the "
                "resume must keep the PR of the configured source repo"
            )
    base_ref = prs[0].get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; recreate the "
            "PR against the configured base branch"
        )
    head_oid = prs[0].get("headRefOid")
    if head_oid != local_head:
        LOGGER.error(
            "pr_head_mismatch pr_head=%s local_head=%s branch=%s",
            head_oid, local_head, branch,
        )
        raise RuntimeError(
            f"PR head {head_oid} is not local HEAD {local_head}; the "
            "verified commit was not pushed, push the reviewed commit and retry"
        )
    marker = run_marker(run_id)
    body = prs[0].get("body")
    if not isinstance(body, str) or marker not in body:
        LOGGER.error(
            "pr_run_marker_missing expected=%s branch=%s", marker, branch,
        )
        raise RuntimeError(
            f"PR body is missing the stable run marker {marker}; the PR "
            "must carry the machine-readable run id of this attempt"
        )
    fixes = f"Fixes #{issue}"
    # The number must match exactly, not as a digit prefix: `Fixes #41`
    # closes Issue 41, not Issue 4 (review F1, Issue #53).
    if not re.search(rf"Fixes #{issue}(?!\d)", body):
        LOGGER.error(
            "pr_fixes_missing issue=%s branch=%s", issue, branch,
        )
        raise RuntimeError(
            f"PR body is missing `{fixes}`; the keyword must point at the "
            "source Issue so GitHub closes it natively when the PR merges "
            "into the default branch"
        )
    if expected_url is not None and url != expected_url:
        LOGGER.error(
            "pr_url_mismatch expected=%s actual=%s branch=%s",
            expected_url, url, branch,
        )
        raise RuntimeError(
            f"PR URL {url} is not the recovered original PR "
            f"{expected_url}; the resume must keep the same PR number"
        )
    return url


def verify_resumed_pr(scene: dict, issue: dict, config: dict,
                      source_repo: str) -> str:
    """Verify the PR of a resumed delivery BEFORE any git/Pi mutation.

    Issue #89: #82 removed the cold-start fixer together with the
    pre-Pi `verify_pr` of the old `resume_delivery` — the resume passed
    the comment's PR URL straight to the delivery wait while the review
    froze the PR derived from the run id, so the two lines could be
    different PRs (a comment must never steer the runner into the wrong
    PR, Issue #45). Restored: branch and worktree are DERIVED from the
    configured repo_dir, source repo, Issue number and run id (never
    read from the comment), the scene base must still equal the
    configured base (Issue #91) and the worktree must exist (Issue #90)
    — both checked BEFORE any command runs — and the existing
    `verify_pr` then validates exactly one open PR of the derived
    branch in the configured source repo, on the configured base,
    carrying the run marker and the `Fixes` keyword, with the EXACT URL
    of the recovered scene. `require_latest_base=False`: being behind
    the latest base is the expected state the review session absorbs
    in-session (Issue #82), so the base merge never returns to the
    runner. The returned URL is the one verify_pr verified — the
    delivery wait only ever sees verified URLs, never the comment
    string.

    Any failure is terminal: the Issue is marked `ai-blocked` ALONE
    (the opened-PR state label is removed, and a leftover
    `ai-fix-needed` too) with a run-marked failure comment, the blocked
    milestone and the blocked progress scene, and the error is
    re-raised so the tick stops — no review Pi is started, nothing is
    merged, and the PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    run_id = scene["run_id"]
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config["repo_dir"], source_repo, number, run_id,
    )
    try:
        if scene["base_branch"] != config["base_branch"]:
            raise ValueError(
                f"resume scene base_branch={scene['base_branch']} "
                f"differs from configured base_branch="
                f"{config['base_branch']}; the PR is frozen on a "
                "different base and must not be resumed against the "
                "configured one"
            )
        if not worktree.is_dir():
            raise RuntimeError(f"worktree missing: {worktree}")
        return verify_pr(
            worktree, branch, config["base_branch"], run_id,
            issue=number, pr_repo=source_repo,
            expected_url=scene["pr_url"], require_latest_base=False,
        )
    except Exception as exc:
        LOGGER.exception(
            "issue=%s resume_pr_verification_failed pr=%s branch=%s",
            number, scene["pr_url"], branch,
        )
        try:
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=PR_OPENED_LABEL,
            )
            # A resume that fails while the Issue awaits the next
            # review session (`ai-fix-needed`) leaves that label
            # behind: remove it too, so the terminal state is
            # `ai-blocked` alone (Issue #82 routes both opened-PR
            # states into the same resume).
            if FIX_NEEDED_LABEL in issue_labels(number, source_repo):
                edit_issue(
                    number, repo=source_repo, remove=FIX_NEEDED_LABEL,
                )
            body = (
                f"Muyan Pilot failed: the resume verification of "
                f"PR {scene['pr_url']} failed: {exc}; the PR, branch "
                f"{branch} and worktree {worktree} are preserved"
            )
            if current_run_id():
                body = f"{run_marker(current_run_id())}\n{body}"
            comment_issue(number, repo=source_repo, body=body)
            bound_run_id = current_run_id()
            if bound_run_id:
                # Issue #79: the blocked-scene progress publishing is
                # bypass — a 404 here must not abort the failure
                # reporting (the `ai-blocked` transition and the
                # failure comment above already completed, and the
                # original error is re-raised below either way).
                _safe_publish(
                    run_id=bound_run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
                    action=lambda: ProgressPublisher(
                        number, source_repo, bound_run_id,
                        run_command=run_command,
                    ).milestone(
                        f"blocked: the resume verification of "
                        f"PR {scene['pr_url']} failed: {exc}"
                    ),
                )
                _safe_publish(
                    run_id=bound_run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
                    action=lambda: _finish_blocked_progress(
                        number, bound_run_id, source_repo, worktree,
                        branch, scene["pr_url"],
                        f"the resume verification of PR "
                        f"{scene['pr_url']} failed: {exc}",
                        "fix the resume verification failure above "
                        "and resume the delivery of this same PR",
                        title=issue["title"],
                        role=ROLE_REVIEW,
                        review_round=review_rounds_so_far(
                            issue_comments(number, repo=source_repo),
                        ),
                        priority=issue_priority(issue),
                    ),
                )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def parse_review_verdict(text: str) -> dict:
    """Extract the last REVIEW_VERDICT JSON line from a review session.

    The reviewer must end with a machine-readable verdict so the Runner can
    decide without parsing prose. Missing or malformed verdicts fail fast; a
    review that cannot be read as a pass is never treated as a pass.
    """
    payload = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(VERDICT_MARKER):
            payload = stripped[len(VERDICT_MARKER):].strip()
    if payload is None:
        raise ValueError("no REVIEW_VERDICT line in review output")
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError:
        raise ValueError("malformed REVIEW_VERDICT JSON") from None
    if not isinstance(verdict, dict):
        raise ValueError("malformed REVIEW_VERDICT JSON")
    if verdict.get("verdict") not in ("pass", "findings"):
        raise ValueError("verdict must be 'pass' or 'findings'")
    for key in ("blockers", "majors", "minors"):
        value = verdict.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not isinstance(verdict.get("findings", []), list):
        raise ValueError("findings must be a list")
    blockers = verdict["blockers"]
    majors = verdict["majors"]
    if verdict["verdict"] == "pass" and (blockers > 0 or majors > 0):
        raise ValueError("pass verdict cannot have blockers or majors")
    if verdict["verdict"] == "findings" and blockers == 0 and majors == 0:
        raise ValueError("findings verdict requires blockers or majors")
    return verdict


def review_has_findings(verdict: dict) -> bool:
    """True when a verdict still blocks the merge gate (Blocker or Major)."""
    return verdict["blockers"] > 0 or verdict["majors"] > 0


def freeze_pr(worktree: Path, branch: str, base_branch: str) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "number,url,baseRefName,baseRefOid,headRefName,headRefOid",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    pr = prs[0]
    base_ref = pr.get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; the merge gate "
            "only accepts the configured protected branch"
        )
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": base_ref,
        "base_oid": pr["baseRefOid"],
        "head_ref": pr["headRefName"],
        "head_oid": pr["headRefOid"],
    }


# Role-specific skill filtering (Issue #83): the review session ends
# with a single REVIEW_VERDICT line and its job is to review this one
# diff and fix it until it can merge (Issue #82) — not to open another
# full delivery — so the delivery-oriented skills must not be loaded
# there (tdd-dev would steer it into the implement/test/PR flow,
# review-fix-loop would open another fix/review round). The
# implementer keeps tdd-dev and code-review but not review-fix-loop:
# the Runner itself runs the independent review loop once the PR is
# open.
REVIEW_EXCLUDED_SKILLS = frozenset({"tdd-dev", "review-fix-loop"})
IMPLEMENT_EXCLUDED_SKILLS = frozenset({"review-fix-loop"})


def _skill_name(entry: str | Path) -> str:
    """Return the skill name of one configured skill entry.

    Entries point at the SKILL.md file inside the skill directory
    (e.g. .../skills/tdd-dev/SKILL.md); the skill name is the parent
    directory. A bare markdown entry (e.g. my-skill.md) or a skill
    directory is named after its own stem.
    """
    path = Path(entry)
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def _skills_for(config: dict, excluded: frozenset[str]) -> list[str | Path]:
    """Return one role's configured skills, dropping excluded names."""
    return [
        skill for skill in config["skills"]
        if _skill_name(skill) not in excluded
    ]


def _skill_args(skills: list[str | Path]) -> list[str]:
    """Return the --skill command args for one role's skill list."""
    return [
        item for skill in skills
        for item in ("--skill", str(skill))
    ]


def run_review(worktree: Path, pr: dict, config: dict, source_repo: str,
               issue: int, branch: str, round: int,
               timeout: int | None = None,
               progress: Callable[[dict], None] | None = None) -> str:
    """Run one independent review session for a frozen PR.

    The session is independent (new process, `prompt_review.md`, a new
    session JSONL) and reviews the exact frozen base/head. Issue #82:
    when it finds Blocker/Major issues it fixes them IN THIS SAME
    SESSION (modify code, run the full test suite with coverage, commit
    and push the task branch) and re-emits the final verdict — there is
    no cold-start fixer and no third review. The review streams live
    activity through the same pipeline as the implementer (role=review;
    Issue #41: one run_id end to end, the roles are steps of the same
    run).
    """
    system_prompt = render_prompt(
        config["prompt_review"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "PR_NUMBER": str(pr["number"]),
            "PR_URL": pr["url"],
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": pr["base_oid"],
            "HEAD_SHA": pr["head_oid"],
            "HEAD_REF": pr["head_ref"],
            "ROUND": str(round),
        },
    )
    context = (
        f"Independently review PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} against base {config['base_branch']}@{pr['base_oid']} "
        f"and head {pr['head_oid']} (round {round}). Follow code-review R1-R9; "
        "fix Blocker/Major findings in this same session (push only the "
        "task branch) and end with a single REVIEW_VERDICT line."
    )
    command = [
        "pi", *_skill_args(_skills_for(config, REVIEW_EXCLUDED_SKILLS)),
        *_pi_model_args(config),
        "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt,
        context,
    ]
    # Issue #157: the review session uses the SAME provider config as
    # the implementer (one materialized dir per worktree, re-used).
    agent_dir = prepare_pi_agent_dir(worktree, config)
    extra = {}
    if agent_dir is not None:
        extra["pi_env"] = {"PI_CODING_AGENT_DIR": str(agent_dir)}
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_model_args(config), "--print", "--session-dir",
            str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<review-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=issue,
        source_repo=source_repo,
        branch=branch,
        role=ROLE_REVIEW,
        progress=progress,
        **extra,
    )


def merge_gate(worktree: Path, pr: dict, base_branch: str) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, and the remote head to still be the reviewed head. Then
    merge with `--match-head-commit` so only that exact head can land. No force
    push, no direct push of the protected branch.
    """
    run_command(["git", "fetch", "origin", base_branch], cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor",
             f"origin/{base_branch}", pr["head_oid"]],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "merge_gate_behind_base base_branch=%s pr=%s head=%s",
            base_branch, pr["number"], pr["head_oid"],
        )
        raise RuntimeError(
            f"PR #{pr['number']} head {pr['head_oid']} is behind latest "
            f"remote base origin/{base_branch}; absorb the latest base, rerun "
            "tests and review, then retry"
        ) from None
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergeable,headRefOid",
    ], cwd=worktree)
    state = json.loads(raw)
    mergeable = state.get("mergeable")
    if mergeable != "MERGEABLE":
        LOGGER.error(
            "merge_gate_not_mergeable pr=%s mergeable=%s",
            pr["number"], mergeable,
        )
        raise RuntimeError(
            f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
            "resolve conflicts and retry"
        )
    remote_head = state.get("headRefOid")
    if remote_head != pr["head_oid"]:
        LOGGER.error(
            "merge_gate_head_moved pr=%s reviewed=%s remote=%s",
            pr["number"], pr["head_oid"], remote_head,
        )
        raise RuntimeError(
            f"PR #{pr['number']} head moved since review "
            f"(reviewed={pr['head_oid']} remote={remote_head}); re-review "
            "before merging"
        )
    run_command([
        "gh", "pr", "merge", str(pr["number"]),
        "--match-head-commit", pr["head_oid"], "--merge",
    ], cwd=worktree)
    LOGGER.info("merged pr=%s head=%s", pr["number"], pr["head_oid"])
    return {**pr, "merged": True}


def confirm_merged(worktree: Path, pr: dict, base_branch: str) -> dict:
    """Confirm the PR is MERGED and origin/<base> contains the merge commit."""
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergedAt,mergeCommit",
    ], cwd=worktree)
    state = json.loads(raw)
    if state.get("state") != "MERGED" or not state.get("mergedAt"):
        LOGGER.error("confirm_merged_not_merged pr=%s state=%s",
                     pr["number"], state.get("state"))
        raise RuntimeError(
            f"PR #{pr['number']} is not merged (state={state.get('state')})"
        )
    merge_commit = (state.get("mergeCommit") or {}).get("oid")
    if not merge_commit:
        raise RuntimeError(
            f"PR #{pr['number']} is merged but has no merge commit oid"
        )
    run_command(["git", "fetch", "origin", base_branch], cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", merge_commit,
             f"origin/{base_branch}"],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "confirm_merged_missing_on_base pr=%s merge_commit=%s",
            pr["number"], merge_commit,
        )
        raise RuntimeError(
            f"merge commit {merge_commit} is not on origin/{base_branch}; "
            "the merge did not land on the protected branch"
        ) from None
    return {"state": "MERGED", "merge_commit": merge_commit}


def review_rounds_so_far(comments: list[dict]) -> int:
    """Count the review rounds already recorded on the Issue.

    Each round with Blocker/Major findings posts one
    `Muyan Pilot review round N for PR #...` comment, so the GitHub
    record alone bounds the loop (GitHub Issues are the only state
    store; a runner restart never loses the count). Only trusted
    maintainer comments count: a public comment cannot exhaust the
    round budget or skip review (same filter as resume_scene).
    """
    rounds = 0
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            if line.startswith("Muyan Pilot review round "):
                rounds += 1
                break
    return rounds


BASE_SYNC_LOCK_NAME = "base-sync.lock"


def base_sync_lock_path(repo_dir: Path) -> Path:
    """Issue #149: the lock file serializing ALL writers of the
    deployment base checkout.

    Two timer instances may start in the same tick, so the service
    template's `ExecStartPre` wraps the fetch + fast-forward in a
    short-lived `flock` on this SAME file, and the Python-side sync
    below takes the same lock: the main worktree is never written
    concurrently. The lock lives in the shared state dir (next to the
    slot files), never in a per-process temp dir.
    """
    return Path(repo_dir) / ".muyan-pilot" / BASE_SYNC_LOCK_NAME


def _acquire_base_sync_lock(
    repo_dir: Path, lock_timeout_seconds: float,
) -> int:
    """Take the base-sync flock; fail fast when it is still held after
    the timeout. The kernel releases an flock when its holder exits,
    so a dead holder can never wedge the lock."""
    lock_path = base_sync_lock_path(repo_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + lock_timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, InterruptedError, PermissionError):
            if time.monotonic() >= deadline:
                os.close(fd)
                LOGGER.error(
                    "base_sync_lock_timeout repo_dir=%s lock=%s "
                    "timeout_seconds=%s",
                    repo_dir, lock_path, lock_timeout_seconds,
                )
                raise RuntimeError(
                    f"could not take the base-sync lock {lock_path} "
                    f"within {lock_timeout_seconds}s (another Runner "
                    "instance or the ExecStartPre preflight is syncing "
                    "the deployment checkout)"
                ) from None
            time.sleep(0.1)


def sync_base_checkout(repo_dir: Path, base_branch: str,
                       *, lock_timeout_seconds: float = 300.0) -> None:
    """Fast-forward the configured repo_dir base checkout to origin/<base>.

    systemd executes the runner from this checkout: after a merge lands
    on origin/<base>, the next tick must load the newly merged code, so
    the deployment checkout is synced here and verified to equal the
    remote base. A checkout that cannot fast-forward (local drift) fails
    fast; the merge itself already landed on GitHub.

    Issue #149: the whole sync runs under the short-lived base-sync
    flock (the SAME lock the service template's `ExecStartPre` uses),
    so two instances starting in the same tick never write the main
    worktree concurrently; the lock is released when the sync finishes
    (success or failure).
    """
    fd = _acquire_base_sync_lock(repo_dir, lock_timeout_seconds)
    try:
        _sync_base_checkout_locked(repo_dir, base_branch)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sync_base_checkout_locked(repo_dir: Path, base_branch: str) -> None:
    """The actual fetch + fast-forward + verify, under the base-sync
    flock (see ``sync_base_checkout``)."""
    run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    remote_head = run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )
    if local_head == remote_head:
        return
    try:
        run_command(
            ["git", "merge", "--ff-only", f"origin/{base_branch}"],
            cwd=repo_dir,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "base_checkout_not_fast_forwardable repo_dir=%s base=%s "
            "local=%s remote=%s",
            repo_dir, base_branch, local_head, remote_head,
        )
        raise RuntimeError(
            f"deployment checkout {repo_dir} cannot fast-forward to "
            f"origin/{base_branch} (local={local_head} "
            f"remote={remote_head}); the merged code cannot be loaded "
            "by the next tick"
        ) from None
    synced = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if synced != remote_head:
        raise RuntimeError(
            f"deployment checkout {repo_dir} is at {synced} after the "
            f"sync, expected origin/{base_branch} at {remote_head}"
        )
    LOGGER.info(
        "base_checkout_synced repo_dir=%s base=%s head=%s",
        repo_dir, base_branch, synced,
    )


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: dict, source_repo: str,
                              number: int, title: str, priority: str) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    `title` is the issue's GitHub title (Issue #100): the review
    progress scenes (ensure, findings, merged) show `#<number> <title>`
    like every other scene; it is required, never fabricated.

    The delivery wait loop (which holds the slot) calls this while the
    PR is open and the Issue awaits review (`ai-pr-opened`) or awaits the
    next review session (`ai-fix-needed`). It freezes the PR, runs the
    independent review (streamed, role=review), and then:

    - clean verdict -> the reviewer may have fixed findings IN THE SAME
      SESSION and pushed the task branch (Issue #82), so the PR is
      RE-FROZEN before the merge gate: the gate (latest-base ancestor,
      mergeable, head match, `gh pr merge --match-head-commit`) then
      runs against the head the verdict actually covers; confirm the
      merge landed on origin/<base>, sync the deployment checkout,
      label the Issue `ai-merged`; returns True;
    - Blocker/Major findings the reviewer could not fix in-session ->
      comment them to Issue and PR and label the Issue `ai-fix-needed`;
      the next wait iteration (or the next tick after a restart) runs
      the same independent review again — no cold-start fixer, no
      third review; returns False;
    - a gate failure because the head is behind the latest base or has
      a merge conflict -> label the Issue `ai-fix-needed` with the
      absorb-base finding (the next review session absorbs the latest
      base in-session); returns False;
    - missing/malformed verdict or an exhausted round budget -> raise;
      the caller marks the Issue `ai-blocked`.
    """
    marker = run_marker(config["run_id"])
    comments = issue_comments(number, repo=source_repo)
    rounds = review_rounds_so_far(comments)
    if rounds >= MAX_REVIEW_ROUNDS:
        LOGGER.error(
            "review_rounds_exhausted issue=%s rounds=%s",
            number, rounds,
        )
        raise RuntimeError(
            f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
            "without a clean verdict; needs human review"
        )
    round = rounds + 1
    pr = freeze_pr(worktree, branch, base_branch)
    publisher = ProgressPublisher(
        number, source_repo, config["run_id"], run_command=run_command,
    )
    started = time.monotonic()
    # Issue #79: ensure is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.ensure(_progress_body(_progress_state(
            issue=number, title=title, run_id=config["run_id"],
            role=ROLE_REVIEW, branch=branch, worktree=worktree,
            started=started, pr_url=pr["url"], review_round=round,
            priority=priority,
        ))),
    )
    output = run_review(
        worktree, pr, config, source_repo, number, branch, round,
        progress=LiveProgressThrottle(
            publisher, issue=number, title=title,
            run_id=config["run_id"], role=ROLE_REVIEW, branch=branch,
            worktree=worktree, started=started, pr_url=pr["url"],
            review_round=round, priority=priority,
        ),
    )
    verdict = parse_review_verdict(output)
    LOGGER.info(
        "review pr=%s round=%s verdict=%s blockers=%s majors=%s",
        pr["number"], round, verdict["verdict"], verdict["blockers"],
        verdict["majors"],
    )
    if review_has_findings(verdict):
        # The reviewer could not make the PR mergeable in this session
        # (Issue #82: findings are fixed in the same session; reaching
        # this branch means the fix was not verifiable or not this
        # session's to decide). The Issue moves to the explicit
        # fix-needed state: the next review session retries the same PR
        # (no cold-start fixer), and the round budget bounds the loop.
        LOGGER.info(
            "review_findings_unfixed pr=%s round=%s; the Issue moves to "
            "ai-fix-needed, the next review session retries the same PR",
            pr["number"], round,
        )
        body = (
            f"{marker}\n"
            f"Muyan Pilot review round {round} for PR #{pr['number']}: "
            f"{verdict['blockers']} blocker(s), {verdict['majors']} "
            "major(s). Findings: "
            + json.dumps(verdict["findings"], ensure_ascii=False)
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], body=body)
        # Issue #79: the findings publishing is bypass — a 404 here
        # must not stop the `ai-fix-needed` transition below (the next
        # review session retries the same PR either way).
        _safe_publish(
            run_id=config["run_id"], issue=number,
            source_repo=source_repo, role=ROLE_REVIEW,
            action=lambda: publisher.milestone(
                f"review findings: round {round}, "
                f"{verdict['blockers']} blocker(s), "
                f"{verdict['majors']} major(s) for PR #{pr['number']}"
            ),
        )
        _safe_publish(
            run_id=config["run_id"], issue=number,
            source_repo=source_repo, role=ROLE_REVIEW,
            action=lambda: publisher.finish(_progress_body(
                _progress_state(
                    issue=number, title=title, run_id=config["run_id"],
                    role=ROLE_REVIEW, branch=branch,
                    worktree=worktree, started=started,
                    pr_url=pr["url"], review_round=round,
                    priority=priority,
                ), outcome=(
                    "**Muyan Pilot review findings**\n\n"
                    f"round {round}: {verdict['blockers']} blocker(s), "
                    f"{verdict['majors']} major(s); the next review "
                    "session retries the same PR automatically"
                ),
            )),
        )
        edit_issue(
            number, repo=source_repo, add=FIX_NEEDED_LABEL,
            remove=PR_OPENED_LABEL,
        )
        return False
    # Issue #82: the reviewer fixes findings in the same session and
    # pushes the task branch, so the head the verdict covers may be
    # NEWER than the frozen head. Re-freeze before the merge gate: the
    # gate then checks the latest-base ancestor, mergeability and the
    # exact reviewed head against the current remote head, and merges
    # only that head via --match-head-commit.
    refrozen = freeze_pr(worktree, branch, base_branch)
    if refrozen["head_oid"] != pr["head_oid"]:
        LOGGER.info(
            "review_head_advanced pr=%s round=%s frozen=%s reviewed=%s",
            pr["number"], round, pr["head_oid"], refrozen["head_oid"],
        )
    try:
        merged = merge_gate(worktree, refrozen, base_branch)
    except RuntimeError as exc:
        message = str(exc)
        # Behind and merge-conflict are the same next-session job:
        # absorb origin/<base>, resolve, retest. Other gate failures
        # (head moved, etc.) fail fast.
        if (
            "behind latest remote base" not in message
            and "not mergeable" not in message
        ):
            raise
        body = (
            f"{marker}\n"
            f"Muyan Pilot review round {round} for PR #{pr['number']}: "
            "the PR is behind the latest base or has a merge conflict; "
            f"the next review session merges the latest "
            f"origin/{base_branch} into the branch in-session, resolves "
            "conflicts, and reruns the full test suite"
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], body=body)
        edit_issue(
            number, repo=source_repo, add=FIX_NEEDED_LABEL,
            remove=PR_OPENED_LABEL,
        )
        return False
    confirmed = confirm_merged(worktree, merged, base_branch)
    # Issue #79: the merged publishing is bypass — the GitHub merge
    # already landed; a 404 here must not stop the `ai-merged`
    # transition and the merged PR scene comment below.
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round})"
        ),
    )
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.finish(_progress_body(
            _progress_state(
                issue=number, title=title, run_id=config["run_id"],
                role=ROLE_REVIEW, branch=branch,
                worktree=worktree, started=started,
                pr_url=merged["url"], review_round=round,
                priority=priority,
            ), outcome=(
                "**Muyan Pilot delivered**\n\n"
                f"PR {merged['url']} merged "
                f"(merge_commit={confirmed['merge_commit']} "
                f"review_rounds={round})"
            ),
        )),
    )
    # The GitHub merge already landed. Record ai-merged before touching
    # the local systemd checkout: a checkout that cannot fast-forward is
    # runner ops, not a failed delivery (must not become ai-blocked).
    edit_issue(
        number, repo=source_repo, add=MERGED_LABEL,
        remove=PR_OPENED_LABEL,
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{marker}\n"
            f"Muyan Pilot merged PR: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"base_branch={base_branch} run_id={config['run_id']})"
        ),
    )
    try:
        sync_base_checkout(config["repo_dir"], base_branch)
    except RuntimeError:
        LOGGER.exception(
            "base_checkout_sync_failed after merge pr=%s repo_dir=%s; "
            "the delivery already landed on origin/%s",
            merged["url"], config["repo_dir"], base_branch,
        )
    return True


def comment_pr(number: int, *, body: str) -> None:
    """Comment on a PR (used to record each review round's findings)."""
    run_command(["gh", "pr", "comment", str(number), "--body", body])

def _pr_head_repo(pr: dict) -> str:
    """Return `owner/name` of the PR head repo, or '<missing>' if absent."""
    owner = pr.get("headRepositoryOwner")
    repo = pr.get("headRepository")
    if not isinstance(owner, dict) or not isinstance(repo, dict):
        return "<missing>"
    login = owner.get("login")
    name = repo.get("name")
    if not isinstance(login, str) or not login \
            or not isinstance(name, str) or not name:
        return "<missing>"
    return f"{login}/{name}"




# pytest's final summary line: `1 failed, 155 passed in 4.43s` (the
# counts and the `in <seconds>` part are optional; the line is NOT
# wrapped in `=` section padding).
_Pytest_SUMMARY_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors|skipped|xfailed|xpassed"
    r"|deselected)(?:, \d+ \w+)*(?: in [\d.]+s)?$")


def _is_section_header(line: str) -> bool:
    """True for pytest section headers like `=== FAILURES ===`.

    A header is `=`-delimited at both ends (padding may be absent on
    one side for short titles) and its title carries no digits; the
    real summary line (`1 failed, 155 passed in 4.43s`, bare or
    `=`-padded) and the `FAILED`/`ERROR` evidence lines never match.
    """
    if not line.startswith("="):
        return False
    core = line.strip("=").strip()
    return core == "" or not any(ch.isdigit() for ch in core)


# A pytest summary line reports an outcome only when its FIRST count
# category is failed/passed/error(s): pytest orders the counts
# failed, passed, skipped, errors, xfailed, xpassed, deselected, so a
# run that collected tests always leads with failed or passed (or a
# collection error). `no tests ran in 0.01s` matches no summary regex
# at all; `3 deselected in 0.02s` / `2 skipped in 0.01s` match but
# carry no outcome — reporting them as a pass is a false notification
# (review round 3, PR #42).
_OUTCOME_FIRST_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors)\b",
)
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)")


def _is_no_result(line: str) -> bool:
    """True for pytest lines that verified nothing (no tests ran)."""
    if _NO_TESTS_RE.search(line):
        return True
    stripped = line.strip("=").strip()
    return bool(_Pytest_SUMMARY_RE.match(stripped)) \
        and not _OUTCOME_FIRST_RE.match(stripped)


def read_test_result(worktree: Path) -> str | None:
    """Summarize the worktree's `test.log`, or None when it does not exist.

    Prefers the pytest summary line (`1 failed, 155 passed in 4.43s`):
    the LAST one with an outcome when the log holds several runs (TDD
    red, then green), so the progress comment and the `tests
    passed/failed` milestone report the most recent run that actually
    collected tests. Section headers (`=== FAILURES ===`) are never
    reported: the fallback takes the first `FAILED`/`ERROR` evidence
    line or the last non-empty line instead, and a log holding nothing
    but headers yields None (no result info to report). Lines that
    verified nothing (`no tests ran`, `N deselected`, `N skipped`) are
    never reported either: a run that collected no tests is no result,
    and posting `tests passed` for it is a false notification (review
    round 3, PR #42).
    """
    path = worktree / "test.log"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        # The summary line is bare in pytest 9; some runners wrap it in
        # `=` padding, so the stripped form is matched as well.
        stripped = line.strip("=").strip()
        if _Pytest_SUMMARY_RE.match(line) \
                or _Pytest_SUMMARY_RE.match(stripped):
            if _is_no_result(line):
                # No tests collected in this run: keep looking for an
                # earlier run that did (or report nothing at all).
                continue
            return sanitize(stripped)
    for line in lines:
        if _is_section_header(line) or _is_no_result(line):
            continue
        if line.startswith(("FAILED", "ERROR")) or "passed" in line:
            return sanitize(line)
    last = lines[-1] if lines else None
    if last is not None and (_is_section_header(last)
                             or _is_no_result(last)):
        return None
    return sanitize(last) if last is not None else None


def delivery_head_advanced(worktree: Path, base_sha: str) -> bool:
    """True when the task branch has commits beyond the frozen base."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head != base_sha


def _progress_state(*, issue: int, title: str, run_id: str, role: str,
                    branch: str, worktree: Path, started: float,
                    pr_url: str | None, review_round: int, priority: str,
                    activity: dict | None = None) -> dict:
    """Collect the current run state for the GitHub progress comment.

    `title` is the issue's GitHub title (Issue #100): the progress
    comment's issue line shows `#<number> <title>` in every scene. It
    is required — the GitHub issue data contract guarantees a
    non-empty string title (every runner scan fetches it), and a
    missing title fails fast in `progress.issue_field` instead of
    fabricating one.
    `priority` is the pickup priority of the issue (`p0` or `normal`,
    Issue #101), derived from the issue's labels at claim/resume time.
    `activity` is the live state from the `stream_pi` watcher while a Pi
    session runs (fresh and already read); without it the newest session
    file is full-scanned. Activity snapshotting is best-effort
    observability: a read failure is logged and reported as "no session
    yet", it never blocks the task.
    """
    if activity is None:
        try:
            activity = activity_snapshot(worktree / ".pi-session")
        except Exception:
            LOGGER.exception("issue=%s activity snapshot failed", issue)
            activity = None
    return {
        "run_id": run_id,
        "issue": issue,
        "issue_title": title,
        "role": role,
        "priority": priority,
        "phase": (activity or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (activity or {}).get("last_activity"),
        "last_action": (activity or {}).get("action"),
        "tests": read_test_result(worktree),
        "review_round": review_round,
        "branch": branch,
        "pr": pr_url,
        "session": (activity or {}).get("session_id"),
        # Idle-stall recovery state (Issue #94): `term` / `kill` while
        # the runner recovers a stalled session, absent/None otherwise
        # (the body renders the line only while it is active).
        "recovery": (activity or {}).get("recovery"),
    }


def _progress_body(state: dict, *, outcome: str | None = None) -> str:
    """Render the progress body, optionally with a final outcome header."""
    body = progress_body(state)
    if outcome is None:
        return body
    return f"{outcome}\n\n{body}"


def _publish_plan_milestone(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the `plan ready` milestone once the worktree has a plan.md."""
    if (worktree / "plan.md").is_file():
        publisher.milestone("plan ready")


def _publish_test_milestone(publisher: ProgressPublisher,
                            worktree: Path) -> None:
    """Post `tests passed` / `tests failed` from the worktree's test.log.

    The failure check is case-insensitive: pytest evidence lines carry
    uppercase markers (`FAILED`, `FAILURES`) that a lowercase-only check
    would misreport as a pass (review round 2, PR #42).
    """
    result = read_test_result(worktree)
    if result is None:
        return
    lowered = result.lower()
    if "fail" in lowered or "error" in lowered:
        publisher.milestone(f"tests failed: {result}")
    else:
        publisher.milestone(f"tests passed: {result}")


def _failure_detail(exc: BaseException) -> str:
    """One-line failure description; keeps subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()}"
    return detail


def _safe_publish(*, run_id: str, issue: int, source_repo: str,
                  role: str, action: Callable[[], None]) -> None:
    """Run one progress-publishing step as a pure bypass (Issue #79).

    The main delivery path is claim -> worktree -> Pi -> verify PR ->
    review -> fix -> merge; the GitHub progress comment is observability
    on the side. A publishing failure (404, rate limit, API shape
    change) is logged as `progress_publish_failed` and never fails the
    delivery, never marks the Issue `ai-blocked`, and never skips
    `run_pi` / `wait_for_delivery`. This is the same semantics as the
    in-stream live-PATCH callback; Issue #60 already applied it to the
    post-PR record, Issue #79 extends it to the whole
    `ProgressPublisher` path (ensure / milestone / finish).
    """
    try:
        action()
    except Exception:
        LOGGER.exception(
            "progress_publish_failed run=%s issue=%s role=%s",
            run_id, issue_context(source_repo, issue), role,
        )


def _live_progress(publisher: ProgressPublisher, *, issue: int,
                   title: str, run_id: str, role: str, branch: str,
                   worktree: Path, started: float, pr_url: str | None,
                   review_round: int, priority: str,
                   activity: dict | None = None) -> None:
    """One live GitHub progress update while a Pi session is running.

    Called from the `stream_pi` poll loop (every activity change or
    heartbeat, Issue #18): the same run-marker comment is PATCHed in
    place at most every `PI_HEARTBEAT_SECONDS` or when the visible
    activity changed. `activity` is the watcher state of that poll, so
    the live comment shows the session exactly as the journal reports
    it. The publisher already knows the comment id after `ensure`, so a
    callback before it would fail fast here — and the wiring always
    ensures first.
    """
    state = _progress_state(
        issue=issue, title=title, run_id=run_id, role=role,
        branch=branch, worktree=worktree, started=started,
        pr_url=pr_url, review_round=review_round, priority=priority,
        activity=activity,
    )
    publisher.patch(_progress_body(state))


class LiveProgressThrottle:
    """Throttle live GitHub PATCHes to change-driven or <=30-second cadence.

    The `stream_pi` poll loop fires on every poll (15 s default); PATCHing
    GitHub on every poll would double the traffic for no visible gain.
    The throttle passes an update through when the visible activity
    (phase, action, result, model_wait) changed since the last PATCH or
    when at least `PI_HEARTBEAT_SECONDS` passed since it.
    """

    def __init__(self, publisher: ProgressPublisher, *, issue: int,
                 title: str, run_id: str, role: str, branch: str,
                 worktree: Path, started: float, pr_url: str | None,
                 review_round: int, priority: str) -> None:
        def publish(activity: dict) -> None:
            _live_progress(
                publisher, issue=issue, title=title, run_id=run_id,
                role=role, branch=branch, worktree=worktree,
                started=started,
                pr_url=pr_url, review_round=review_round,
                priority=priority, activity=activity,
            )

        self._publish = publish
        self._last_visible: tuple | None = None
        self._last_patch = 0.0

    def __call__(self, activity: dict) -> None:
        visible = (
            activity["phase"], activity["action"], activity["result"],
            activity["model_wait"],
            # The idle-recovery state is visible progress (Issue #94):
            # entering/leaving it PATCHes the live comment immediately.
            activity.get("recovery"),
        )
        now = time.monotonic()
        if visible == self._last_visible and \
                now - self._last_patch < PI_HEARTBEAT_SECONDS:
            return
        self._last_visible = visible
        self._last_patch = now
        self._publish(activity)


def process_issue(issue: dict, config: dict, source_repo: str) -> str:
    number = int(issue["number"])
    # Issue #100: the progress comment's issue line shows the number
    # AND the title in every scene. The scanned issue dict always
    # carries the GitHub title (every scan fetches `title`); a missing
    # or non-string title fails fast here (KeyError / ValueError in
    # `progress.issue_field`) — it is never fabricated.
    title = issue["title"]
    base_branch = config["base_branch"]
    # The run id is generated once per attempt and bound BEFORE any
    # other step is logged, so every journal line of the attempt
    # carries it — including the claim-time lines of the restart resume
    # scan below (Issue #41; review round 3, PR #42).
    run_id = new_run_id()
    set_run_id(run_id)
    # Restart resume (Issue #18): a killed runner leaves the task
    # worktree and the `ai-in-progress` label behind. Only in that state
    # the newest worktree's run id is reused, so the same hidden-marker
    # progress comment is found and kept instead of a second one.
    # Completed runs keep their worktrees as evidence but lose the
    # label, so re-claiming an issue always starts a fresh run.
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config["repo_dir"], source_repo, number,
        )
        if existing_run_id is not None:
            run_id = existing_run_id
            # The attempt continues the dead run: re-bind the reused
            # id so every later line (including resuming_run) carries
            # it.
            set_run_id(run_id)
            LOGGER.info(
                "issue=%s resuming_run run_id=%s",
                number, run_id,
            )
    base_sha = freeze_base(config["repo_dir"], base_branch)
    branch = task_branch(source_repo, number, run_id)
    # Pickup priority (Issue #101): derived from the scanned issue's
    # labels (no extra gh call) and carried on every journal line and
    # scene comment of the attempt via `run_info`.
    priority = issue_priority(issue)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id} "
        f"priority={priority}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    edit_issue(number, repo=source_repo, add=IN_PROGRESS_LABEL)
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    worktree: Path | None = None
    started = time.monotonic()
    # Issue #79: the `Muyan Pilot opened PR:` scene comment is the first
    # delivery step AFTER the opened-PR label transition that can still
    # fail; when it does, the failure path below must leave the Issue in
    # the terminal state `ai-blocked` ALONE (README label lifecycle:
    # `ai-pr-opened` is removed on terminal failure) — the same
    # convention as every other terminal failure path (verify_resumed_pr,
    # wait_for_delivery).
    pr_opened = False
    try:
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, base_sha,
        )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        comment_issue(
            number, repo=source_repo,
            body=started_pi_comment_body(run_id, run_info, branch, worktree),
        )
        # Issue #79: the whole ProgressPublisher path is a bypass — a
        # failure here (404, rate limit) is logged and never skips
        # `run_pi` or fails the delivery.
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.ensure(_progress_body(
                _progress_state(
                    issue=number, title=title, run_id=run_id,
                    role=ROLE_IMPLEMENT, branch=branch,
                    worktree=worktree, started=started,
                    pr_url=None, review_round=0, priority=priority,
                ),
            )),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.milestone(
                f"started: {run_info} branch={branch} worktree={worktree}"
            ),
        )
        run_pi(
            issue, worktree, config, source_repo, branch=branch,
            progress=LiveProgressThrottle(
                publisher, issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch, worktree=worktree,
                started=started, pr_url=None, review_round=0,
                priority=priority,
            ),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: _publish_plan_milestone(publisher, worktree),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: _publish_test_milestone(publisher, worktree),
        )
        pr_url = verify_pr(
            worktree, branch, base_branch, run_id, issue=number,
        )
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        # The PR opened milestone announces the delivery: the
        # implementer always commits the delivery on top of the frozen
        # base, so the head always advanced. (Issue #82 removed the
        # fixer's `fix pushed` milestone: findings are fixed by the
        # review session, which records its own round comments.)
        edit_issue(
            number, repo=source_repo, add=PR_OPENED_LABEL,
            remove=IN_PROGRESS_LABEL,
        )
        pr_opened = True
        # The scene comment is NOT a bypass (Issue #79): the next
        # tick's resume (Issue #45/#89) parses it to recover run_id,
        # base and PR, so a failure here is a real delivery failure —
        # it propagates into the failure path below (ai-blocked, the
        # `Muyan Pilot failed` comment, re-raise). The `ProgressPublisher`
        # steps around it stay bypass: a failure there (Issue #60: the
        # #57 delivered PATCH 404'd and the runner labeled the Issue
        # ai-blocked, skipping the review of a valid PR) is logged as
        # `progress_publish_failed` and the run continues into the
        # review/merge wait loop.
        comment_issue(
            number, repo=source_repo,
            body=opened_pr_comment_body(run_id, run_info, pr_url),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.milestone(
                f"PR opened: {pr_url} ({run_info})"
            ),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch,
                worktree=worktree, started=started,
                pr_url=pr_url, review_round=0, priority=priority,
            ), outcome="**Muyan Pilot delivered**")),
        )
        LOGGER.info(
            "run_end %s",
            format_end_scene(
                run_id=run_id, issue=issue_context(source_repo, number),
                role=ROLE_IMPLEMENT, result="pr_opened",
                elapsed=time.monotonic() - started,
                pr=pr_url, commit=commit,
            ),
        )
        return pr_url
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        scene = ""
        if worktree is not None:
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
                if snapshot is None:
                    # No session file yet: the scene still carries the full
                    # debug entry (worktree, branch) with '-' session fields.
                    snapshot = {
                        "session_id": None, "session_file": None,
                        "phase": "starting", "last_activity": None,
                        "action": None, "result": None,
                    }
                scene = format_run_scene(
                    snapshot,
                    run_id=run_id, issue=issue_context(source_repo, number),
                    role=ROLE_IMPLEMENT, branch=branch, worktree=str(worktree),
                )
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
        try:
            # The claim label is removed on every failure; when the
            # delivery already made the opened-PR transition (the
            # scene-comment failure of Issue #79), the opened-PR label
            # is removed too, so the terminal state is `ai-blocked`
            # ALONE — never `ai-pr-opened` + `ai-blocked` (README label
            # lifecycle: `ai-pr-opened` is removed on terminal failure).
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=(
                    PR_OPENED_LABEL if pr_opened else IN_PROGRESS_LABEL
                ),
            )
            detail = _failure_detail(exc)
            body = (
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot failed: {detail} ({run_info})"
            )
            if scene:
                body += f" {scene}"
            comment_issue(number, repo=source_repo, body=body)
            # The blocked milestone is posted even when the worktree was
            # never created or the progress comment was never ensured:
            # the mobile notification of the terminal failure must not
            # depend on local state. Both steps are bypass (Issue #79):
            # a progress 404 here must not abort the `ai-blocked`
            # transition above or the re-raise below.
            _safe_publish(
                run_id=run_id, issue=number, source_repo=source_repo,
                role=ROLE_IMPLEMENT,
                action=lambda: publisher.milestone(
                    f"blocked: {sanitize(detail)} ({run_info})"
                ),
            )
            if worktree is not None and publisher.comment_id is not None:
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_IMPLEMENT,
                    action=lambda: publisher.finish(_progress_body(
                        _progress_state(
                            issue=number, title=title, run_id=run_id,
                            role=ROLE_IMPLEMENT, branch=branch,
                            worktree=worktree, started=started,
                            pr_url=None, review_round=0,
                            priority=priority,
                        ), outcome=(
                            "**Muyan Pilot blocked**\n\n"
                            f"failure: {detail}\n"
                            "next step: fix the failure above and "
                            "re-run this Issue (a new run id is "
                            "created automatically)"
                        ),
                    )),
                )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def pr_state(pr_url: str) -> str:
    """Return the GitHub state of one PR: `OPEN`, `MERGED` or `CLOSED`.

    The delivery-wait loop (Issue #39) uses it to tell a delivery that is
    still awaiting review from one that is done: only `MERGED` or
    `CLOSED` ends the slot hold. Anything else is a corrupted state and
    fails fast.
    """
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    raw = run_command([
        "gh", "pr", "view", number, "--json", "state",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    return state


def issue_labels(number: int, repo: str) -> list[str]:
    """Return the current label names of one Issue."""
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "labels",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError("issue labels must be a JSON array")
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _finish_blocked_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, next_step: str, title: str,
    role: str = ROLE_REVIEW, review_round: int = 0,
    priority: str = "normal",
) -> None:
    """Finish the tracked progress comment with the blocked scene.

    `title` is the issue's GitHub title (Issue #100): the blocked scene
    shows `#<number> <title>` like every other progress scene; it is
    required, never fabricated.

    The contract (Issue #18): on failure the progress comment becomes
    the blocked scene with the next-step reason — the same terminal
    body the `process_issue` failure path writes. `ensure` finds the
    run's existing progress comment by its hidden marker (PATCHing it
    in place) or creates it when the run never reached one; either way
    the blocked scene is the final state. `role` and `review_round`
    are the actual role and completed review rounds of the blocked run
    (review round 2, PR #42): the caller derives them from the
    Issue's trusted review-round comments, so the terminal comment
    never shows a stale hardcoded role/round. Issue #82: the only
    post-PR role is `review` (the review session fixes findings in the
    same session), so the default is `ROLE_REVIEW`.
    """
    if run_id is None:
        return
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publisher.ensure(_progress_body(_progress_state(
        issue=number, title=title, run_id=run_id, role=role,
        branch=branch or "-", worktree=worktree or Path("-"),
        started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=(
        "**Muyan Pilot blocked**\n\n"
        f"failure: {detail}\n"
        f"next step: {next_step}"
    )))


def wait_for_delivery(pr_url: str, issue: dict, config: dict,
                      source_repo: str, poll_interval: float = PI_POLL_INTERVAL) -> None:
    """Own the delivery lifecycle: hold the slot until merge or failure.

    The slot is acquired by `main` before the claim and must stay
    occupied through implement -> review -> merge (Issue #39): a
    delivery whose PR is open still needs the machine, and no other
    Runner may start a second Pi while it is held. The Runner is the
    owner of that lifecycle, so it re-checks the delivery every
    `poll_interval` seconds (the same cadence as the Pi activity poll):

    - PR `MERGED` -> terminal: the delivery is done, the slot is
      released by the caller and the next tick may claim new work;
    - PR `CLOSED` without merge -> terminal failure: the Issue is
      marked `ai-blocked` (removing `ai-pr-opened`/`ai-fix-needed`) with
      a failure comment carrying the run marker, then the slot is
      released by the caller;
    - Issue in an opened-PR state (`ai-pr-opened` awaiting review, or
      `ai-fix-needed` awaiting the next review session after a finding
      or a base conflict) -> the Runner runs the independent review of
      the frozen PR (Issue #34) itself, on the same run, while still
      holding the slot: the review session fixes Blocker/Major findings
      IN THE SAME SESSION (Issue #82 — no cold-start fixer, no third
      review), a clean verdict re-freezes the head and merges the PR
      via the merge gate, confirms the merge, syncs the deployment
      checkout and labels the Issue `ai-merged` (terminal, the slot is
      released); unfixed findings or a behind/conflict gate label the
      Issue `ai-fix-needed` and the next iteration re-runs the same
      independent review;
    - otherwise -> keep holding the slot and re-check.

    There is no timeout: systemd owns the run lifecycle and kills the
    service on stop, which releases the slot via the kernel (flock on
    the open descriptor). No polling shim, no daemon loop, no queue.
    """
    number = int(issue["number"])
    # Issue #100: the progress comment's issue line shows the number
    # AND the title in every scene; the scanned issue dict always
    # carries the GitHub title (every scan fetches `title`) — a
    # missing or non-string title fails fast, never fabricated.
    title = issue["title"]
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    # Pickup priority (Issue #101): derived from the scanned issue's
    # labels (the resumable/in-flight scans fetch `labels`), so the
    # progress comment of a resumed P0 delivery keeps showing `p0`
    # through review/merge.
    priority = issue_priority(issue)
    LOGGER.info(
        "issue=%s delivery_awaiting pr=%s priority=%s; holding the "
        "slot until the PR is merged or terminally failed",
        number, pr_url, priority,
    )
    while True:
        state = pr_state(pr_url)
        if state == "MERGED":
            LOGGER.info(
                "issue=%s delivery_merged pr=%s; releasing the slot",
                number, pr_url,
            )
            return
        if state == "CLOSED":
            LOGGER.info(
                "issue=%s delivery_closed_unmerged pr=%s; marking the "
                "Issue ai-blocked and releasing the slot",
                number, pr_url,
            )
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=PR_OPENED_LABEL,
            )
            # A PR closed while in `ai-fix-needed` (awaiting the next
            # review session) leaves that label behind: remove it too,
            # so the terminal state is `ai-blocked` alone.
            if FIX_NEEDED_LABEL in issue_labels(number, source_repo):
                edit_issue(
                    number, repo=source_repo, remove=FIX_NEEDED_LABEL,
                )
            body = (
                f"Muyan Pilot failed: PR {pr_url} was closed without "
                "a merge; the delivery is terminally failed"
            )
            if marker:
                body = f"{marker}\n{body}"
            comment_issue(number, repo=source_repo, body=body)
            if run_id:
                # Issue #79: the blocked-scene progress publishing is
                # bypass — a 404 here must not escape the wait loop
                # (the terminal bookkeeping above already completed and
                # the slot must be released).
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
                    action=lambda: ProgressPublisher(
                        number, source_repo, run_id,
                        run_command=run_command,
                    ).milestone(
                        f"blocked: PR {pr_url} was closed without a "
                        "merge; the delivery is terminally failed"
                    ),
                )
                # The blocked scene carries the actual role and the
                # completed review rounds (review round 2, PR #42):
                # Issue #82 — both opened-PR states are review states
                # (the review session fixes findings in the same
                # session), so the role is always `review`, and the
                # trusted review-round comments bound the round count
                # (GitHub is the only state store).
                blocked_round = review_rounds_so_far(
                    issue_comments(number, repo=source_repo),
                )
                # The tracked progress comment becomes the blocked scene
                # (Issue #18): the same terminal body the other failure
                # paths write, with the next-step reason.
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
                    action=lambda: _finish_blocked_progress(
                        number, run_id, source_repo, None, None,
                        pr_url,
                        f"PR {pr_url} was closed without a merge; the "
                        "delivery is terminally failed",
                        "investigate why the PR was closed and re-open "
                        "the delivery or start a fresh run on the "
                        "Issue",
                        title=title,
                        role=ROLE_REVIEW, review_round=blocked_round,
                        priority=priority,
                    ),
                )
            return
        labels = issue_labels(number, source_repo)
        if ((PR_OPENED_LABEL in labels or FIX_NEEDED_LABEL in labels)
                and BLOCKED_LABEL not in labels
                and MERGED_LABEL not in labels):
            # The PR is in an opened-PR review state: run the
            # independent review of the frozen PR on the same run
            # (Issue #34). `ai-pr-opened` awaits review; `ai-fix-needed`
            # awaits the next review session after a finding or a base
            # conflict (Issue #82: the review session fixes findings in
            # the same session, so both states run the same review). A
            # clean verdict re-freezes the head, merges and returns
            # True (terminal); unfixed findings or a behind/conflict
            # gate label the Issue `ai-fix-needed` and the next
            # iteration re-runs the same independent review. A review
            # that cannot run (unrecoverable scene, missing worktree,
            # missing/malformed verdict, exhausted rounds) is a real
            # failure: the Issue is marked `ai-blocked` ALONE (the
            # opened-PR state label, `ai-pr-opened` or `ai-fix-needed`,
            # is removed) so it is never stranded in an opened-PR state
            # without an owner.
            worktree = None
            branch = None
            try:
                scene = resume_scene(
                    issue_comments(number, repo=source_repo),
                )
                # Issue #91: the scene freezes the base the PR was
                # opened against. The config may have moved on (or the
                # comment is stale): reviewing or merging a PR frozen on
                # another base against the configured one would run the
                # freeze/merge gate on the wrong base, so fail fast
                # before any git/Pi mutation instead of silently
                # switching bases. The handler below marks the Issue
                # ai-blocked with both base values named.
                if scene["base_branch"] != config["base_branch"]:
                    raise ValueError(
                        f"resume scene base_branch={scene['base_branch']} "
                        f"differs from configured base_branch="
                        f"{config['base_branch']}; the PR is frozen on a "
                        "different base and must not be reviewed or "
                        "merged against the configured one"
                    )
                worktree = worktree_path(
                    config["repo_dir"], source_repo, number,
                    scene["run_id"],
                )
                # Issue #90: the worktree is derived from the
                # configured repo_dir, source repo, Issue number and
                # run id (never read from a comment). A missing
                # directory means the scene cannot be resumed: fail
                # fast BEFORE any git/Pi mutation (no freeze_pr, no
                # review Pi) — the handler below marks the Issue
                # ai-blocked ALONE with the PR and branch preserved
                # (the pre-#82 resume_delivery fail-fast, restored).
                if not worktree.is_dir():
                    raise RuntimeError(f"worktree missing: {worktree}")
                branch = task_branch(source_repo, number, scene["run_id"])
                review_config = {
                    **config,
                    "base_sha": scene["base_sha"],
                    "run_id": scene["run_id"],
                }
                merged = review_and_merge_if_clean(
                    worktree, branch, config["base_branch"],
                    review_config, source_repo, number,
                    title=title, priority=priority,
                )
            except Exception as exc:
                LOGGER.exception(
                    "issue=%s delivery_review_failed pr=%s", number, pr_url,
                )
                edit_issue(
                    number, repo=source_repo, add=BLOCKED_LABEL,
                    remove=PR_OPENED_LABEL,
                )
                # A review failure while the Issue is in `ai-fix-needed`
                # (awaiting the next review session) leaves that label
                # behind: remove it too, so the terminal state is
                # `ai-blocked` alone (Issue #82 routes both opened-PR
                # states into the same review).
                if FIX_NEEDED_LABEL in issue_labels(number, source_repo):
                    edit_issue(
                        number, repo=source_repo, remove=FIX_NEEDED_LABEL,
                    )
                body = (
                    f"Muyan Pilot failed: the independent review of "
                    f"PR {pr_url} failed: {exc}"
                )
                if marker:
                    body = f"{marker}\n{body}"
                comment_issue(number, repo=source_repo, body=body)
                if run_id:
                    # Issue #79: the blocked-scene progress publishing
                    # is bypass — a 404 here must not escape the wait
                    # loop (the terminal bookkeeping above already
                    # completed and the slot must be released).
                    _safe_publish(
                        run_id=run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: ProgressPublisher(
                            number, source_repo, run_id,
                            run_command=run_command,
                        ).milestone(
                            f"blocked: the independent review of "
                            f"PR {pr_url} failed: {exc}"
                        ),
                    )
                    # The blocked scene carries the actual role and the
                    # completed review rounds (review round 2, PR #42):
                    # the failure happened during the independent
                    # review, and the trusted review-round comments
                    # bound the round count (GitHub is the only state
                    # store).
                    _safe_publish(
                        run_id=run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: _finish_blocked_progress(
                            number, run_id, source_repo, worktree,
                            branch, pr_url,
                            f"the independent review of PR {pr_url} "
                            f"failed: {exc}",
                            "fix the review failure above and resume "
                            "the delivery of this same PR",
                            title=title,
                            role=ROLE_REVIEW,
                            review_round=review_rounds_so_far(
                                issue_comments(number, repo=source_repo),
                            ),
                            priority=priority,
                        ),
                    )
                return
            if merged:
                LOGGER.info(
                    "issue=%s delivery_auto_merged pr=%s; releasing the "
                    "slot",
                    number, pr_url,
                )
                return
            continue  # findings: the next iteration re-runs the review
        LOGGER.info(
            "issue=%s delivery_awaiting pr=%s state=%s",
            number, pr_url, state,
        )
        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("MUYAN_PILOT_CONFIG", "muyan-pilot.toml")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=log_format())

    config = load_config(args.config)
    validate_config(config)
    # Deployment consistency (Issue #103, #142): BEFORE any slot or
    # claim the installed systemd units must match the repo templates
    # (the templates the ExecStartPre-synced checkout just loaded).
    # Drift is self-healed with the SAME idempotent install (copy the
    # templates, daemon-reload, enable the timer — never start/stop/
    # restart the service: a currently RUNNING task is never
    # interrupted) and re-verified with the SAME hash check. Drift
    # that survives the sync — or a failing install step — logs a
    # structured `unit_drift` line per unit and fails fast: this
    # start takes no slot, claims no Issue and changes no label.
    try:
        check_unit_drift(config["repo_dir"])
    except UnitDriftError:
        sync_drifted_units(config["repo_dir"], run_command=run_command)
    # Git transport preflight (Issue #114): BEFORE any slot or claim
    # the deployment checkout's git transport must be SSH and reachable
    # (the task worktrees share the checkout's single `origin` remote,
    # so the worktree's fetch/push — including `.github/workflows/*.yml`
    # — uses it). A broken transport fails the start with the
    # structured reason: no slot, no claim, no label change, no HTTPS
    # fallback. The `gh` token (GitHub API) is untouched.
    try:
        transport = check_transport(
            config["repo_dir"], config["source_repos"],
            run_command=run_command, migrate=False,
        )
    except TransportError as exc:
        LOGGER.error(
            "transport_check_failed repo_dir=%s source_repos=%s "
            "reason=%s",
            config["repo_dir"], config["source_repos"], exc,
        )
        raise
    LOGGER.info(
        "transport clean remote=%s protocol=%s url=%s "
        "ssh_reachable=%s",
        transport.get("remote", "-"), transport.get("protocol", "-"),
        transport.get("url", "-"), transport.get("ssh_reachable", "-"),
    )
    # Concurrency cap (Issue #39): take one slot BEFORE claiming anything.
    # The slot is held for the whole delivery lifecycle (implement ->
    # review -> fix -> merge) and released only after the delivery is
    # merged or terminally failed — or when this process exits for any
    # reason, which the kernel handles (flock on an open descriptor).
    slot = acquire_slot(
        config["slot_dir"], config["max_concurrency"], os.getpid(),
    )
    if slot is None:
        LOGGER.info(
            "capacity_full max_concurrency=%s slot_dir=%s",
            config["max_concurrency"], config["slot_dir"],
        )
        return 0
    try:
        selected = pick_next_delivery(
            config["source_repos"], config["slot_dir"],
            config["max_concurrency"],
            config["active_milestone"],
        )
        if selected is None:
            LOGGER.info(
                "source_repos=%s outcome=no_ready_issue",
                config["source_repos"],
            )
            return 0
        source_repo, issue, scene = selected
        if scene is not None:
            # An open PR is a recoverable review state: resume the
            # same run on the same branch, worktree and PR (Issue #45).
            # Bind the scene's run id first so every journal line and
            # GitHub comment of the resumed delivery carries it
            # (Issue #41). Both opened-PR states go straight to the
            # delivery wait: `ai-pr-opened` awaits review, and
            # `ai-fix-needed` awaits the next review session —
            # Issue #82: the review session itself fixes findings in the
            # same session, so there is no cold-start fixer to run
            # here (a stranded `ai-pr-opened` delivery, dead runner or
            # the progress failure of Issue #70, is reviewed the same
            # way).
            set_run_id(scene["run_id"])
            # Issue #89: verify the open PR BEFORE any git/Pi mutation
            # (head repo, base, run marker, exact URL of the recovered
            # scene — the pre-#82 resume_delivery check, restored):
            # the wait receives the VERIFIED URL, never the comment
            # string, so a comment can never steer the runner into the
            # wrong PR (Issue #45). A mismatch is terminal: the Issue
            # is marked ai-blocked and the tick stops.
            pr_url = verify_resumed_pr(scene, issue, config, source_repo)
        else:
            pr_url = process_issue(issue, config, source_repo)
        # The delivery is not done when the PR is open: hold the slot
        # through review -> merge and release it only after the PR is
        # merged or terminally failed (Issue #39).
        wait_for_delivery(pr_url, issue, config, source_repo)
    finally:
        slot.release()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
