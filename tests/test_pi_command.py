"""Exact argv contract of the pure `orbi.pi_command` module (Issue #1231).

The module owns Pi's command-line grammar. These tests pin the exact
`command` and `log_command` lists for every session role, configured and
unconfigured, plus the review model fallback and the redaction rule that
neither the system prompt nor the context string may reach a log command.

The module is pure, so every case constructs a real `RunnerConfig` and
calls it — no fake, no monkeypatch.
"""
from pathlib import Path

import pytest

from orbi import config as config_domain
from orbi import pi_command
from orbi.pi_process import ROLE_IMPLEMENT


SESSION_DIR = Path("/tmp/session/.pi-session")
SYSTEM_PROMPT = "SYSTEM SECRET"
CONTEXT = "CONTEXT SECRET"


def _configured() -> config_domain.RunnerConfig:
    return config_domain.RunnerConfig(
        skills=("code-review", "review-fix-loop", "tdd-dev"),
        pi_extensions=(
            {"source": "npm:fixture@1.2.3", "enabled": True,
             "env": {"FIXTURE_TOKEN": "secret"}},
            {"source": "local.mjs", "enabled": False, "env": {}},
        ),
        pi_provider="openai",
        pi_model="gpt-5.6-sol",
        pi_thinking="medium",
        review_pi_provider="strong",
        review_pi_model="careful",
        review_pi_thinking="high",
    )


def _unconfigured() -> config_domain.RunnerConfig:
    return config_domain.RunnerConfig(
        skills=(),
        pi_extensions=(),
        pi_provider=None,
        pi_model=None,
        pi_thinking=None,
        review_pi_provider=None,
        review_pi_model=None,
        review_pi_thinking=None,
    )


EXTENSION_ARGS = ["--no-extensions", "--extension", "npm:fixture@1.2.3"]
IMPLEMENT_SKILL_ARGS = ["--skill", "code-review", "--skill", "tdd-dev"]
REVIEW_SKILL_ARGS = ["--skill", "code-review"]


def test_ticket_command_and_log_with_everything_configured():
    command, log_command = pi_command.build_pi_command(
        _configured(), pi_command.ROLE_TICKET,
        pi_command.IMPLEMENT_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<issue-context-redacted>",
        tools=False, extensions=True,
    )
    assert command == [
        "pi", "--no-tools", *EXTENSION_ARGS, *IMPLEMENT_SKILL_ARGS,
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", SYSTEM_PROMPT, CONTEXT,
    ]
    assert log_command == [
        "pi", *EXTENSION_ARGS,
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_implement_command_and_log_with_everything_configured():
    command, log_command = pi_command.build_pi_command(
        _configured(), ROLE_IMPLEMENT,
        pi_command.IMPLEMENT_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<issue-context-redacted>",
        tools=True, extensions=True,
    )
    assert command == [
        "pi", *EXTENSION_ARGS, *IMPLEMENT_SKILL_ARGS,
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", SYSTEM_PROMPT, CONTEXT,
    ]
    assert log_command == [
        "pi", *EXTENSION_ARGS,
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_review_command_and_log_with_everything_configured():
    command, log_command = pi_command.build_pi_command(
        _configured(), pi_command.ROLE_REVIEW,
        pi_command.REVIEW_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<review-context-redacted>",
        tools=True, extensions=True,
    )
    assert command == [
        "pi", *EXTENSION_ARGS, *REVIEW_SKILL_ARGS,
        "--provider", "strong", "--model", "careful", "--thinking", "high",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", SYSTEM_PROMPT, CONTEXT,
    ]
    assert log_command == [
        "pi", *EXTENSION_ARGS,
        "--provider", "strong", "--model", "careful", "--thinking", "high",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", "<review-context-redacted>",
    ]


@pytest.mark.parametrize(
    ("role", "excluded_skills", "tools", "extensions",
     "placeholder", "expect_no_tools"),
    [
        (pi_command.ROLE_TICKET, pi_command.IMPLEMENT_EXCLUDED_SKILLS,
         False, True, "<issue-context-redacted>", True),
        (ROLE_IMPLEMENT, pi_command.IMPLEMENT_EXCLUDED_SKILLS,
         True, True, "<issue-context-redacted>", False),
        (pi_command.ROLE_REVIEW, pi_command.REVIEW_EXCLUDED_SKILLS,
         True, True, "<review-context-redacted>", False),
    ],
)
def test_unconfigured_command_and_log(
    role, excluded_skills, tools, extensions, placeholder, expect_no_tools,
):
    command, log_command = pi_command.build_pi_command(
        _unconfigured(), role, excluded_skills, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder=placeholder, tools=tools, extensions=extensions,
    )
    assert command == [
        "pi",
        *(["--no-tools"] if expect_no_tools else []),
        *(["--no-extensions"] if extensions else []),
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", SYSTEM_PROMPT, CONTEXT,
    ]
    assert log_command == [
        "pi",
        *(["--no-extensions"] if extensions else []),
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", placeholder,
    ]


def test_review_model_args_fall_back_to_the_implementer_selection():
    config = config_domain.RunnerConfig(
        pi_provider="cheap", pi_model="fast", pi_thinking="low",
    )
    review = pi_command._pi_model_args(config, pi_command.ROLE_REVIEW)
    implement = pi_command._pi_model_args(config, ROLE_IMPLEMENT)
    assert review == implement == [
        "--provider", "cheap", "--model", "fast", "--thinking", "low",
    ]


def test_review_override_beats_the_implementer_selection():
    config = config_domain.RunnerConfig(
        pi_provider="cheap", pi_model="fast", pi_thinking="low",
        review_pi_provider="strong", review_pi_model="careful",
        review_pi_thinking="high",
    )
    assert pi_command._pi_model_args(config, pi_command.ROLE_REVIEW) == [
        "--provider", "strong", "--model", "careful", "--thinking", "high",
    ]


def test_partial_review_override_falls_back_per_key():
    config = config_domain.RunnerConfig(
        pi_provider="cheap", pi_model="fast", pi_thinking="low",
        review_pi_model="careful",
    )
    assert pi_command._pi_model_args(config, pi_command.ROLE_REVIEW) == [
        "--provider", "cheap", "--model", "careful", "--thinking", "low",
    ]


def test_log_command_never_carries_prompt_or_context():
    for role, excluded, placeholder in (
        (pi_command.ROLE_TICKET, pi_command.IMPLEMENT_EXCLUDED_SKILLS,
         "<issue-context-redacted>"),
        (ROLE_IMPLEMENT, pi_command.IMPLEMENT_EXCLUDED_SKILLS,
         "<issue-context-redacted>"),
        (pi_command.ROLE_REVIEW, pi_command.REVIEW_EXCLUDED_SKILLS,
         "<review-context-redacted>"),
    ):
        _command, log_command = pi_command.build_pi_command(
            _configured(), role, excluded, SESSION_DIR,
            SYSTEM_PROMPT, CONTEXT,
            context_placeholder=placeholder, tools=True, extensions=True,
        )
        rendered = " ".join(log_command)
        assert SYSTEM_PROMPT not in rendered
        assert CONTEXT not in rendered
        assert "secret" not in rendered


def test_extension_args_isolate_disabled_and_secrets():
    config = _configured()
    args = pi_command._pi_extension_args(config)
    assert args == EXTENSION_ARGS
    assert "secret" not in " ".join(args)


SUGGEST_SKILL = Path(
    "/deploy/integrations/claude-plugin/skills/"
    "suggest-ai-ready-issues/SKILL.md"
)
SUGGEST_COMMAND_ARGS = [
    "pi", "--tools", "read,grep,find,ls", "--no-skills",
    "--no-context-files", *EXTENSION_ARGS, "--skill", str(SUGGEST_SKILL),
    "--provider", "openai", "--model", "gpt-5.6-sol",
    "--thinking", "medium",
    "--print", "--session-dir", str(SESSION_DIR),
    "--system-prompt", SYSTEM_PROMPT, CONTEXT,
]


def test_suggest_command_uses_a_tool_allowlist_and_one_skill():
    """Issue #1576: the suggest role is the ONE builder with a tool
    allowlist (no bash/edit/write), no auto-discovered skills, no
    context files and exactly the suggest skill."""
    command, log_command = pi_command.build_pi_command(
        _configured(), pi_command.ROLE_SUGGEST,
        pi_command.IMPLEMENT_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<suggest-context-redacted>",
        tools="read,grep,find,ls", extensions=True,
        no_skills=True, no_context_files=True, skills=[SUGGEST_SKILL],
    )
    assert command == SUGGEST_COMMAND_ARGS
    for forbidden in ("bash", "edit", "write"):
        assert forbidden not in " ".join(command)
    assert log_command == [
        "pi", *EXTENSION_ARGS,
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", "<suggest-context-redacted>",
    ]
    rendered = " ".join(log_command)
    assert str(SUGGEST_SKILL) not in rendered
    assert "--tools" not in log_command
    assert "--no-skills" not in log_command
    assert "--no-context-files" not in log_command


UNDERSTANDING_COMMAND_ARGS = [
    "pi", "--tools", "read,ls,find", "--no-skills", "--no-context-files",
    "--no-extensions",
    "--provider", "openai", "--model", "gpt-5.6-sol",
    "--thinking", "medium",
    "--print", "--session-dir", str(SESSION_DIR),
    "--system-prompt", SYSTEM_PROMPT, CONTEXT,
]


def test_understanding_command_denies_extensions_without_loading_any():
    """Issue #1600: the parallel understanding session of `orbi suggest`
    carries `--no-extensions` and NONE of the configured `--extension`
    entries, and loads no skill."""
    command, log_command = pi_command.build_pi_command(
        _configured(), pi_command.ROLE_SUGGEST,
        pi_command.IMPLEMENT_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<suggest-understanding-redacted>",
        tools="read,ls,find", extensions=False, no_extensions=True,
        no_skills=True, no_context_files=True, skills=[],
    )
    assert command == UNDERSTANDING_COMMAND_ARGS
    assert "--extension" not in command
    assert "--skill" not in command
    assert log_command == [
        "pi", "--no-extensions",
        "--provider", "openai", "--model", "gpt-5.6-sol",
        "--thinking", "medium",
        "--print", "--session-dir", str(SESSION_DIR),
        "--system-prompt", "<redacted>", "<suggest-understanding-redacted>",
    ]


def test_extension_flags_are_omitted_when_extensions_is_false():
    """`extensions=False` without `no_extensions` keeps the pre-#1600
    behavior: no extension flag at all."""
    command, log_command = pi_command.build_pi_command(
        _configured(), ROLE_IMPLEMENT,
        pi_command.IMPLEMENT_EXCLUDED_SKILLS, SESSION_DIR,
        SYSTEM_PROMPT, CONTEXT,
        context_placeholder="<issue-context-redacted>",
        tools=True, extensions=False,
    )
    assert "--no-extensions" not in command
    assert "--extension" not in command
    assert "--no-extensions" not in log_command
