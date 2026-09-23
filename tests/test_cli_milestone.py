"""`orbi milestone set` — the explicit manual active_milestone
advance (Issue #895).

One command moves the single source repo's `active_milestone` to an exact
GitHub Milestone title — no manual config editing, no second state, no
guessing. The value lands where the Runner actually reads it from (Issue
#1306): the repository policy `.github/orbi.toml` when it declares
`active_milestone` (per-key override), otherwise the host `orbi.toml`.

- the title must exist as exactly ONE Milestone (GitHub Milestone
  titles are NOT unique, so a duplicate exact title is a hard error);
- ONLY the `active_milestone` line is rewritten — comments, blank
  lines and every other field stay byte-identical
  (`rewrite_active_milestone_line`);
- every failure (missing target, duplicate title, GitHub API /
  permission failure, config without the field, missing config,
  unwritable config, a failed policy write) exits non-zero with one
  structured `milestone_set_failed reason=... fix=...` line on stderr
  and leaves the untouched target byte-identical;
- the command does NOT sync the `ORBI_ACTIVE_MILESTONE` Actions
  variable itself: the Runner's preflight syncs it on the next tick
  (the bypass contract), and the success output says so.

The command writes to GitHub only when the repository policy declares
`active_milestone` (the contents PUT); the network calls are stubbed at
the one subprocess seam with the in-memory `FakeGh` (Article 5.2: the
command line is the contract at the adapter seam, and the fake owns its
dispatch).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from orbi import cli
from orbi import pilot_setup

from seam import seam
from tests.fakes.github import FakeGh

REPO = "octocat/hello-world"


def make_world(
    tmp_path: Path, *, milestone: str | None = "v0.5.0",
    auto_next_milestone: bool | None = None,
) -> Path:
    """A minimal valid deployment layout; returns the config path.

    The same shape `orbi check`'s world uses: an explicit deploy home
    (prompts + provider starter + env file) and a `run/orbi.toml`
    carrying the source repo — plus the `active_milestone` line this
    command advances (omitted for the no-field failure path).
    """
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "prompt.md").write_text("prompt\n", encoding="utf-8")
    (home / "prompts" / "prompt_review.md").write_text(
        "review\n", encoding="utf-8")
    (home / "pi-providers.json").write_text(
        json.dumps(pilot_setup.PROVIDER_STARTER), encoding="utf-8")
    (home / ".orbi").mkdir()
    (home / ".orbi" / "env").write_text(
        "PROVIDER_API_KEY=milestone-test-key\n", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config_path = run_dir / "orbi.toml"
    lines = [
        f'source_repos = ["{REPO}"]',
        f'repo_dir = "{tmp_path / "repo"}"',
        f'deploy_home = "{home}"',
        'pi_provider = "openai"',
        'pi_model = "your-model"',
        f'pi_providers = "{home / "pi-providers.json"}"',
    ]
    if milestone is not None:
        lines.append(f'active_milestone = "{milestone}"')
    if auto_next_milestone is not None:
        lines.append(
            "auto_next_milestone = "
            + ("true" if auto_next_milestone else "false")
        )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def wire(monkeypatch, gh: FakeGh) -> list:
    """Patch the ONE subprocess seam with a call-recording FakeGh."""
    calls: list[list[str]] = []

    def recording(command, **kwargs):
        calls.append(list(command))
        return gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", recording)
    return calls


def run_set(config_path: Path, title: str) -> int:
    return cli.main([
        "milestone", "set", title, "--config", str(config_path),
    ])


POLICY_WITH_MILESTONE = (
    'base_branch = "main"\nactive_milestone = "v0.5.0"\n'
)
POLICY_WITHOUT_MILESTONE = 'base_branch = "main"\n'


def _call_text(command: list[str]) -> str:
    return " ".join(command)


def _policy_reads(calls: list[list[str]]) -> list[list[str]]:
    return [
        command for command in calls
        if "/contents/" in _call_text(command) and "--method" not in command
    ]


def _policy_puts(calls: list[list[str]]) -> list[list[str]]:
    return [command for command in calls if "--method" in command]


def _milestone_reads(calls: list[list[str]]) -> list[list[str]]:
    return [command for command in calls if "/milestones?" in _call_text(command)]


# --- the success path -----------------------------------------------------------


def test_milestone_set_success_rewrites_only_that_line(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    original = config_path.read_text(encoding="utf-8")
    assert 'active_milestone = "v0.5.0"' in original
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.5.0", state="closed")
    gh.add_milestone(2, title="v0.6.0", open_issues=3)
    calls = wire(monkeypatch, gh)

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 0
    updated = config_path.read_text(encoding="utf-8")
    # The ONLY change is the one line, in place: every other byte
    # (the other fields, their order, the trailing newline) identical.
    assert updated == original.replace(
        'active_milestone = "v0.5.0"', 'active_milestone = "v0.6.0"',
    )
    out = capsys.readouterr().out
    assert "active_milestone: v0.5.0 -> v0.6.0" in out
    assert f"repo: {REPO}" in out
    # The output names the target actually written (Issue #1306).
    assert f"wrote: host config {config_path}" in out
    # The variable is NOT synced by this command: the next Runner tick
    # does it (the bypass contract) and the output makes that checkable.
    assert "ORBI_ACTIVE_MILESTONE" in out
    assert "next tick" in out
    # No repository policy: the host config is the only place written
    # (exactly one read-only Milestone list, plus the policy probe).
    assert len(_policy_reads(calls)) == 1
    assert len(_policy_puts(calls)) == 0
    assert len(_milestone_reads(calls)) == 1


def test_milestone_set_lands_in_the_repository_policy_when_declared(
    tmp_path, monkeypatch, capsys,
):
    """The repository policy overrides the host config: the value must
    land there (Issue #1306). The host config stays byte-identical and
    the contents PUT commits the rewritten policy blob."""
    config_path = make_world(tmp_path)
    original = config_path.read_bytes()
    gh = FakeGh(REPO)
    gh.set_repo_config(POLICY_WITH_MILESTONE)
    gh.add_milestone(1, title="v0.5.0", state="closed")
    gh.add_milestone(2, title="v0.6.0", open_issues=3)
    calls = wire(monkeypatch, gh)

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 0
    # The repository policy got the new value ...
    assert gh.repo_config == POLICY_WITH_MILESTONE.replace(
        'active_milestone = "v0.5.0"', 'active_milestone = "v0.6.0"',
    )
    # ... and the host config is untouched, byte for byte.
    assert config_path.read_bytes() == original
    puts = _policy_puts(calls)
    assert len(puts) == 1
    put = puts[0]
    assert put[:2] == ["gh", "api"]
    assert put[2:4] == ["--method", "PUT"]
    assert put[4] == f"repos/{REPO}/contents/.github/orbi.toml"
    assert any(item.startswith("sha=") for item in put)  # the blob read is pinned
    assert not any(item.startswith("branch=") for item in put)
    out = capsys.readouterr().out
    assert "active_milestone: v0.5.0 -> v0.6.0" in out
    assert "wrote: repo policy .github/orbi.toml" in out


def test_milestone_set_policy_without_the_key_writes_the_host_config(
    tmp_path, monkeypatch, capsys,
):
    """A policy file that does NOT declare `active_milestone` leaves the
    host config as the effective source (regression guard)."""
    config_path = make_world(tmp_path)
    original = config_path.read_text(encoding="utf-8")
    gh = FakeGh(REPO)
    gh.set_repo_config(POLICY_WITHOUT_MILESTONE)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    calls = wire(monkeypatch, gh)

    assert run_set(config_path, "v0.6.0") == 0

    assert config_path.read_text(encoding="utf-8") == original.replace(
        'active_milestone = "v0.5.0"', 'active_milestone = "v0.6.0"',
    )
    assert gh.repo_config == POLICY_WITHOUT_MILESTONE
    assert _policy_puts(calls) == []
    assert f"wrote: host config {config_path}" in capsys.readouterr().out


def test_milestone_set_policy_declares_without_host_line_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """When the policy declares the value, a host config without the
    line is not a failure: the policy is the effective source."""
    config_path = make_world(tmp_path, milestone=None)
    original = config_path.read_bytes()
    gh = FakeGh(REPO)
    gh.set_repo_config(POLICY_WITH_MILESTONE)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    assert run_set(config_path, "v0.6.0") == 0

    assert gh.repo_config == POLICY_WITH_MILESTONE.replace(
        'active_milestone = "v0.5.0"', 'active_milestone = "v0.6.0"',
    )
    assert config_path.read_bytes() == original
    assert "active_milestone: v0.5.0 -> v0.6.0" in capsys.readouterr().out


def test_milestone_set_preserves_comments_and_blank_lines(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n# operator notes stay\nmax_concurrency = 2\n\n",
        )
    original = config_path.read_text(encoding="utf-8")
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    assert run_set(config_path, "v0.6.0") == 0

    updated = config_path.read_text(encoding="utf-8")
    assert "# operator notes stay" in updated
    assert "max_concurrency = 2" in updated
    assert updated == original.replace(
        'active_milestone = "v0.5.0"', 'active_milestone = "v0.6.0"',
    )


# --- auto_next_milestone vs. a closed target (Issue #933) -----------------------


def test_milestone_set_closed_target_with_auto_next_milestone_true_is_refused(
    tmp_path, monkeypatch, capsys,
):
    """A closed target with `auto_next_milestone = true` (the default) is
    refused: the next idle tick would advance active_milestone away from
    it, so the command's success output would contradict the real
    behavior. One structured line, config byte-identical (Issue #933)."""
    config_path = make_world(tmp_path)
    original = config_path.read_bytes()
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.5.8", open_issues=2)
    gh.add_milestone(2, title="v0.5.7", state="closed")
    calls = wire(monkeypatch, gh)

    exit_code = run_set(config_path, "v0.5.7")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "reason=milestone_closed" in err
    assert "v0.5.7" in err and REPO in err
    assert "fix=" in err
    assert "auto_next_milestone = false" in err
    assert "Traceback" not in err
    assert config_path.read_bytes() == original
    # Refused before any write: one read-only Milestone list, no policy PUT.
    assert len(_milestone_reads(calls)) == 1
    assert _policy_puts(calls) == []


def test_milestone_set_open_target_with_auto_next_milestone_true_still_lands(
    tmp_path, monkeypatch, capsys,
):
    """Regression (Issue #933): an OPEN target is not touched by the idle
    advance, so `auto_next_milestone = true` must not block it."""
    config_path = make_world(tmp_path)
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.5.4", state="closed")
    gh.add_milestone(2, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    assert run_set(config_path, "v0.6.0") == 0

    assert 'active_milestone = "v0.6.0"' in config_path.read_text(
        encoding="utf-8")
    assert "active_milestone: v0.5.0 -> v0.6.0" in capsys.readouterr().out


def test_milestone_set_closed_target_allowed_with_auto_next_milestone_false(
    tmp_path, monkeypatch, capsys,
):
    """`auto_next_milestone = false` is the documented manual-advance flow:
    a closed target stays allowed and is written (Issue #933)."""
    config_path = make_world(tmp_path, auto_next_milestone=False)
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.5.8", open_issues=2)
    gh.add_milestone(2, title="v0.5.7", state="closed")
    wire(monkeypatch, gh)

    assert run_set(config_path, "v0.5.7") == 0

    assert 'active_milestone = "v0.5.7"' in config_path.read_text(
        encoding="utf-8")
    assert "active_milestone: v0.5.0 -> v0.5.7" in capsys.readouterr().out


# --- the failure paths (the config file must never change) ----------------------


def test_milestone_set_missing_target_fails_without_touching_config(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    original = config_path.read_text(encoding="utf-8")
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.5.0", state="closed")
    gh.add_milestone(2, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    exit_code = run_set(config_path, "v0.9.9")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "reason=" in err and "v0.9.9" in err
    assert "milestone_not_found" in err
    assert "fix=" in err
    assert "v0.6.0(3)" in err  # the open list is the repair input
    assert "Traceback" not in err
    assert config_path.read_text(encoding="utf-8") == original


def test_milestone_set_invalid_repository_policy_fails_fast(
    tmp_path, monkeypatch, capsys,
):
    """A repository policy the Runner reads but cannot validate is a hard
    failure naming the policy — never a silent fallback to the host config
    (Issue #1306)."""
    config_path = make_world(tmp_path)
    original = config_path.read_bytes()
    gh = FakeGh(REPO)
    gh.set_repo_config("nope = 1\n")
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    assert run_set(config_path, "v0.6.0") == 1

    err = capsys.readouterr().err
    assert err.startswith("milestone_set_failed")
    assert "repository policy invalid" in err
    assert "fix=" in err
    assert "Traceback" not in err
    assert config_path.read_bytes() == original


def test_milestone_set_policy_write_failure_is_structured(
    tmp_path, monkeypatch, capsys,
):
    """A failed policy PUT is a structured failure: non-zero exit, one
    `milestone_set_failed reason=...` line, no success, and the host
    config untouched (Issue #1306)."""
    config_path = make_world(tmp_path)
    original = config_path.read_bytes()
    gh = FakeGh(REPO)
    gh.set_repo_config(POLICY_WITH_MILESTONE)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    calls: list[list[str]] = []

    def recording(command, **kwargs):
        calls.append(list(command))
        if "--method" in command:
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="gh: HTTP 403",
            )
        return gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", recording)

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "reason=" in err
    assert "fix=" in err
    assert "Traceback" not in err
    assert config_path.read_bytes() == original
    # The PUT was attempted and only the policy write path ran.
    assert len(_policy_puts(calls)) == 1


def test_milestone_set_duplicate_exact_title_fails_without_touching_config(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    original = config_path.read_text(encoding="utf-8")
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    gh.add_milestone(2, title="v0.6.0", state="closed")
    wire(monkeypatch, gh)

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "milestone_ambiguous" in err and "v0.6.0" in err
    assert "fix=" in err
    assert "Traceback" not in err
    assert config_path.read_text(encoding="utf-8") == original


def test_milestone_set_gh_api_failure_fails_fast_without_touching_config(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    original = config_path.read_text(encoding="utf-8")
    # A fake bound to ANOTHER repo makes the read fail fast (the fake's
    # repository-mismatch failure — non-transient, so no read retry).
    calls = wire(monkeypatch, FakeGh("other/repo"))

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "milestone lookup failed" in err
    assert "fix=" in err and "gh auth" in err
    assert "Traceback" not in err
    assert config_path.read_text(encoding="utf-8") == original
    # Exactly one milestone-list attempt: the failure is not transient,
    # the read-retry loop must not spin (the policy probe is separate).
    assert len(_milestone_reads(calls)) == 1


def test_milestone_set_without_active_milestone_line_fails_without_touching(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path, milestone=None)
    original = config_path.read_text(encoding="utf-8")
    calls = wire(monkeypatch, FakeGh(REPO))

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "active_milestone" in err and "fix=" in err
    assert "Traceback" not in err
    assert config_path.read_text(encoding="utf-8") == original
    # The only network call is the policy probe that confirms neither
    # source declares the field; the Milestone list is never read.
    assert len(_policy_reads(calls)) == 1
    assert _milestone_reads(calls) == []


def test_milestone_set_missing_config_fails(
    tmp_path, monkeypatch, capsys,
):
    wire(monkeypatch, FakeGh(REPO))

    exit_code = run_set(tmp_path / "absent.toml", "v0.6.0")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root writes read-only files",
)
def test_milestone_set_unwritable_config_fails_without_touching_content(
    tmp_path, monkeypatch, capsys,
):
    config_path = make_world(tmp_path)
    original = config_path.read_bytes()
    config_path.chmod(0o444)
    gh = FakeGh(REPO)
    gh.add_milestone(1, title="v0.6.0", open_issues=3)
    wire(monkeypatch, gh)

    try:
        exit_code = run_set(config_path, "v0.6.0")
    finally:
        config_path.chmod(0o644)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("milestone_set_failed")
    assert "fix=" in err
    assert "Traceback" not in err
    assert config_path.read_bytes() == original


# --- titles that only survive a real escaping implementation ---------------------


def test_milestone_set_title_with_double_quote_writes_valid_toml(
    tmp_path, monkeypatch,
):
    """GitHub Milestone titles are arbitrary; a title containing `"` must
    still produce a parseable TOML basic string. The old implementation
    interpolated the raw title and wrote `active_milestone = "say "hi""`
    — invalid TOML on disk while the command reported success, so the
    next tick died in load_config."""
    import tomllib

    config_path = make_world(tmp_path)
    gh = FakeGh(REPO)
    gh.add_milestone(1, title='say "hi"', open_issues=1)
    wire(monkeypatch, gh)

    assert run_set(config_path, 'say "hi"') == 0

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["active_milestone"] == 'say "hi"'


def test_milestone_set_title_with_backslash_writes_valid_toml(
    tmp_path, monkeypatch,
):
    """A `\\` in the title used to travel into the re.subn replacement
    string as an escape sequence and blow up with re.error — an
    exception type outside the command's failure contract (a raw
    traceback instead of one structured milestone_set_failed line)."""
    import tomllib

    config_path = make_world(tmp_path)
    gh = FakeGh(REPO)
    gh.add_milestone(1, title=r"back\slash", open_issues=1)
    wire(monkeypatch, gh)

    assert run_set(config_path, r"back\slash") == 0

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["active_milestone"] == r"back\slash"


def test_milestone_set_timeout_failure_raises_the_structured_error(
    tmp_path, monkeypatch, capsys,
):
    """The failure contract (docstring: every failure raises
    MilestoneSetError) covers more than CalledProcessError: a hung gh
    times out (TimeoutExpired), a missing gh raises OSError, a bad
    payload raises ValueError — all must collapse into the one
    structured line instead of leaking a traceback."""
    import subprocess

    config_path = make_world(tmp_path)

    def hung(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(seam, "run_command", hung)

    exit_code = run_set(config_path, "v0.6.0")

    assert exit_code != 0
    err = capsys.readouterr().err
    assert "milestone_set_failed" in err
    assert "milestone lookup failed" in err
