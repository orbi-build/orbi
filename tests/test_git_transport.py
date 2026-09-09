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

from orbi import git_transport


# --- ssh_url_for -------------------------------------------------------------


def test_ssh_url_for_builds_the_github_ssh_url():
    assert git_transport.ssh_url_for("xqliu/orbi") == (
        "git@github.com:xqliu/orbi.git"
    )


def test_ssh_url_for_rejects_a_repo_without_a_slash():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("orbi")


def test_ssh_url_for_rejects_an_empty_segment():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("/orbi")
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("xqliu/")


def test_ssh_url_for_rejects_an_extra_slash():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.ssh_url_for("xqliu/orbi/pilot")


# --- https_url_for (Issue #580) -----------------------------------------------


def test_https_url_for_builds_the_github_https_url():
    assert git_transport.https_url_for("xqliu/orbi") == (
        "https://github.com/xqliu/orbi.git"
    )


def test_https_url_for_rejects_a_malformed_repo():
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.https_url_for("orbi")
    with pytest.raises(git_transport.TransportError, match="malformed"):
        git_transport.https_url_for("xqliu/orbi/pilot")


# --- remote_protocol ---------------------------------------------------------


def test_remote_protocol_classifies_ssh_scp_style():
    assert git_transport.remote_protocol(
        "git@github.com:xqliu/orbi.git"
    ) == "ssh"


def test_remote_protocol_classifies_ssh_scheme():
    assert git_transport.remote_protocol(
        "ssh://git@github.com/xqliu/orbi.git"
    ) == "ssh"


def test_remote_protocol_classifies_https():
    assert git_transport.remote_protocol(
        "https://github.com/xqliu/orbi.git"
    ) == "https"


def test_remote_protocol_classifies_http():
    assert git_transport.remote_protocol(
        "http://github.com/xqliu/orbi.git"
    ) == "http"


def test_remote_protocol_classifies_everything_else_other():
    assert git_transport.remote_protocol("origin") == "other"
    assert git_transport.remote_protocol(
        "/home/u/repos/orbi"
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
                             "git@github.com:xqliu/orbi.git")
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
        tmp_path, ["xqliu/orbi"], run_command=fake_run,
    )
    assert result == {
        "remote": "origin",
        "protocol": "ssh",
        "url": "git@github.com:xqliu/orbi.git",
        "expected": "git@github.com:xqliu/orbi.git",
        "migrated": False,
        "ssh_reachable": True,
        "transport_reachable": True,
    }
    # Read-only: no set-url, one probe of the exact SSH URL.
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [
        "git", "ls-remote", "git@github.com:xqliu/orbi.git",
    ] in calls


def test_check_transport_accepts_an_ssh_url_without_the_dot_git_suffix(
    tmp_path,
):
    state = {"origin_url": "git@github.com:xqliu/orbi"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"], run_command=fake_run,
    )
    assert result["protocol"] == "ssh"
    assert result["ssh_reachable"] is True
    # The probe uses the normalized .git form.
    assert [
        "git", "ls-remote", "git@github.com:xqliu/orbi.git",
    ] in calls


def test_check_transport_fails_fast_on_a_missing_origin(tmp_path):
    state = {"no_origin": True}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="no origin remote",
    ):
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    # No probe, no migration after the missing remote.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_diagnoses_an_https_remote_without_migrating(
    tmp_path,
):
    state = {
        "origin_url": "https://github.com/xqliu/orbi.git",
    }
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(git_transport.TransportError, match="HTTPS") as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    message = str(exc.value)
    # The failure carries the exact migration command and the setup
    # entry that performs it — never a silent rewrite, never a remote
    # read from a comment or Issue.
    assert "git remote set-url origin git@github.com:xqliu/orbi.git" in message
    assert "orbi setup" in message
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    # No HTTPS fallback: no ls-remote of the HTTPS URL either.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_migrates_an_https_remote_when_authorized(tmp_path):
    state = {"origin_url": "https://github.com/xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"],
        run_command=fake_run, migrate=True,
    )
    assert result["protocol"] == "ssh"
    assert result["migrated"] is True
    assert result["url"] == "git@github.com:xqliu/orbi.git"
    assert result["ssh_reachable"] is True
    assert state["migrations"] == [
        "git@github.com:xqliu/orbi.git",
    ]


def test_check_transport_fails_fast_on_an_ssh_repo_mismatch(tmp_path):
    state = {"origin_url": "git@github.com:other/repo.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="mismatch",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git@github.com:other/repo.git" in message
    assert "git@github.com:xqliu/orbi.git" in message
    # A mismatching remote is not probed and not migrated.
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []


def test_check_transport_fails_fast_on_an_unsupported_protocol(tmp_path):
    state = {"origin_url": "http://github.com/xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(git_transport.TransportError, match="http"):
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )


def test_check_transport_fails_fast_when_ssh_is_unreachable(tmp_path):
    state = {"ssh_down": True}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    message = str(exc.value)
    # The structured scene: the exact probe command and the git stderr.
    assert "git ls-remote git@github.com:xqliu/orbi.git" in message
    assert "Permission denied (publickey)" in message
    # No HTTPS fallback: the probe is never retried over HTTPS.
    probes = [c for c in calls if c[:2] == ["git", "ls-remote"]]
    assert len(probes) == 1
    assert probes[0][2].startswith("git@github.com:")


def test_check_transport_skips_the_probe_when_disabled(tmp_path):
    state = {}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"],
        run_command=fake_run, probe=False,
    )
    assert result["ssh_reachable"] is None
    assert result["transport_reachable"] is None
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
        tmp_path, ["xqliu/orbi", "xqliu/orbi-backlog"],
        run_command=fake_run,
    )
    assert [
        "git", "ls-remote", "git@github.com:xqliu/orbi.git",
    ] in calls


def test_check_transport_accepts_an_ssh_scheme_url(tmp_path):
    """Both SSH forms are the same transport: the `ssh://` scheme URL
    matches the expected repo and is probed in the normalized SCP form."""
    state = {
        "origin_url": "ssh://git@github.com/xqliu/orbi.git",
    }
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"], run_command=fake_run,
    )
    assert result["protocol"] == "ssh"
    assert result["ssh_reachable"] is True
    assert [
        "git", "ls-remote", "git@github.com:xqliu/orbi.git",
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
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )


def test_check_transport_reports_a_probe_failure_without_stderr(tmp_path):
    """A probe failure without captured stderr (e.g. the error went to
    the terminal) still carries the exact probe command — the scene is
    never incomplete."""
    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        raise subprocess.CalledProcessError(128, command)

    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git ls-remote git@github.com:xqliu/orbi.git" in message
    assert "stderr=" not in message


def test_check_transport_fails_fast_on_a_generic_probe_error(tmp_path):
    """A non-git failure of the SSH probe (e.g. a spawn error) fails
    fast as ssh_unreachable — no HTTPS fallback."""
    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        raise OSError("spawn failed")

    with pytest.raises(
        git_transport.TransportError, match="ssh_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"], run_command=fake_run,
        )
    assert "spawn failed" in str(exc.value)


def test_ok_run_factory_rejects_an_unexpected_command():
    fake_run, _, _ = ok_run_factory({})
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["definitely", "not", "a", "known", "command"])


def test_remote_repo_path_extracts_the_repo_from_every_github_form():
    """All four GitHub URL forms (SCP SSH, `ssh://` scheme, https,
    http) yield the `owner/name` path; the optional `.git` suffix is
    stripped."""
    assert git_transport._remote_repo_path(
        "git@github.com:xqliu/orbi"
    ) == "xqliu/orbi"
    assert git_transport._remote_repo_path(
        "ssh://git@github.com/xqliu/orbi.git"
    ) == "xqliu/orbi"
    assert git_transport._remote_repo_path(
        "https://github.com/xqliu/orbi.git"
    ) == "xqliu/orbi"
    assert git_transport._remote_repo_path(
        "http://github.com/xqliu/orbi"
    ) == "xqliu/orbi"


def test_remote_repo_path_returns_none_for_a_non_github_url():
    """A URL on any other host (or a local path) has no GitHub repo
    path: such a remote can never be migrated (a rewrite would
    re-target the checkout at a guessed destination)."""
    assert git_transport._remote_repo_path(
        "git@gitlab.com:other/repo.git"
    ) is None
    assert git_transport._remote_repo_path(
        "/home/u/repos/orbi"
    ) is None


def test_check_transport_never_migrates_a_remote_pointing_at_a_different_repo(
    tmp_path,
):
    """Issue #114: the migration rewrites the checkout's single
    `origin` remote — pointing it at a DIFFERENT repository would
    re-target the whole checkout (subsequent fetches/pulls hit the
    wrong repo). A remote that does not point at the first configured
    source repo therefore fails with the mismatch scene even when
    `migrate` is authorized (the human-run setup entry): no
    `set-url`, no probe, the remote is left untouched."""
    state = {
        "origin_url": "https://github.com/other/repo.git",
    }
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="mismatch",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, migrate=True,
        )
    message = str(exc.value)
    assert "https://github.com/other/repo.git" in message
    assert "git@github.com:xqliu/orbi.git" in message
    # No rewrite of the mismatching remote, no probe.
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []
    assert state["origin_url"] == "https://github.com/other/repo.git"


def test_check_transport_never_migrates_a_non_github_remote(tmp_path):
    """A remote on a non-GitHub host (or a local path) has no
    recognizable repo: it fails with the mismatch scene (repo=None)
    even when `migrate` is authorized — never a guessed rewrite."""
    state = {"origin_url": "git@gitlab.com:other/repo.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="mismatch",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, migrate=True,
        )
    assert "repo=None" in str(exc.value)
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []


# --- https mode (Issue #580): origin stays HTTPS, gh credential helper --------


def test_check_transport_https_passes_for_a_matching_https_remote(tmp_path):
    """Issue #580: in https mode the HTTPS origin IS the transport —
    no SSH requirement, no rewrite; the probe is
    `git ls-remote <https-url>` (credentials via the gh credential
    helper)."""
    state = {"origin_url": "https://github.com/xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"],
        run_command=fake_run, mode="https",
    )
    assert result == {
        "remote": "origin",
        "protocol": "https",
        "url": "https://github.com/xqliu/orbi.git",
        "expected": "https://github.com/xqliu/orbi.git",
        "migrated": False,
        "ssh_reachable": None,
        "transport_reachable": True,
    }
    # No migration, no SSH anywhere: the single probe targets the
    # HTTPS URL.
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [
        "git", "ls-remote", "https://github.com/xqliu/orbi.git",
    ] in calls
    assert [c for c in calls if c[2].startswith("git@github.com:")] == []


def test_check_transport_https_migrates_an_ssh_remote_when_authorized(
    tmp_path,
):
    """Issue #580: setup (the human-run entry) migrates symmetrically —
    in https mode an SSH `origin` is rewritten to the HTTPS URL of the
    first configured source repo."""
    state = {"origin_url": "git@github.com:xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"],
        run_command=fake_run, migrate=True, mode="https",
    )
    assert result["protocol"] == "https"
    assert result["migrated"] is True
    assert result["url"] == "https://github.com/xqliu/orbi.git"
    assert result["transport_reachable"] is True
    assert result["ssh_reachable"] is None
    assert state["migrations"] == ["https://github.com/xqliu/orbi.git"]
    # The probe runs against the HTTPS URL (after the migration).
    assert [
        "git", "ls-remote", "https://github.com/xqliu/orbi.git",
    ] in calls


def test_check_transport_https_fails_fast_on_an_ssh_remote_without_migrating(
    tmp_path,
):
    """Issue #580: outside setup an SSH origin is never rewritten — the
    failure carries the exact HTTPS migration command and the setup
    entry, and nothing is probed."""
    state = {"origin_url": "git@github.com:xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(git_transport.TransportError, match="SSH") as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, mode="https",
        )
    message = str(exc.value)
    assert (
        "git remote set-url origin https://github.com/xqliu/orbi.git"
    ) in message
    assert "orbi setup" in message
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_https_fails_fast_when_the_probe_fails(tmp_path):
    """Issue #580: a failed HTTPS probe (bad/absent gh token) fails
    with the structured `transport_unreachable` reason — the exact
    probe command and git stderr, never a fallback probe."""
    state = {
        "origin_url": "https://github.com/xqliu/orbi.git",
        "ssh_down": True,
    }
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="transport_unreachable",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, mode="https",
        )
    message = str(exc.value)
    assert "git ls-remote https://github.com/xqliu/orbi.git" in message
    assert "Permission denied (publickey)" in message
    # The credential fix hint: the transport authenticates via the gh
    # credential helper.
    assert "gh auth" in message
    # Exactly one probe, of the HTTPS URL — no SSH fallback.
    probes = [c for c in calls if c[:2] == ["git", "ls-remote"]]
    assert probes == [
        ["git", "ls-remote", "https://github.com/xqliu/orbi.git"],
    ]


def test_check_transport_https_skips_the_probe_when_disabled(tmp_path):
    state = {"origin_url": "https://github.com/xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    result = git_transport.check_transport(
        tmp_path, ["xqliu/orbi"],
        run_command=fake_run, probe=False, mode="https",
    )
    assert result["transport_reachable"] is None
    assert result["ssh_reachable"] is None
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_https_still_fails_fast_on_a_repo_mismatch(tmp_path):
    """The repo gate is transport-independent: an https remote pointing
    at a DIFFERENT repo is never probed nor migrated, and the expected
    URL in the message is the configured transport's."""
    state = {"origin_url": "https://github.com/other/repo.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="mismatch",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, mode="https",
        )
    message = str(exc.value)
    assert "https://github.com/other/repo.git" in message
    assert "https://github.com/xqliu/orbi.git" in message
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []
    assert [c for c in calls if c[:2] == ["git", "ls-remote"]] == []


def test_check_transport_https_rejects_an_http_remote(tmp_path):
    state = {"origin_url": "http://github.com/xqliu/orbi.git"}
    fake_run, calls, _ = ok_run_factory(state)
    with pytest.raises(
        git_transport.TransportError, match="git_transport",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, mode="https",
        )
    assert "https://github.com/xqliu/orbi.git" in str(exc.value)


def test_check_transport_rejects_an_unknown_mode(tmp_path):
    fake_run, calls, _ = ok_run_factory({})
    with pytest.raises(
        git_transport.TransportError, match="git_transport",
    ) as exc:
        git_transport.check_transport(
            tmp_path, ["xqliu/orbi"],
            run_command=fake_run, mode="gopher",
        )
    assert "'ssh' or 'https'" in str(exc.value)
    # Nothing was read, probed or rewritten.
    assert calls == []


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
    ssh_style = "git@github.com:xqliu/orbi.git"
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
    ssh_style = "git@github.com:xqliu/orbi.git"
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


def test_real_https_mode_checkout_and_worktree_delivery_without_ssh(tmp_path):
    """Issue #580 acceptance: the full https-mode delivery path against
    real git — the checkout's `origin` stays the HTTPS URL, the check
    passes in https mode (the probe of the HTTPS URL succeeds via the
    configured data plane), and a task worktree created from the
    checkout shares that origin and pushes over it. No SSH anywhere:
    the `url.<base>.insteadOf` rewrite keeps the data plane on a local
    bare repo (the repo's standard e2e mechanism — the credential
    helper's stand-in)."""
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
    # The transport: HTTPS URL on the remote, local data plane.
    https_style = "https://github.com/xqliu/orbi.git"
    git(clone, "remote", "set-url", "origin", https_style)
    git(clone, "config", f"url.{origin}.insteadOf", https_style)

    def run_command(command, cwd):
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, command, stderr=result.stderr,
            )
        return result.stdout.strip()

    # The helper fails fast on a nonzero exit (the same guard the
    # git() helper above is verified by).
    with pytest.raises(subprocess.CalledProcessError):
        run_command(["git", "rev-parse", "no-such-ref"], clone)

    result = git_transport.check_transport(
        clone, ["xqliu/orbi"], run_command=run_command, mode="https",
    )
    assert result["protocol"] == "https"
    assert result["migrated"] is False
    assert result["transport_reachable"] is True
    assert result["ssh_reachable"] is None
    # The task worktree inherits the single HTTPS remote and delivers
    # over it (the runner's fetch/push path). The CONFIGURED URL is
    # the transport (`remote get-url` would report the insteadOf
    # data-plane URL).
    worktree = tmp_path / "wt"
    git(clone, "worktree", "add", "-b", "task", str(worktree), "HEAD")
    assert git(worktree, "config", "remote.origin.url") == https_style
    (worktree / "delivery.txt").write_text("x", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "feat: https-mode delivery")
    git(worktree, "push", "origin", "HEAD:refs/heads/task")
    assert "task" in git(origin, "branch", "--list")
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
    https_style = "https://github.com/xqliu/orbi.git"
    git(clone, "remote", "set-url", "origin", https_style)
    assert git(clone, "remote", "get-url", "origin") == https_style
    assert git_transport.remote_protocol(
        git(clone, "remote", "get-url", "origin")
    ) == "https"
    # The migration (the exact command the failure message reports).
    git(clone, "remote", "set-url", "origin",
        "git@github.com:xqliu/orbi.git")
    assert git(clone, "remote", "get-url", "origin") == (
        "git@github.com:xqliu/orbi.git"
    )
    assert git_transport.remote_protocol(
        git(clone, "remote", "get-url", "origin")
    ) == "ssh"
