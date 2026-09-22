#!/usr/bin/env python3
"""Pure construction of the Pi agent command line (Issue #1231).

One module owns the agent's command-line grammar: the session-role
constants, the role's skill selection, the provider/model flags and the
argv layout every Orbi session role shares. `runner` builds its ticket,
implementer and review commands from a single call to
:func:`build_pi_command`, which returns the real `command` and the
redacted `log_command` together, so the journal can no longer misreport
what was launched.

The module is pure — no I/O, no subprocess, no import of `orbi.runner` —
so it needs no fake or patch to test. `RunnerConfig` is imported under
`TYPE_CHECKING` only, the same annotation-only rule `orbi.pi_process`
follows. `ROLE_IMPLEMENT` lives in `orbi.pi_process` and is imported
here; this leaf module only moves down the constants `runner` still
consumes.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from orbi.pi_process import ROLE_IMPLEMENT

if TYPE_CHECKING:
    from orbi.config import RunnerConfig


# Non-implement Pi session roles. `ROLE_IMPLEMENT` is the default role of
# a delivery Pi session and lives in `orbi.pi_process`.
ROLE_REVIEW = "review"
ROLE_TICKET = "ticket"


# Role-specific skill filtering: the review session ends with a single
# REVIEW_VERDICT line and its job is to review this one diff and fix it
# until it can merge — not to open another full delivery — so the
# delivery-oriented skills must not be loaded there (tdd-dev would steer
# it into the implement/test/PR flow, review-fix-loop would open another
# fix/review round). The implementer keeps tdd-dev and code-review but
# not review-fix-loop: the Runner itself runs the independent review
# loop once the PR is open.
REVIEW_EXCLUDED_SKILLS = frozenset({"tdd-dev", "review-fix-loop"})
IMPLEMENT_EXCLUDED_SKILLS = frozenset({"review-fix-loop"})


def _pi_extension_args(config: RunnerConfig) -> list[str]:
    """Return the isolated extension flags for every Pi role."""
    args = ["--no-extensions"]
    for extension in config.pi_extensions:
        if extension["enabled"]:
            args.extend(("--extension", extension["source"]))
    return args


def _pi_model_args(config: RunnerConfig, role: str = ROLE_IMPLEMENT) -> list[str]:
    """Return the configured Pi model flags.

    One `--flag value` pair per configured key, in the fixed order
    provider, model, thinking; an unset key contributes nothing, so a
    config without any of the three keys returns [] and the Pi command
    keeps its exact pre-#119 shape. The review role reads its own
    `review_pi_*` override per key and falls back to the implementer's
    value when the override is unset. The values are non-sensitive model
    identifiers (never keys or tokens) and are part of the redacted
    `log_command`, so the journal run scene records what was launched.
    """
    args: list[str] = []
    prefix = "review_" if role == ROLE_REVIEW else ""
    for flag, key in (
        ("--provider", "pi_provider"),
        ("--model", "pi_model"),
        ("--thinking", "pi_thinking"),
    ):
        value = getattr(config, f"{prefix}{key}")
        if prefix and value is None:
            value = getattr(config, key)
        if value is not None:
            args.extend((flag, value))
    return args


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


def _skills_for(config: RunnerConfig, excluded: frozenset[str]) -> list[str | Path]:
    """Return one role's configured skills, dropping excluded names."""
    return [
        skill for skill in config.skills
        if _skill_name(skill) not in excluded
    ]


def _skill_args(skills: list[str | Path]) -> list[str]:
    """Return the --skill command args for one role's skill list."""
    return [
        item for skill in skills
        for item in ("--skill", str(skill))
    ]


def build_pi_command(
    config: RunnerConfig,
    role: str,
    excluded_skills: frozenset[str],
    session_dir: str | Path,
    system_prompt: str,
    context: str,
    *,
    context_placeholder: str,
    tools: bool,
    extensions: bool,
) -> tuple[list[str], list[str]]:
    """Return one Pi session's `(command, log_command)` argv pair.

    `command` is the real argv in the fixed order every role has always
    used: the optional `--no-tools` boundary, the isolated extension
    flags, the role's `--skill` entries, the provider/model flags, then
    `--print --session-dir <dir> --system-prompt <prompt> <context>`.
    `tools=False` adds `--no-tools`; `extensions=False` omits the
    extension flags.

    `log_command` is derived here from the SAME extension flags, model
    flags and session dir as `command`, so the two cannot drift. The
    system prompt and context are replaced by `<redacted>` and the
    caller's context placeholder; skill paths and `--no-tools` are never
    logged (matching the frozen pre-#1231 journal shape).
    """
    extension_args = _pi_extension_args(config) if extensions else []
    skill_args = _skill_args(_skills_for(config, excluded_skills))
    model_args = _pi_model_args(config, role)
    tools_args = [] if tools else ["--no-tools"]
    command = [
        "pi", *tools_args, *extension_args, *skill_args, *model_args,
        "--print", "--session-dir", str(session_dir),
        "--system-prompt", system_prompt, context,
    ]
    log_command = [
        "pi", *extension_args, *model_args,
        "--print", "--session-dir", str(session_dir),
        "--system-prompt", "<redacted>", context_placeholder,
    ]
    return command, log_command
