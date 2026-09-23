"""The in-ticket `/milestone <version>` command (Issue #1290).

A closed active milestone with ``auto_next_milestone = false`` puts the
engine in an intentional wait, and the confirmation ticket asks a human to
advance it. A hosted tenant can neither run the CLI nor edit the host
config, so the ticket itself carries the command: a comment line
``/milestone vX.Y.Z`` runs the same deterministic three steps a maintainer
would — create the milestone, open its release ticket, land
``active_milestone``.

Deliberate limits: the command never advances anything by itself, never
infers the next version, and never calls a model. It only accepts a title
from the caller's candidate set.

The generic machinery — the anchored matching, the permission gate, the
receipts — lives in `orbi.ticket_command` (Issue #1294); this module is its
first registrant, `MILESTONE_COMMAND`. Everything milestone-specific (the
candidate-set rule, the three idempotent steps and the active_milestone
line rewrite) stays here: landing that value is the command's third step,
and the idle-time auto-advance shares the same write.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from orbi import config as config_domain
from orbi.delivery_labels import RELEASE_LABEL
from orbi.github import list_issues, list_milestones, run_gh_read_command
from orbi.journal import run_command
from orbi.release_git import RELEASE_VERSION_FILE_OPTIONS
from orbi.repo_config import RepoConfigError, RepoPolicy, read_repo_config
from orbi.ticket_command import (
    CommandSpec, TicketCommandError, register_command,
)


_ACTIVE_MILESTONE_LINE_RE = re.compile(
    r"(?m)^[ \t]*active_milestone[ \t]*=[ \t]*[^\r\n]+"
)


def rewrite_active_milestone_text(text: str, new_value: str) -> str:
    """Return ``text`` with only its active_milestone line replaced."""
    if not _ACTIVE_MILESTONE_LINE_RE.search(text):
        raise RuntimeError("active_milestone line not found")
    # The value is serialized, never interpolated: a Milestone title is
    # arbitrary text, and a raw f-string produced invalid TOML for `"`,
    # or a re replacement escape error for `\`. json.dumps emits a TOML-
    # compatible basic string; the lambda keeps the replacement text out
    # of the regex escape layer entirely.
    serialized = json.dumps(new_value, ensure_ascii=False)
    updated, _ = _ACTIVE_MILESTONE_LINE_RE.subn(
        lambda _match: f"active_milestone = {serialized}", text, count=1,
    )
    return updated


def rewrite_active_milestone_line(config_path: Path, new_value: str) -> None:
    """Replace only the configured active_milestone line, byte-for-byte."""
    text = config_path.read_bytes().decode("utf-8")
    try:
        updated = rewrite_active_milestone_text(text, new_value)
    except RuntimeError as exc:
        raise RuntimeError(
            f"active_milestone line not found in {config_path}"
        ) from exc
    config_path.write_bytes(updated.encode("utf-8"))


# --- the three idempotent steps -----------------------------------------

_RELEASE_TICKET_TEMPLATE_PATH = ".github/release-ticket-template.md"
_RELEASE_TICKET_DROP_SECTIONS = (
    "## Background",
    "## Not included in this release",
)
# The maintainers-only reference is the template's trailing section; a
# prefix match is used (and drops to end of file) because its body carries
# fenced ```markdown alternates that themselves start with `## Release`.
_RELEASE_TICKET_REFERENCE_PREFIX = "## Release section reference"
_RELEASE_TICKET_VERSION_PLACEHOLDER = "vX.Y.Z"

_STEP_MILESTONE = "milestone"
_STEP_RELEASE_TICKET = "release ticket"
_STEP_ACTIVE_MILESTONE = "active_milestone"


def _strip_release_sections(text: str) -> str:
    """Drop the judgement and maintainers-only sections of the template."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith(_RELEASE_TICKET_REFERENCE_PREFIX):
            break
        if skipping:
            if not stripped.startswith("## "):
                continue
            skipping = False
        if stripped in _RELEASE_TICKET_DROP_SECTIONS:
            skipping = True
            continue
        kept.append(line)
    return "\n".join(kept)


def _set_release_version_file(text: str, version_file: str) -> str:
    """Write a definite `version_file` into the machine-readable block."""
    lines = text.splitlines()
    heading = next(
        (index for index, line in enumerate(lines)
         if line.rstrip() == "## Release"), None,
    )
    field = f"- version_file: {version_file}"
    if heading is None:
        return text.rstrip("\n") + "\n" + field + "\n"
    end = len(lines)
    for index in range(heading + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    existing = next(
        (index for index in range(heading + 1, end)
         if lines[index].startswith("- version_file:")), None,
    )
    if existing is not None:
        lines[existing] = field
        return "\n".join(lines) + "\n"
    last_field = next(
        (index for index in range(end - 1, heading, -1)
         if lines[index].startswith("- ")), None,
    )
    lines.insert(last_field + 1 if last_field is not None else heading + 1, field)
    return "\n".join(lines) + "\n"


def _builtin_release_ticket_template(
    version: str, base_branch: str, version_file: str,
) -> str:
    """The minimal field set used when the repository carries no template."""
    return (
        f"> This repository has no `{_RELEASE_TICKET_TEMPLATE_PATH}`; this "
        "release ticket was generated from the built-in minimal field set.\n\n"
        "## Preconditions\n\n"
        f"- All milestone {version} Issues except this ticket are closed\n"
        "- CI is green for the release commit\n\n"
        "**This ticket carries `ai-ready`**: it must be claimable as soon as "
        "it is opened.\n\n"
        "## Acceptance\n\n"
        f"- The remote contains tag `{version}` and the corresponding GitHub "
        "Release\n"
        "- Release notes include the ticket numbers in this release's scope\n"
        "- Every ticket in scope has state `ai-merged`\n\n"
        "## Release\n\n"
        f"- version: {version}\n"
        f"- base_branch: {base_branch}\n"
        f"- scope_from_milestone: {version}\n"
        f"- version_file: {version_file}\n"
    )


def render_release_ticket(
    template_text: str | None, *, version: str, base_branch: str,
    version_file: str,
) -> str:
    """Render the release ticket body from the repository template.

    The template is the single source of truth: the body is always rendered
    from it, never hard-coded. The two judgement sections (`## Background`,
    `## Not included in this release`) and the maintainers-only reference
    section are dropped; every `vX.Y.Z` becomes the real version and a
    definite `version_file` is written into the `## Release` block.
    """
    if template_text is None:
        return _builtin_release_ticket_template(version, base_branch, version_file)
    body = _strip_release_sections(template_text)
    body = body.replace(_RELEASE_TICKET_VERSION_PLACEHOLDER, version)
    body = body.strip("\n") + "\n"
    return _set_release_version_file(body, version_file)


def _read_release_ticket_template(repo: str, base_branch: str) -> str | None:
    endpoint = (
        f"repos/{repo}/contents/{_RELEASE_TICKET_TEMPLATE_PATH}"
        f"?ref={quote(base_branch, safe='')}"
    )
    try:
        raw = run_gh_read_command(
            ["gh", "api", endpoint], timeout=30,
            failure_log_level=logging.DEBUG,
        )
        data = json.loads(raw)
    except Exception:
        return None
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str):
        return None
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _detect_version_file(repo: str, base_branch: str) -> str | None:
    """Detect the repository's version file from the base-branch tree."""
    try:
        raw = run_gh_read_command([
            "gh", "api",
            f"repos/{repo}/git/trees/{quote(base_branch, safe='')}",
        ], timeout=30, failure_log_level=logging.DEBUG)
        data = json.loads(raw)
    except Exception:
        return None
    tree = data.get("tree") if isinstance(data, dict) else None
    if not isinstance(tree, list):
        return None
    names = {
        entry.get("path") for entry in tree
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    for candidate in RELEASE_VERSION_FILE_OPTIONS:
        if candidate != "none" and candidate in names:
            return candidate
    return None


def _ensure_command_milestone(repo: str, version: str) -> None:
    milestones = list_milestones(repo, timeout=30)
    if any(
        isinstance(item, dict) and item.get("title") == version
        for item in milestones
    ):
        return
    # Documented route: POST /repos/{owner}/{repo}/milestones ("Create a
    # milestone"); `title` is the only required field and a new milestone is
    # created open.
    run_command([
        "gh", "api", "--method", "POST", f"repos/{repo}/milestones",
        "-f", f"title={version}",
    ], timeout=30)


def _ensure_command_release_ticket(
    repo: str, version: str, *, base_branch: str, dispatch_label: str,
    version_file: str | None,
) -> None:
    existing = list_issues(
        repo, state="all",
        search=f'label:{RELEASE_LABEL} milestone:"{version}"',
        json_fields="number", limit=200, timeout=30,
    )
    if existing:
        return
    resolved = version_file or _detect_version_file(repo, base_branch)
    if resolved is None:
        # No configured value and no supported file in the base tree: the
        # release state machine would read a file that does not exist and
        # the armed ticket would end `release_failed` -> `ai-blocked`. Fail
        # here, before reading the template or creating anything, with the
        # repair the maintainer owes. Never guess `none` either: a version
        # stored in an unlisted file would then be tagged without being
        # bumped (Issue #1307).
        raise RuntimeError(
            "version_file_unresolved; fix=set `version_file` in "
            "`.github/orbi.toml` to the file that carries the version, or "
            'to "none" for a tag-only release'
        )
    template = _read_release_ticket_template(repo, base_branch)
    body = render_release_ticket(
        template, version=version, base_branch=base_branch,
        version_file=resolved,
    )
    # `--milestone <name>` attaches the ticket to the version's Milestone;
    # without it the release gate would never find its own scope.
    run_command([
        "gh", "issue", "create", "--repo", repo,
        "--title", f"release {version}",
        "--body", body,
        "--label", RELEASE_LABEL,
        "--label", dispatch_label,
        "--milestone", version,
    ], timeout=30)


def _write_policy_active_milestone(
    repo: str, policy_path: str, version: str,
) -> None:
    """Land `active_milestone` in the repository policy file.

    The blob is read AND committed on the repository's DEFAULT branch: the
    engine reads the policy from that branch tip (``read_repo_config`` sends
    no ref), so the sha resolved by the read above belongs to the branch the
    PUT must update. Naming the delivery base branch here would pin a sha
    from another branch whenever the two differ — a 409, or an update on a
    branch the engine never reads the policy back from.
    """
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/contents/{policy_path}",
    ], timeout=30)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{policy_path}: contents API returned unreadable JSON"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise RuntimeError(f"{policy_path}: contents API returned no file content")
    sha = data.get("sha")
    if not isinstance(sha, str) or not sha:
        raise RuntimeError(f"{policy_path}: contents API returned no blob sha")
    text = base64.b64decode(data["content"]).decode("utf-8")
    updated = rewrite_active_milestone_text(text, version)
    # Documented route: PUT /repos/{owner}/{repo}/contents/{path} ("Create or
    # update file contents"); `message` and `content` are required, `sha`
    # pins the blob that was just read, and omitting `branch` updates that
    # same blob's branch — the API default branch the read resolved.
    run_command([
        "gh", "api", "--method", "PUT",
        f"repos/{repo}/contents/{policy_path}",
        "-f", f"message=chore: set active_milestone to {version}",
        "-f", f"content={base64.b64encode(updated.encode('utf-8')).decode('ascii')}",
        "-f", f"sha={sha}",
    ], timeout=30)


def _land_active_milestone(
    repo: str, version: str, *, policy: RepoPolicy | None, policy_path: str,
    config_path: Path,
) -> str:
    """Land `active_milestone` where the current value actually comes from.

    Returns a readable name of the target written — the repository policy
    when it declares the key (it overrides the host config per key),
    otherwise the host config line — so a reporting caller can name it
    (Issue #1306).
    """
    if policy is not None and policy.active_milestone is not None:
        _write_policy_active_milestone(repo, policy_path, version)
        return f"repo policy {policy_path}"
    rewrite_active_milestone_line(config_path, version)
    return f"host config {config_path}"


class MilestoneSetError(Exception):
    """The `milestone set` fail-fast error: one structured line (reason + fix)."""


def milestone_set(
    config: config_domain.RunnerConfig, config_path: Path, title: str,
) -> tuple[str, str, str]:
    """Advance `active_milestone` to one exact Milestone title (Issue #895).

    The manual advance behind the `auto_next_milestone = false`
    confirmation flow. The claim scope the Runner reads is set the same
    way it is resolved: the title must exist as exactly ONE Milestone
    on the source repo (GitHub Milestone titles are not unique, so a
    duplicate exact title is a hard error, never a guess), then the new
    value is landed where the CURRENT value comes from (Issue #1306):
    the repository policy `.github/orbi.toml` when it declares
    `active_milestone` (the per-key override the Runner reads), else the
    host config `active_milestone` line. In the rewrite only the
    `active_milestone` line changes — comments, blank lines and every
    other field stay byte-identical. A CLOSED target is refused while
    `auto_next_milestone` is true (Issue #933): the idle path would
    advance a closed active_milestone to the newest open one on the next
    tick, so success here would contradict the real behavior; the repair
    is to reopen the milestone or set `auto_next_milestone = false`
    first. The variable sync is NOT part of
    this command: the Runner's next tick publishes
    `ORBI_ACTIVE_MILESTONE` (the bypass contract). Returns
    (old, new, target); every failure raises MilestoneSetError with the
    written target untouched.
    """
    repo = config.source_repos[0]
    policy_path = config_domain.repository_config_path(config, repo)
    try:
        policy = read_repo_config(
            repo, path=policy_path, run_command=run_command,
        )
    except RepoConfigError as exc:
        raise MilestoneSetError(
            f"milestone_set_failed reason=repository policy invalid: {exc}; "
            f"fix=repair {policy_path} on the default branch"
        ) from exc
    declared = policy.active_milestone if policy is not None else None
    current = declared if declared is not None else config.active_milestone
    if current is None:
        raise MilestoneSetError(
            f"milestone_set_failed reason=no active_milestone in "
            f"{config_path} or {policy_path}; fix=add "
            '`active_milestone = "<current>"` to the config first '
            "(the field is never created implicitly)"
        )
    try:
        milestones = list_milestones(repo, timeout=30)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # The docstring promises one structured line for EVERY failure:
        # a hung gh raises TimeoutExpired, a missing gh raises OSError,
        # a malformed payload raises ValueError — same collapse.
        detail = (getattr(exc, "stderr", "") or "").strip() or str(exc)
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone lookup failed: {detail}; "
            "fix=check `gh auth status` and Milestone read access to "
            f"{repo}"
        ) from exc
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict) and milestone.get("title") == title
    ]
    if not matches:
        open_list = ", ".join(
            f"{milestone.get('title')}({milestone.get('open_issues')})"
            for milestone in milestones
            if isinstance(milestone, dict) and milestone.get("state") == "open"
        ) or "(none)"
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone_not_found title={title!r} "
            f"repo={repo} open milestones: {open_list}; fix=use one exact "
            "title from `gh api "
            f"repos/{repo}/milestones?state=open --jq '.[].title'`"
        )
    if len(matches) > 1:
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone_ambiguous title={title!r} "
            f"repo={repo}: the exact title matches {len(matches)} "
            "Milestones; fix=rename or close the duplicate Milestone first"
        )
    if config.auto_next_milestone and matches[0].get("state") == "closed":
        # A closed active_milestone is exactly what the next idle tick's
        # auto-advance rewrites (Issue #933): reporting success here would
        # promise a claim scope that is undone one tick later.
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone_closed title={title!r} "
            f"repo={repo} state=closed; auto_next_milestone is true, so the "
            "next idle tick would advance active_milestone away from it; "
            "fix=reopen the milestone or set auto_next_milestone = false"
        )
    try:
        target = _land_active_milestone(
            repo, title, policy=policy, policy_path=policy_path,
            config_path=config_path,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError,
            ValueError) as exc:
        # The landing is the command's only write: a failed policy PUT
        # or a bad payload collapses into the same structured line as
        # an unwritable host config, and the target stays untouched.
        detail = (getattr(exc, "stderr", "") or "").strip() or str(exc)
        if declared is not None:
            fix = f"repair the repository policy at {policy_path}"
        else:
            fix = f"repair the config file at {config_path}"
        raise MilestoneSetError(
            f"milestone_set_failed reason={detail}; fix={fix}"
        ) from exc
    return current, title, target


def apply_milestone_command(
    repo: str, version: str, *, config_path: Path, policy: RepoPolicy | None,
    policy_path: str, base_branch: str, dispatch_label: str,
    version_file: str | None,
) -> list[str]:
    """Run the three idempotent steps; the first failure names its step."""
    steps = (
        (_STEP_MILESTONE, lambda: _ensure_command_milestone(repo, version)),
        (_STEP_RELEASE_TICKET, lambda: _ensure_command_release_ticket(
            repo, version, base_branch=base_branch,
            dispatch_label=dispatch_label, version_file=version_file,
        )),
        (_STEP_ACTIVE_MILESTONE, lambda: _land_active_milestone(
            repo, version, policy=policy, policy_path=policy_path,
            config_path=config_path,
        )),
    )
    completed: list[str] = []
    for step, action in steps:
        try:
            action()
        except Exception as exc:  # reported as one readable step receipt
            raise TicketCommandError(step, str(exc)) from exc
        completed.append(step)
    return completed


def _milestone_validate(
    version: str, *, candidate_titles: list[str], **_context: object,
) -> str | None:
    """The command's own business rule: the title must be a candidate."""
    if version not in set(candidate_titles):
        return (
            f"`{version}` is not an open milestone above the current one; "
            f"candidates: {', '.join(candidate_titles) or '(none)'}"
        )
    return None


def _milestone_apply(
    version: str, *, repo: str, config_path: Path, policy: RepoPolicy | None,
    policy_path: str, base_branch: str, dispatch_label: str,
    version_file: str | None, candidate_titles: list[str],
    issue_number: int, **_context: object,
) -> None:
    """Run the three idempotent steps from the shared dispatcher's context."""
    apply_milestone_command(
        repo, version, config_path=config_path, policy=policy,
        policy_path=policy_path, base_branch=base_branch,
        dispatch_label=dispatch_label, version_file=version_file,
    )


# `/milestone` is the first registrant of the shared in-ticket command layer
# (`orbi.ticket_command`). Registration validates itself: the shared layer
# owns the anchored matcher, the permission gate and the receipts, so this
# module supplies only the verbs, the declarative permission, and the two
# callbacks that carry the milestone's business.
MILESTONE_COMMAND = CommandSpec(
    name="milestone",
    verbs=("milestone",),
    permission="write",
    validate=_milestone_validate,
    apply=_milestone_apply,
    usage="/milestone <version>",
    description="advance the active Milestone from a confirmation Issue",
    argument_label="version",
)

register_command(MILESTONE_COMMAND)
