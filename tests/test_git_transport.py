"""Git transport contract tests (Issue #114).

Git data operations (fetch, push — including `.github/workflows/*.yml`)
go over SSH (`git@github.com:owner/repo.git`); GitHub API operations
stay on the `gh` token. The deployment checkout's single `origin`
remote is the transport: task worktrees created with `git worktree
add` share it (verified against real git). An existing HTTPS remote is
never rewritten silently — only the human-run setup entry migrates it
(`migrate=True`); every other path fails fast with the exact
migration command. A failed SSH probe (`git ls-remote`, verified
against the real CLI: exit 0 = reachable + authenticated) fails fast
with the structured reason — no HTTPS fallback, no silent skip.
"""
import subprocess
from pathlib import Path

import pytest

import git_transport


# --- ssh_url_for -------------------------------------------------------------


def test_ssh_url_for_builds_the_github_ssh_url():
    assert git_transport.ssh_url_for("xqliu/muyan-pilot") == (
        "git@github.com:xqliu/muyan-pilot.git"
    )


def test_ssh_url_for_rejects_a_repo_without_a_slash():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("muyan-pilot")


def test_ssh_url_for_rejects_an_empty_segment():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("/muyan-pilot")
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("xqliu/")


def test_ssh_url_for_rejects_an_extra_slash():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("xqliu/muyan/pilot")


# --- remote_protocol ---------------------------------------------------------


def test_remote_protocol_classifies_ssh_scp_style():
    assert git_transport.remote_protocol(
        "git@github.com:xqliu/muyan-pilot.git"
    ) == "ssh"


def test_remote_protocol_classifies_ssh_scheme():
    assert git_transport.remote_protocol(
        "ssh://git@github.com/xqliu/muyan-pilot.git"
    ) == "ssh"


def test_remote_protocol_classifies_https():
    assert git_transport.remote_protocol(
        "https://github.com/xqliu/muyan-pilot.git"
    ) == "https"


def test_remote_protocol_classifies_http():
    assert git_transport.remote_protocol(
        "http://github.com/xqliu/muyan-pilot.git"
    ) == "http"


def test_remote_protocol_classifies_everything_else_other():
    assert git_transport.remote_protocol("origin") == "other"
    assert git_transport.remote_protocol(
        "/home/u/repos/muyan-pilot"
    ) == "other"


# --- check_transport ----------------------------------------------------------


def ok_run_factory(state: dict):
    """A run_command double driven by `state` (counters + canned output)."""
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "config"]:
            if state.get("no_origin"):
                raise subprocess.CalledProcessError(
                    1, command, stderr="",
                )
            return state.get("origin_url",
                             "git@github.com:xqliu/muyan-pilot.git")
        if command[:2] == ["git", "ls-remote"]:
            if state.get("ssh_down"):
                raise subprocess.CalledProcessError(
                    128, command,
                    stderr="git@github.com: Permission denied (publickey).",
                )
            return "abc123\tHEAD"
        if command[:3] == ["git", "remote", "set-url"]:
            state["origin_url"] = command[4]
            state.setdefault("migrations", []).append(command[4])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    return fake_run, calls, state


def test_check_transport_passes_for_a_matching_ssh_remote(tmp_path):
    state = {}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert result == {
        "remote": "origin",
        "protocol": "ssh",
        "url": "git@github.com:xqliu/muyan-pilot.git",
        "expected": "git@github.com:xqliu/muyan-pilot.git",
        "migrated": False,
        "ssh_reachable": True,
    }
    # Read-only: no set-url, one probe of the exact SSH URL.
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [
        "git", "ls-remote", "git@github.com:xqliu/muyan-pilot.git",
    ] in calls


def test_check_transport_accepts_an_ssh_url_without_the_dot_git_suffix(
    tmp_path,
):
    state = {"origin_url": "git@github.com:xqliu/muyan-pilot"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert result["protocol"] == "ssh"
    assert result["ssh_reachable"] is True
    # The probe uses the normalized .git form.
    assert [
        "git", "ls-remote", "git@github.com:xqliu/muyan-pilot.git",
    ] in calls


def test_check_transport_fails_fast_on_a_missing_origin(tmp_path):
    state = {"no_origin": True}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="no origin remote",
    ):
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    # No probe, no migration after the missing remote.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_diagnoses_an_https_remote_without_migrating(
    tmp_path,
):
    state = {
        "origin_url": "https://github.com/xqliu/muyan-pilot.git",
    }
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(git_transport.TransportError, match="HTTPS") as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    message = str(exc.value)
    # The failure carries the exact migration command and the setup
    # entry that performs it — never a silent rewrite, never a remote
    # read from a comment or Issue.
    assert "git remote set-url origin git@github.com:xqliu/muyan-pilot.git" in message
    assert "muyan_pilot.py setup" in message
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    # No HTTPS fallback: no ls-remote of the HTTPS URL either.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_migrates_an_https_remote_when_authorized(tmp_path):
    state = {"origin_url": "https://github.com/xqliu/muyan-pilot.git"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot"],
        run_command=fake_run, migrate=True,
    )
    assert result["protocol"] == "ssh"
    assert result["migrated"] is True
    assert result["url"] == "git@github.com:xqliu/muyan-pilot.git"
    assert result["ssh_reachable"] is True
    assert state["migrations"] == [
        "git@github.com:xqliu/muyan-pilot.git",
    ]


def test_check_transport_fails_fast_on_an_ssh_repo_mismatch(tmp_path):
    state = {"origin_url": "git@github.com:other/repo.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="mismatch",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git@github.com:other/repo.git" in message
    assert "git@github.com:xqliu/muyan-pilot.git" in message
    # A mismatching remote is not probed and not migrated.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []


def test_check_transport_fails_fast_on_an_unsupported_protocol(tmp_path):
    state = {"origin_url": "http://github.com/xqliu/muyan-pilot.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(git_transport.TransportError, match="http"):
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )


def test_check_transport_fails_fast_when_ssh_is_unreachable(tmp_path):
    state = {"ssh_down": True}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    message = str(exc.value)
    # The structured scene: the exact probe command and the git stderr.
    assert "git ls-remote git@github.com:xqliu/muyan-pilot.git" in message
    assert "Permission denied (publickey)" in message
    # No HTTPS fallback: the probe is never retried over HTTPS.
    probes = [c for c in calls if c[:2] == ["git", "ls-remote"]]
    assert len(probes) == 1
    assert probes[0][2].startswith("git@github.com:")


def test_check_transport_skips_the_probe_when_disabled(tmp_path):
    state = {}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot"],
        run_command=fake_run, probe=False,
    )
    assert result["ssh_reachable"] is None
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_fails_fast_on_a_malformed_source_repo(tmp_path):
    fake_run, calls, _ = ok_run_factory({})
    with pytest.raises(
        git_transport.TransportError, match="malformed",
    ):
        git_transport.check_transport(
            tmp_path, ["not-a-repo"], run_command=fake_run,
        )
    assert calls == []


def test_check_transport_uses_the_first_configured_source_repo(tmp_path):
    state = {}
    fake_run, calls, _ = ok_run_factory(state)
    git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot", "xqliu/muyan-ceo"],
        run_command=fake_run,
    )
    assert [
        "git", "ls-remote", "git@github.com:xqliu/muyan-pilot.git",
    ] in calls


def test_check_transport_accepts_an_ssh_scheme_url(tmp_path):
    """Both SSH forms are the same transport: the `ssh://` scheme URL
    matches the expected repo and is probed in the normalized SCP form."""
    state = {
        "origin_url": "ssh://git@github.com/xqliu/muyan-pilot.git",
    }
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert result["protocol"] == "ssh"
    assert result["ssh_reachable"] is True
    assert [
        "git", "ls-remote", "git@github.com:xqliu/muyan-pilot.git",
    ] in calls


def test_check_transport_fails_fast_on_a_generic_config_error(tmp_path):
    """A non-git failure reading the configured URL (e.g. a spawn
    error) still fails fast as a missing/unknown origin — never a
    guessed transport."""
    def fake_run(command, **kwargs):
        raise OSError("spawn failed")

    with pytest.raises(
        git_transport.TransportError, match="no origin remote",
    ):
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )


def test_check_transport_reports_a_probe_failure_without_stderr(tmp_path):
    """A probe failure without captured stderr (e.g. the error went to
    the terminal) still carries the exact probe command — the scene is
    never incomplete."""
    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
        raise subprocess.CalledProcessError(128, command)

    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git ls-remote git@github.com:xqliu/muyan-pilot.git" in message
    assert "stderr=" not in message


def test_check_transport_fails_fast_on_a_generic_probe_error(tmp_path):
    """A non-git failure of the SSH probe (e.g. a spawn error) fails
    fast as ssh_unreachable — no HTTPS fallback."""
    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
        raise OSError("spawn failed")

    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    assert "spawn failed" in str(exc.value)


def test_ok_run_factory_rejects_an_unexpected_command():
    fake_run, _, _ = ok_run_factory({})
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["definitely", "not", "a", "known", "command"])


def test_ssh_repo_path_returns_none_for_a_non_ssh_url():
    """The defensive branch: a URL that is neither the SCP form nor
    the `ssh://` scheme has no repo path (check_transport only calls
    this for SSH URLs, so the branch is unit-tested directly)."""
    assert git_transport._ssh_repo_path(
        "https://github.com/xqliu/muyan-pilot.git"
    ) is None
    assert git_transport._ssh_repo_path(
        "git@github.com:xqliu/muyan-pilot"
    ) == "xqliu/muyan-pilot"
    assert git_transport._ssh_repo_path(
        "ssh://git@github.com/xqliu/muyan-pilot.git"
    ) == "xqliu/muyan-pilot"


# --- real git: the worktree inherits the checkout's transport -----------------


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_real_worktree_inherits_the_checkout_origin_remote(tmp_path):
    """A `git worktree add` worktree shares the deployment checkout's
    single `origin` remote (verified against real git): configuring the
    transport once on the checkout makes every task worktree's
    `git remote -v` carry it."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    git(clone, "commit", "--allow-empty", "-m", "first")
    # The checkout's origin is a local path (the stand-in for the
    # remote); the transport is configured by rewriting it.
    ssh_style = "git@github.com:xqliu/muyan-pilot.git"
    git(clone, "remote", "set-url", "origin", ssh_style)
    assert git(clone, "remote", "get-url", "origin") == ssh_style
    worktree = tmp_path / "wt"
    git(clone, "worktree", "add", "-b", "task", str(worktree), "HEAD")
    # The worktree's `git remote -v` shows the checkout's transport.
    assert git(worktree, "remote", "get-url", "origin") == ssh_style
    remote_v = git(worktree, "remote", "-v")
    assert f"origin\t{ssh_style} (fetch)" in remote_v
    assert f"origin\t{ssh_style} (push)" in remote_v
    git(clone, "worktree", "remove", "--force", str(worktree))


def test_real_workflow_file_push_goes_over_the_ssh_transport(tmp_path):
    """The Issue #106 scenario: pushing a `.github/workflows/*.yml`
    file from the task worktree. The worktree's `origin` is the SSH
    URL (a `url.<base>.insteadOf` rewrite keeps the data plane local,
    the same mechanism the e2e world uses): the push of the workflow
    file succeeds over the configured SSH transport — it never depends
    on the OAuth App `workflow` scope (the HTTPS/OAuth transport that
    blocked Issue #106)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    git(clone, "commit", "--allow-empty", "-m", "first")
    # The transport: SSH URL on the remote, local data plane (the
    # e2e mechanism, git-config(1) `url.<base>.insteadOf`).
    ssh_style = "git@github.com:xqliu/muyan-pilot.git"
    git(clone, "remote", "set-url", "origin", ssh_style)
    git(clone, "config", f"url.{origin}.insteadOf", ssh_style)
    worktree = tmp_path / "wt"
    git(clone, "worktree", "add", "-b", "task", str(worktree), "HEAD")
    # The delivery: a GitHub workflow file committed in the worktree.
    workflows = worktree / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\njobs:\n  tests:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "feat: workflow file (Issue #106 scene)")
    # The push of the workflow file goes over the worktree's origin
    # (the checkout's single SSH remote).
    git(worktree, "push", "origin", "HEAD:refs/heads/task")
    # The workflow file is on the remote branch.
    refs = git(origin, "ls-tree", "-r", "refs/heads/task")
    assert ".github/workflows/ci.yml" in refs
    git(clone, "worktree", "remove", "--force", str(worktree))


def test_real_set_url_migration_rewrites_only_the_origin_remote(tmp_path):
    """The migration command the check reports (and setup runs) is a
    plain `git remote set-url origin <ssh-url>`: it rewrites the fetch
    URL of `origin` only and leaves the worktree-visible transport
    consistent."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    git(clone, "commit", "--allow-empty", "-m", "first")
    https_style = "https://github.com/xqliu/muyan-pilot.git"
    git(clone, "remote", "set-url", "origin", https_style)
    assert git(clone, "remote", "get-url", "origin") == https_style
    assert git_transport.remote_protocol(
        git(clone, "remote", "get-url", "origin")
    ) == "https"
    # The migration (the exact command the failure message reports).
    git(clone, "remote", "set-url", "origin",
        "git@github.com:xqliu/muyan-pilot.git")
    assert git(clone, "remote", "get-url", "origin") == (
        "git@github.com:xqliu/muyan-pilot.git"
    )
    assert git_transport.remote_protocol(
        git(clone, "remote", "get-url", "origin")
    ) == "ssh"
