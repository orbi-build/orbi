"""Launching one Pi session: the implement, review and ticket-only roles.

Extracted unchanged from ``runner.py`` (Issue #1262, Article 3.2): this
module owns the session boundary — the prompt rendering, the resume
context, the per-run agent directory, the Runner-owned runtime excludes
and the four launch functions (``run_pi``, ``run_review``,
``run_ticket_agent``, ``run_clarify_agent``). It imports no delivery
state machine (Article 3.3): every dependency is a leaf module, and
``runner`` imports from here, never the other way round.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from orbi import config as config_domain
from orbi.delivery_scene import RunContext
from orbi.github import (
    _comment_is_trusted,
    comment_issue,
    issue_comments,
    issue_view,
    normalize_pr_feedback,
    pr_comments,
    pr_review_comments,
    pr_reviews,
    trusted_issue_comments_block,
)
from orbi.gitops import base_sync_lock_path
from orbi.journal import LOGGER, event, issue_context, run_command
from orbi.pi_activity import activity_snapshot
from orbi.pi_command import (
    IMPLEMENT_EXCLUDED_SKILLS,
    REVIEW_EXCLUDED_SKILLS,
    ROLE_REVIEW,
    ROLE_TICKET,
    _skills_for,
    build_pi_command,
)
from orbi.pi_process import (
    ROLE_IMPLEMENT,
    PiWatchOptions,
    SteeringRequest,
    _log_provider_config_loaded,
    stream_pi,
)
from orbi.repo_config import validate_context_file

# Runner-owned runtime paths inside a task worktree: created
# by the parent Runner and the Pi session machinery, never by the agent's
# delivery. The task branch's tracked `.gitignore` must NOT be the thing
# that keeps them out of the delivery commit boundary — a task may legally
# rename that file (the #246 brand-rename scene, run `b879a88c`), so the
# Runner pins its own runtime paths in the worktree's LOCAL git exclude
# (`.git/info/exclude`): git metadata that never enters an agent commit and
# never depends on the task branch's content. The #246 rename converged the
# legacy state dir onto `.orbi/`, so the migration window is closed and a
# single pattern covers it.
#
# The set also covers the ORBI CONTRACT ARTIFACTS: the pi-loop
# plugin state (#215) and the per-run plan/test/verify artifacts the
# Runner's own prompt tells the agent to write (once at the worktree
# root, now under the excluded `.orbi/` run dir). The four historical
# dirty-gate incidents (#215/#235/#256/#301) were all orbi-owned artifacts
# blocking a finished delivery — the exemption is now the Runner's runtime
# behavior, not a hand-maintained tracked blacklist. Excludes hide only
# untracked paths, so a modified tracked file or a committed artifact
# still fails the gate; coverage command artifacts are NOT in this set —
# the contract commands write them into the excluded `.orbi/` run dir and
# the tracked `.gitignore` stays as the fallback layer.
RUNNER_RUNTIME_EXCLUDES = (
    ".orbi/",
    ".worktrees/",
    ".pi-session/",
    ".pi/",
    "plan.md",
    "test.log",
    "verify.md",
)


def runner_runtime_exclude_path(worktree: Path) -> Path:
    """The task worktree's local git exclude file (`.git/info/exclude`).

    A linked worktree's `.git` is a pointer file (`gitdir: <path>`) that
    resolves to `<common-gitdir>/worktrees/<name>`. Git applies the
    exclude file of the COMMON gitdir to every worktree of the repo (the
    worktree-specific gitdir carries no exclude of its own — verified
    against real git), so the exclude is written to
    `<common-gitdir>/info/exclude`. That is repository-local metadata:
    it never enters an agent commit and never touches the user's global
    excludes (`core.excludesFile`).
    """
    git_entry = worktree / ".git"
    if git_entry.is_file():
        for line in git_entry.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                git_dir = Path(line.split(":", 1)[1].strip())
                # <common-gitdir>/worktrees/<name> -> <common-gitdir>
                common_gitdir = git_dir.parent.parent
                return common_gitdir / "info" / "exclude"
        raise ValueError(
            f"worktree .git pointer {git_entry} has no gitdir entry"
        )
    return git_entry / "info" / "exclude"


def apply_runner_runtime_excludes(worktree: Path) -> None:
    """Idempotently pin the Runner-owned runtime paths in the worktree's
    local git exclude.

    Existing exclude content (including user-written patterns) is
    preserved verbatim; a pattern already present is never written twice.
    Called before every Pi launch (implement, resume, review) and before
    the delivery commit-boundary check. A directory without a `.git`
    entry (unit-test tmp dirs) is a no-op: the delivery commit boundary
    still fails fast on a real corrupted scene.
    """
    git_entry = worktree / ".git"
    if not git_entry.exists():
        event(
            "runner_runtime_exclude_skipped", level=logging.DEBUG,
            worktree=worktree, reason="no .git entry",
        )
        return
    exclude_path = runner_runtime_exclude_path(worktree)
    existing = ""
    if exclude_path.is_file():
        existing = exclude_path.read_text(encoding="utf-8")
    present = {line.strip() for line in existing.splitlines()}
    missing = [p for p in RUNNER_RUNTIME_EXCLUDES if p not in present]
    if not missing:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    exclude_path.write_text(
        existing + prefix + "\n".join(missing) + "\n", encoding="utf-8",
    )


def _is_runner_runtime_only(status: str) -> bool:
    """True when EVERY non-empty porcelain entry is a Runner-owned runtime
    path: the delivery repair may then continue; any
    agent-owned entry keeps the `delivery_uncommitted_changes` fail fast."""
    entries = [line for line in status.splitlines() if line.strip()]
    if not entries:
        return False
    for line in entries:
        path = line[3:].strip()
        if path.startswith('"') and path.endswith('"'):
            # Porcelain quotes paths with special characters; the runner
            # paths are plain ASCII, so an unquoted match is exact.
            path = path[1:-1]
        if not any(
            path == pattern.strip("/") or path.startswith(pattern)
            for pattern in RUNNER_RUNTIME_EXCLUDES
        ):
            return False
    return True


def render_prompt(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def changed_files(worktree: Path) -> list[str]:
    """The worktree's uncommitted changes (tracked + untracked paths)."""
    raw = run_command(["git", "status", "--porcelain"], cwd=worktree)
    files: list[str] = []
    for line in raw.splitlines():
        if len(line) > 3 and line[:2].strip():
            files.append(line[3:].strip())
    return files


def resume_context(worktree: Path, steering_comments: list[dict] | None = None,
                   body_revision: dict | None = None) -> str | None:
    """The resume context for a continued run, or None.

    A worktree without uncommitted changes and without a previous
    session is a fresh scene: the agent starts from the Issue alone
    (the exact pre-#219 prompt). Otherwise the new session must
    continue the existing work: the context carries the instruction
    to continue (never redo, never discard), the previous session's
    progress and the list of changed files — the agent inspects the
    actual diff itself, it runs inside the worktree.

    `body_revision` is a steering correction carried as an edited
    Issue body (Issue #1094; keys `issue`, `body`): it renders under
    the 「正文已更新」 header before the steering comments' 「新评论」 block.
    """
    files = changed_files(worktree)
    snapshot = activity_snapshot(worktree / ".pi-session")
    if (not files and snapshot is None and not steering_comments
            and not body_revision):
        return None
    lines = [
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.",
    ]
    if snapshot is not None:
        lines.append(
            "Previous session progress: "
            f"session={snapshot.get('session_id') or '-'} "
            f"events={snapshot.get('events', 0)} "
            f"phase={snapshot.get('phase') or '-'} "
            f"last_action={snapshot.get('action') or '-'} "
            f"last_result={snapshot.get('result') or '-'}"
        )
    if files:
        lines.append(
            f"Uncommitted changed files ({len(files)}):"
        )
        lines.extend(f"- {path}" for path in files)
    if steering_comments or body_revision:
        if body_revision:
            lines.append(
                f"[方向修正 · 来自 Issue #{body_revision.get('issue', '-')} 的正文已更新 · "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} local time]"
            )
            lines.append(str(body_revision.get("body") or "").rstrip())
        if steering_comments:
            lines.append(
                f"[方向修正 · 来自 Issue #{steering_comments[0].get('issue', '-') } 的新评论 · "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} local time]"
            )
            for comment in steering_comments:
                author = comment.get("author")
                login = author.get("login") if isinstance(author, dict) else "unknown"
                lines.append(f"{login}: {str(comment.get('body') or '').rstrip()}")
        lines.append(
            "以上是在你开始这轮工作之后补充的说明，你的上一个会话没有看到它。"
        )
        lines.append(
            "如果它与你已完成的改动冲突，以这条为准，修正已有实现；"
            "如果只是补充信息，按原方向继续。"
        )
    return "\n".join(lines)


def _resolve_enabled_models(patterns: list, providers: dict) -> list:
    """Filter `enabledModels` patterns to the merged catalog.

    Mirrors Pi's exact reference match (`model-resolver.js`
    `findExactModelReferenceMatch`, verified against Pi 0.84.3): the
    canonical `provider/modelId` form, or a bare model id that is
    unambiguous across the catalog — case-insensitive. A pattern that
    resolves to nothing would make Pi warn `No models match pattern`
    at startup and could steer the initial model selection to a model
    the run cannot use, so the per-run settings.json keeps only what
    the per-run models.json can resolve.
    """
    canonical: set = set()
    bare_counts: dict = {}
    for provider_id, entry in providers.items():
        models = entry.get("models") if isinstance(entry, dict) else None
        if not isinstance(models, list):
            continue
        for model in models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str) or not model_id:
                continue
            canonical.add(f"{provider_id}/{model_id}".lower())
            key = model_id.lower()
            bare_counts[key] = bare_counts.get(key, 0) + 1
    resolved = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        reference = pattern.strip().lower()
        if reference in canonical:
            resolved.append(pattern)
        elif bare_counts.get(reference, 0) == 1:
            resolved.append(pattern)
    return resolved


def prepare_pi_agent_dir(worktree: Path, config: config_domain.RunnerConfig,
                         role: str = ROLE_IMPLEMENT) -> Path | None:
    """Materialize the per-run Pi agent dir.

    Returns None when no provider file is configured — the Pi command
    and environment keep their exact pre-#157 shape (Pi uses its own
    agent dir). Otherwise creates `<worktree>/.orbi/pi-agent/`
    (gitignored, per-run) and returns it:

    - `models.json`: the user agent dir's providers merged with the
      configured file's providers (the file wins on id collision) —
      the user's existing providers keep working, the file adds or
      overrides; the merged catalog is what Pi loads via
      `PI_CODING_AGENT_DIR` (verified against real Pi 0.84.3);
    - `auth.json`: a SYMLINK to the user agent dir's file when it
      exists, so Pi's stored auth is unchanged; `settings.json` is a
      per-run REAL file (see below).

    The per-run `settings.json` is a REAL file, consistent
    with the per-run catalog:

    - base: the user agent dir's settings when it exists (the user's
      other settings are preserved), `{}` otherwise — the user's global
      `~/.pi/agent/settings.json` is never modified;
    - `pi_provider`/`pi_model` configured: `defaultProvider` /
      `defaultModel` point at the selected provider/model and
      `enabledModels` is exactly that model, so the initial model
      selection (CLI flags, then scoped models, then settings defaults)
      can only land on a model of the merged catalog;
    - not configured: `enabledModels` keeps only the patterns that
      resolve in the merged catalog (Pi's exact reference match:
      canonical `provider/modelId` or unambiguous bare model id,
      case-insensitive); a pattern that resolves to nothing would make
      Pi warn `No models match pattern` at startup and could steer the
      initial model to a provider the run cannot use — an empty result
      drops the key entirely (Pi falls back to the full catalog);
    - a user `httpIdleTimeoutMs` of `0` (Pi's documented "disabled")
      is dropped: with it, a first response that never arrives hangs
      forever; without the key Pi applies its built-in default (300s)
      and the request fails with a concrete timeout error instead.

    `auth.json` stays a SYMLINK to the user agent dir's file when it
    exists: stored auth for providers present in the merged catalog is
    still valid.

    `apiKey` env-var references (`$VAR` / `${VAR}`) are resolved into
    the per-run copy: config load already required the
    SELECTED provider's references to resolve, so the materialized
    catalog carries a usable real credential — without it Pi would
    hold the literal `$VAR` string and the request could never
    authenticate. References whose variable is missing or empty (only
    possible for non-selected providers) stay verbatim. The user's
    provider file and user agent dir are never modified, and the
    resolved key never reaches the journal, a comment, or a commit:
    the per-run dir is the gitignored `<worktree>/.orbi/pi-agent/`.
    """
    providers_data = config.pi_providers_data
    if providers_data is None:
        return None
    agent_dir = worktree / ".orbi" / "pi-agent"
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
    # The per-run copy carries the resolved `apiKey` values
    # (entries with a string key are copied, so the loaded config data
    # keeps its literal references).
    resolved_providers: dict = {}
    for provider_id, entry in merged_providers.items():
        api_key = entry.get("apiKey") if isinstance(entry, dict) else None
        if isinstance(api_key, str) and api_key:
            entry = {**entry, "apiKey": config_domain._expand_pi_api_key_refs(api_key)}
        resolved_providers[provider_id] = entry
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": resolved_providers}, indent=2),
        encoding="utf-8",
    )
    # Per-run settings.json: a real file consistent with
    # the merged catalog above, never a symlink to the user's global
    # settings (whose defaults/enabledModels may reference models this
    # run's catalog cannot resolve). Idempotent for a resumed run in
    # the same worktree: a stale file or symlink from an earlier
    # attempt is replaced, never kept.
    user_settings = user_agent / "settings.json"
    base_settings: dict = {}
    if user_settings.is_file():
        try:
            loaded = json.loads(user_settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"user agent dir settings.json {user_settings} is not "
                f"valid JSON: {exc}"
            ) from None
        if not isinstance(loaded, dict):
            raise ValueError(
                f"user agent dir settings.json {user_settings} must be "
                f"a JSON object"
            )
        base_settings = loaded
    settings = dict(base_settings)
    if role == ROLE_REVIEW:
        pi_provider = config.review_pi_provider or config.pi_provider
        pi_model = config.review_pi_model or config.pi_model
    else:
        pi_provider = config.pi_provider
        pi_model = config.pi_model
    if pi_provider is not None and pi_model is not None:
        settings["defaultProvider"] = pi_provider
        settings["defaultModel"] = pi_model
        settings["enabledModels"] = [f"{pi_provider}/{pi_model}"]
    else:
        patterns = settings.get("enabledModels")
        if isinstance(patterns, list):
            resolved = _resolve_enabled_models(patterns, merged_providers)
            if resolved:
                settings["enabledModels"] = resolved
            else:
                settings.pop("enabledModels", None)
    if settings.get("httpIdleTimeoutMs") == 0:
        settings.pop("httpIdleTimeoutMs")
    stale = agent_dir / "settings.json"
    if stale.is_symlink() or stale.is_file():
        stale.unlink()
    stale.write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )
    # auth.json keeps its pre-#172 shape: a symlink to the user's
    # stored auth (valid for the merged catalog's providers).
    auth_source = user_agent / "auth.json"
    auth_link = agent_dir / "auth.json"
    if auth_link.is_symlink():
        auth_link.unlink()
    if auth_source.is_file():
        auth_link.symlink_to(auth_source)
    return agent_dir


def _pi_extension_env(config: config_domain.RunnerConfig) -> dict[str, str]:
    """Return extension variables for the Pi child only; never log them."""
    values: dict[str, str] = {}
    for extension in config.pi_extensions:
        if extension["enabled"]:
            values.update(extension["env"])
    return values


def run_pi(issue: dict, ctx: RunContext, config: config_domain.RunnerConfig, *,
           timeout: int | None = None,
           progress: Callable[[dict], None] | None = None,
           resume_context: str | None = None) -> str:
    """Run the implementer Pi session for a freshly claimed Issue.

    Findings are fixed by the review session in the same session, so
    the implementer is the only user of `prompts/prompt.md`.

    `ctx` is the run-identity bundle (Issue #290): the delivery
    worktree, branch, source repo, issue number and run id travel as
    one frozen value.

    `resume_context`: when the worktree already carries
    the interrupted run's work (uncommitted changes and/or a previous
    session), the context argument gains the resume section so the NEW
    session continues the existing work instead of a fresh redo. The
    prompt template itself is untouched; absent -> the exact
    pre-#219 context.
    """
    worktree = ctx.worktree
    source_repo = ctx.source_repo
    branch: str = ctx.branch
    # Pin the Runner-owned runtime paths in the worktree's
    # local exclude BEFORE Pi starts (covers create, resume and
    # implement) — the tracked .gitignore is the agent's to rename.
    apply_runner_runtime_excludes(worktree)
    # The run artifact dir exists BEFORE the session starts,
    # so the contract commands write `.orbi/plan.md`, `.orbi/test.log`
    # and the coverage artifacts without a mkdir step (a shell redirect
    # into a missing directory fails the command outright).
    (worktree / ".orbi").mkdir(exist_ok=True)
    started = time.monotonic()
    # The repository policy's context files are
    # repository-relative; resolve them against the delivery worktree and
    # enforce existence + the size cap before injection (D2).
    context_files = list(config.context_files)
    for relative in config.repo_context_files:
        context_files.append(validate_context_file(worktree, relative))
    template = config.prompt.read_text(encoding="utf-8")
    prompt_values = {
        "SOURCE_REPO": source_repo,
        "SOURCE_REPOS": ", ".join(config.source_repos),
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": issue.get("body", ""),
        "WORKSPACE_ROOT": str(config.workspace_root),
        "CONTEXT_FILES": "\n".join(str(path) for path in context_files),
        "SKILLS": "\n".join(
            str(path)
            for path in _skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)
        ),
        "BASE_BRANCH": config.base_branch,
        "BASE_SHA": config.base_sha,
        "RUN_ID": config.run_id,
        # The implementer prompt no longer carries the
        # base-sync lock (the base fetch is the Runner's operation);
        # the value stays available for custom prompt templates.
        "BASE_SYNC_LOCK": str(base_sync_lock_path(config.repo_dir)),
    }
    # The trusted-comment timeline enters the task context
    # only when the template carries the placeholder — a template
    # without it keeps the exact pre-#745 behavior (no extra GitHub
    # read, no new failure mode).
    if "{{ISSUE_COMMENTS}}" in template:
        prompt_values["ISSUE_COMMENTS"] = trusted_issue_comments_block(
            issue_comments(int(issue["number"]), repo=source_repo),
            config.issue_comments_limit,
        )
    system_prompt = render_prompt(template, prompt_values)
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
    )
    context += "Complete the delivery process in the system prompt."
    if resume_context:
        context += f"\n{resume_context}"
    steering_seen: set[str] = set()
    steering_rounds = 0
    steering_started = time.time()
    steering_limit_logged = False
    steering_limit_notice_logged = False
    # The body this run started from (Issue #1094): the exact text the
    # prompt above embeds. The steering poll compares the live body
    # against it — a maintainer rewrite of the Issue body steers like a
    # new comment.
    steering_body = issue.get("body", "")
    # `resume_context` is also the public parameter name of this function;
    # keep an unshadowed reference for the restart callback below.
    build_resume_context = globals()["resume_context"]

    def check_steering() -> SteeringRequest | None:
        nonlocal steering_rounds, steering_limit_logged
        nonlocal steering_limit_notice_logged, steering_body
        limit_reached = steering_rounds >= config.steering_max_rounds
        if limit_reached and not steering_limit_logged:
            steering_limit_logged = True
            event(
                "steering_limit_reached", issue=issue_context(
                    source_repo, int(issue["number"]),
                ), round=steering_rounds,
            )
        try:
            # ONE request per poll (Issue #1094): the same `gh issue
            # view` that lists the comments also returns the body, so a
            # body rewrite needs no second call and no new poll.
            snapshot = issue_view(
                int(issue["number"]), "comments,body",
                repo=source_repo, timeout=30,
            )
            comments = snapshot.get("comments")
            if not isinstance(comments, list):
                raise ValueError("issue comments must be a JSON array")
        except Exception as exc:
            event(
                "steering_poll_failed", level=logging.WARNING,
                issue=issue_context(source_repo, int(issue["number"])),
                reason=type(exc).__name__,
            )
            return None
        # A str-only compare: an absent body field cannot prove an edit
        # and leaves body steering inert for the poll (the bypass shape).
        current_body = snapshot.get("body")
        body_changed = (
            isinstance(current_body, str) and current_body != steering_body
        )
        fresh: list[dict] = []
        # GitHub exposes comment timestamps only to whole seconds. Round
        # the boundary up so a comment created in the startup second is not
        # mistaken for a post-start correction.
        started_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(math.ceil(steering_started))
        )
        for comment in comments:
            identifier = str(comment.get("id", ""))
            if not identifier or identifier in steering_seen:
                continue
            steering_seen.add(identifier)
            if str(comment.get("createdAt") or "") < started_at:
                continue
            if _comment_is_trusted(comment):
                item = dict(comment)
                item["issue"] = issue["number"]
                fresh.append(item)
        if limit_reached:
            if (fresh or body_changed) and not steering_limit_notice_logged:
                steering_limit_notice_logged = True
                try:
                    comment_issue(
                        int(issue["number"]), repo=source_repo,
                        body=(
                            f"<!-- orbi:run={config.run_id} -->\n"
                            "The steering limit was reached, so this correction "
                            "was not applied.\n"
                            f"run_id={config.run_id}"
                        ),
                    )
                except Exception as exc:
                    event(
                        "steering_limit_notice_failed", level=logging.WARNING,
                        issue=issue_context(source_repo, int(issue["number"])),
                        reason=type(exc).__name__,
                    )
            return None
        if not fresh and not body_changed:
            return None
        steering_rounds += 1
        authors = []
        for comment in fresh:
            author = comment.get("author")
            authors.append(author.get("login") if isinstance(author, dict) else "unknown")
        if body_changed:
            # The recorded body becomes the edited one, so the SAME edit
            # never triggers a second restart (Issue #1094).
            steering_body = current_body
        resume = build_resume_context(
            worktree, fresh,
            body_revision=(
                {"issue": issue["number"], "body": current_body}
                if body_changed else None
            ),
        ) or ""
        return SteeringRequest(
            context=f"{context}\n{resume}",
            comment_ids=tuple(str(item["id"]) for item in fresh),
            author=", ".join(authors),
            body_revision=body_changed,
        )
    command, log_command = build_pi_command(
        config, ROLE_IMPLEMENT, IMPLEMENT_EXCLUDED_SKILLS,
        worktree / ".pi-session", system_prompt, context,
        context_placeholder="<issue-context-redacted>",
        tools=True, extensions=True,
    )
    # The provider file (baseUrl / api / apiKey / models)
    # reaches Pi through the materialized per-run agent dir, never
    # through the command line or the log (the redacted command keeps
    # only the #119 provider/model/thinking identifiers). Unconfigured
    # -> the stream_pi call keeps its exact pre-#157 shape.
    agent_dir = prepare_pi_agent_dir(worktree, config, role=ROLE_IMPLEMENT)
    # Startup phase: the provider config is loaded and
    # materialized for this run (or resolved to Pi's own agent dir when
    # unconfigured) — the first startup line, before the process is
    # spawned.
    _log_provider_config_loaded(
        issue_ref=issue_context(source_repo, int(issue["number"])),
        role=ROLE_IMPLEMENT, config=config,
        elapsed=time.monotonic() - started,
    )
    extra = {}
    pi_env = _pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    if pi_env:
        extra["pi_env"] = pi_env
    return stream_pi(
        command,
        cwd=worktree,
        ctx=ctx,
        timeout=timeout,
        log_command=log_command,
        progress=progress,
        # The configured model_wait dead threshold and the
        # /slots swallow probe (absent URL -> disabled, the exact
        # pre-#233 behavior) ride the watch bundle (the real config_domain.load_config
        # always provides the keys; the module constants stay the
        # fallback for hand-built configs).
        watch=PiWatchOptions(
            model_wait_dead_seconds=config.model_wait_dead_seconds,
            model_wait_probe_url=config.model_wait_probe_url,
            model_wait_probe_seconds=config.model_wait_probe_seconds,
            steering_poll_seconds=config.steering_poll_seconds,
            steering_check=check_steering if config.steering_enabled else None,
        ),
        **extra,
    )


def run_review(ctx: RunContext, pr: dict, config: config_domain.RunnerConfig, round: int,
               timeout: int | None = None,
               progress: Callable[[dict], None] | None = None) -> str:
    """Run one independent review session for a frozen PR.

    The session is independent (new process, `prompts/prompt_review.md`, a new
    session JSONL) and reviews the exact frozen base/head. When it finds Blocker/Major issues it fixes them IN THIS SAME
    SESSION (modify code, run the full test suite with coverage, commit
    and push the task branch) and re-emits the final verdict — there is
    no cold-start fixer and no third review. The review streams live
    activity through the same pipeline as the implementer (role=review;
    One run_id end to end, the roles are steps of the same
    run).
    """
    worktree = ctx.worktree
    source_repo: str = ctx.source_repo
    issue: int = ctx.issue
    branch: str = ctx.branch
    # The review/fix session gets the SAME local-exclude
    # preflight as the implementer (one idempotent helper, Pi 前).
    apply_runner_runtime_excludes(worktree)
    # Same run-dir guarantee as the implementer — the
    # review session reads/writes the same `.orbi/` artifacts.
    (worktree / ".orbi").mkdir(exist_ok=True)
    started = time.monotonic()
    review_template = config.prompt_review.read_text(encoding="utf-8")
    # Acceptance criteria are mutable review input. Read the Issue body for
    # every round, rather than relying on the reviewer's initiative or on a
    # body captured by an earlier run. Custom templates that do not consume
    # it retain their historical GitHub-read behavior.
    issue_body = ""
    if "{{ISSUE_BODY}}" in review_template:
        issue_data = issue_view(issue, "body", repo=source_repo)
        issue_body = issue_data.get("body") or ""
        if not isinstance(issue_body, str):
            raise ValueError("issue body must be a string")
    review_values = {
        "SOURCE_REPO": source_repo,
        "PR_NUMBER": str(pr["number"]),
        "PR_URL": pr["url"],
        "BASE_BRANCH": config.base_branch,
        "BASE_SHA": pr["base_oid"],
        "HEAD_SHA": pr["head_oid"],
        "HEAD_REF": pr["head_ref"],
        "ROUND": str(round),
        # The SAME shared base-sync lock as the
        # implementer — the review session's base-absorb fetch must
        # run under it (flock <lock> git fetch origin <base>).
        "BASE_SYNC_LOCK": str(base_sync_lock_path(config.repo_dir)),
    }
    # The review path sees one bounded trusted timeline. PR feedback is input
    # only: delivery state remains on the Issue and the current PR is the sole
    # PR source selected by this call.
    if "{{ISSUE_COMMENTS}}" in review_template:
        comments = issue_comments(issue, repo=source_repo)
        try:
            feedback = pr_comments(pr["number"], repo=source_repo)
            feedback += pr_reviews(pr["number"], repo=source_repo)
            feedback += pr_review_comments(pr["number"], repo=source_repo)
            comments += normalize_pr_feedback(feedback)
            comments.sort(key=lambda item: str(item.get("createdAt") or ""))
        except Exception:
            LOGGER.exception(
                "pr_review_feedback_read_failed repo=%s pr=%s",
                source_repo, pr["number"],
            )
        review_values["ISSUE_COMMENTS"] = trusted_issue_comments_block(
            comments, config.issue_comments_limit,
        )
    # Substitute the body last so placeholder-shaped text in the body remains
    # verbatim instead of being recursively interpreted as a prompt variable.
    review_values["ISSUE_BODY"] = issue_body
    system_prompt = render_prompt(review_template, review_values)
    context = (
        f"Independently review PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} against base {config.base_branch}@{pr['base_oid']} "
        f"and head {pr['head_oid']} (round {round}). Follow code-review R1-R9; "
        "fix Blocker/Major findings in this same session (push only the "
        "task branch) and end with a single REVIEW_VERDICT line carrying "
        "the head it covers."
    )
    command, log_command = build_pi_command(
        config, ROLE_REVIEW, REVIEW_EXCLUDED_SKILLS,
        worktree / ".pi-session", system_prompt, context,
        context_placeholder="<review-context-redacted>",
        tools=True, extensions=True,
    )
    # The review session uses its role-specific provider selection,
    # falling back to the implementer selection when no override exists.
    agent_dir = prepare_pi_agent_dir(worktree, config, role=ROLE_REVIEW)
    # Startup phase: the review session's provider config
    # is loaded and materialized too (same line shape, role=review).
    _log_provider_config_loaded(
        issue_ref=issue_context(source_repo, issue),
        role=ROLE_REVIEW, config=config,
        elapsed=time.monotonic() - started,
    )
    extra = {}
    pi_env = _pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    if pi_env:
        extra["pi_env"] = pi_env
    return stream_pi(
        command,
        cwd=worktree,
        ctx=ctx,
        timeout=timeout,
        role=ROLE_REVIEW,
        log_command=log_command,
        progress=progress,
        # The review session uses the SAME configured
        # model_wait dead threshold and /slots swallow probe as the
        # implementer (the real config_domain.load_config always provides the keys;
        # the module constants stay the fallback for hand-built
        # configs).
        watch=PiWatchOptions(
            model_wait_dead_seconds=config.model_wait_dead_seconds,
            model_wait_probe_url=config.model_wait_probe_url,
            model_wait_probe_seconds=config.model_wait_probe_seconds,
        ),
        **extra,
    )


def run_clarify_agent(issue: dict, config: config_domain.RunnerConfig,
                      source_repo: str, run_id: str, *, system_prompt: str,
                      context: str,
                      progress: Callable[[dict], None] | None = None) -> str:
    """Ask one no-tools Pi session whether this ticket is deliverable (#1088).

    The gate runs BEFORE any worktree or branch exists, so the session is
    transient OS state in a temp dir (like the ticket-only session) — a
    stopped ticket leaves nothing under the repository. When a provider
    file is configured, the per-run agent dir is materialized inside that
    temp dir so the judgment reaches the same provider/model a delivery
    would; without one, Pi keeps its own agent dir. The answer is the
    session's stdout, parsed by `orbi.clarify.parse_verdict` — a model
    error or an unusable answer is the caller's fail-open path.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="orbi-clarify-") as directory:
        clarify_dir = Path(directory)
        session_dir = clarify_dir / ".pi-session"
        command, log_command = build_pi_command(
            config, ROLE_TICKET, IMPLEMENT_EXCLUDED_SKILLS, session_dir,
            system_prompt, context,
            context_placeholder="<issue-context-redacted>",
            tools=False, extensions=False,
        )
        _log_provider_config_loaded(
            issue_ref=issue_context(source_repo, int(issue["number"])),
            role=ROLE_TICKET, config=config,
            elapsed=time.monotonic() - started,
        )
        agent_dir = prepare_pi_agent_dir(clarify_dir, config, role=ROLE_TICKET)
        pi_env = _pi_extension_env(config)
        if agent_dir is not None:
            pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        return stream_pi(
            command, cwd=clarify_dir,
            ctx=RunContext(
                run_id=run_id, issue=int(issue["number"]),
                branch="-", worktree=Path("-"), source_repo=source_repo,
            ),
            role=ROLE_TICKET,
            log_command=log_command,
            progress=progress,
            pi_env=pi_env or None,
            watch=PiWatchOptions(
                model_wait_dead_seconds=config.model_wait_dead_seconds,
                model_wait_probe_url=config.model_wait_probe_url,
                model_wait_probe_seconds=config.model_wait_probe_seconds,
            ),
        )


def run_ticket_agent(issue: dict, config: config_domain.RunnerConfig, source_repo: str,
                     *, progress: Callable[[dict], None] | None = None) -> str:
    """Generate one ticket-only deliverable without using Git state (#209)."""
    started = time.monotonic()
    system_prompt = (
        "You are a ticket-only content agent. Produce the requested final "
        "content as your complete stdout response. Do not create or modify "
        "files, branches, commits, pull requests, tests, or use git/gh tools."
    )
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        "Return only the final content to post on this Issue."
    )
    # Pi's session is transient OS state, not a task worktree or repository
    # artifact. Its output and all terminal evidence are kept on the Issue.
    with tempfile.TemporaryDirectory(prefix="orbi-ticket-") as directory:
        ticket_dir = Path(directory)
        session_dir = ticket_dir / ".pi-session"
        command, log_command = build_pi_command(
            config, ROLE_TICKET, IMPLEMENT_EXCLUDED_SKILLS, session_dir,
            system_prompt, context,
            context_placeholder="<issue-context-redacted>",
            tools=False, extensions=False,
        )
        # Startup phase: the ticket-only session keeps Pi's
        # own agent dir (no per-run materialization) — the provider
        # config is still loaded and resolved before the spawn.
        _log_provider_config_loaded(
            issue_ref=issue_context(source_repo, int(issue["number"])),
            role=ROLE_TICKET, config=config,
            elapsed=time.monotonic() - started,
        )
        return stream_pi(
            command, cwd=ticket_dir,
            ctx=RunContext(
                run_id=config.run_id, issue=int(issue["number"]),
                branch="-", worktree=Path("-"), source_repo=source_repo,
            ),
            role=ROLE_TICKET,
            log_command=log_command,
            progress=progress,
        )
