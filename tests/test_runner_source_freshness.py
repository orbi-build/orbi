"""Runner source freshness gate (Issue #525).

The 2026-09-07 incident: the editable install resolved to an OLD issue
worktree while the ExecStartPre preflight kept the deployment checkout
fresh — the Runner executed stale code while every delivery looked
up-to-date (fresh base_sha, stale engine). The startup invariant: before
any slot or claim, the checkout the RUNNING process imports `orbi` from
must be exactly the fetched ``origin/main`` ref (editable form),
or the installed distribution version must not be older than the latest
release tag reachable from that ref (non-editable form).

All git probes are LOCAL reads (``rev-parse`` / ``describe``): the
freshness of the remote-tracking ref is supplied by the ExecStartPre
fetch (worktrees share the deployment checkout's refs), so a fresh
checkout costs zero network requests.

The repro paths build REAL git repositories in tmp_path: a checkout
reset to an old commit while ``refs/remotes/origin/main`` points at a
newer one (the stale-deployment shape), and a linked worktree pinned to
the old commit (the exact incident scene).
"""
import subprocess
from pathlib import Path

import pytest

import orbi.cli_source as cli_source
import orbi.runner as runner

# The conftest autouse fixture stubs the gate for the in-process
# dispatch tests; the gate's OWN tests restore the real implementation
# (module import happens before any fixture runs, so this is the real
# function; this module's fixture runs after conftest's and wins).
_REAL_GATE = runner.check_runner_source_freshness


@pytest.fixture(autouse=True)
def _restore_real_gate(monkeypatch):
    monkeypatch.setattr(runner, "check_runner_source_freshness", _REAL_GATE)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def recording_run_command(commands: list[list[str]]):
    """A run_command that records argv and executes real local git."""
    def _run(command, *, cwd=None, timeout=None, **kwargs):
        commands.append([str(part) for part in command])
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            timeout=timeout or 30,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, command,
                output=result.stdout, stderr=result.stderr,
            )
        return result.stdout.strip()
    return _run


def build_stale_repo(tmp_path: Path, name: str = "deploy") -> tuple[Path, str, str]:
    """A git checkout whose HEAD sits one commit behind its own
    ``refs/remotes/origin/main`` (the stale deployment shape)."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "pilot@test.local")
    git(repo, "config", "user.name", "Pilot")
    (repo / "f.txt").write_text("old", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "old")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("new", encoding="utf-8")
    git(repo, "commit", "-am", "new")
    new = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", new)
    git(repo, "reset", "--hard", old)
    return repo, old, new


def point_module_file(monkeypatch, checkout: Path) -> None:
    """Resolve the running process's import source into `checkout`
    (the src-layout package path) — the editable-install seam."""
    pkg = checkout / "src" / "orbi"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_source, "module_file", lambda: pkg / "__init__.py")


def gate_config(deploy_home: Path, **extra) -> runner.RunnerConfig:
    return runner.RunnerConfig(
        **{"base_branch": "main", "deploy_home": deploy_home, **extra},
    )


# --- editable install form ----------------------------------------------------


@pytest.mark.parametrize(
    ("delivery_target", "delivery_base"),
    [("core", "main"), ("cloud", "main"), ("website", "beta")],
)
def test_fresh_editable_checkout_uses_engine_branch_for_all_delivery_targets(
    monkeypatch, tmp_path, caplog, delivery_target, delivery_base,
):
    """The engine freshness ref stays main even when a delivery target uses
    a different base branch (the website beta incident)."""
    repo, old, new = build_stale_repo(tmp_path, delivery_target)
    git(repo, "reset", "--hard", new)
    point_module_file(monkeypatch, repo)
    with caplog.at_level("INFO"):
        info = runner.check_runner_source_freshness(
            gate_config(repo, base_branch=delivery_base),
            run_command=recording_run_command([]),
        )
    assert info["engine_source_branch"] == "main"
    assert info["delivery_base_branch"] == delivery_base
    assert info["origin_main"] == new
    assert "engine_source_branch=main" in caplog.text
    assert f"delivery_base_branch={delivery_base}" in caplog.text


def test_fresh_editable_checkout_passes_with_only_local_git(
    monkeypatch, tmp_path,
):
    """HEAD == origin/main passes, and every probe is a LOCAL git read:
    a fresh checkout costs zero network requests (Issue #525)."""
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "reset", "--hard", new)
    point_module_file(monkeypatch, repo)
    commands: list[list[str]] = []
    info = runner.check_runner_source_freshness(
        gate_config(repo), run_command=recording_run_command(commands),
    )
    assert info["install"] == "editable"
    assert info["head"] == new
    assert info["origin_main"] == new
    assert commands, "the gate must actually probe the import checkout"
    network = {"fetch", "push", "pull", "ls-remote", "clone"}
    assert all(set(cmd).isdisjoint(network) for cmd in commands)


def test_stale_editable_checkout_fails_fast(monkeypatch, tmp_path, caplog):
    """deploy checkout reset to an old commit while origin/main advanced:
    the Runner refuses to run, with the structured `runner_source_stale`
    line (facts + the exact fix command) — the #525 failure must never
    again look like a business precondition failure."""
    repo, old, new = build_stale_repo(tmp_path)
    point_module_file(monkeypatch, repo)
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError) as excinfo:
            runner.check_runner_source_freshness(
                gate_config(repo), run_command=recording_run_command([]),
            )
    assert "runner_source_stale" in caplog.text
    assert old in caplog.text
    assert new in caplog.text
    assert cli_source.reinstall_command(repo) in caplog.text
    assert "runner_source_stale" in str(excinfo.value)


def test_stale_editable_worktree_scene_is_rejected(monkeypatch, tmp_path):
    """The exact 09-07 scene: the editable install resolves into a linked
    worktree pinned to an old commit while the shared origin/main ref is
    fresh. The import path — not the WorkingDirectory — is judged, so
    the scene is rejected."""
    repo, old, new = build_stale_repo(tmp_path)
    worktree = tmp_path / "orbi-issue-467-950d1bfc"
    git(repo, "worktree", "add", str(worktree), "-b", "task", old)
    point_module_file(monkeypatch, worktree)
    with pytest.raises(runner.RunnerSourceStaleError):
        runner.check_runner_source_freshness(
            gate_config(repo), run_command=recording_run_command([]),
        )


def test_allow_stale_runner_downgrades_to_warning(monkeypatch, tmp_path, caplog):
    """The explicit offline escape hatch: the same stale facts are logged
    as a warning and the tick continues — never silently."""
    repo, old, new = build_stale_repo(tmp_path)
    point_module_file(monkeypatch, repo)
    with caplog.at_level("WARNING"):
        info = runner.check_runner_source_freshness(
            gate_config(repo, allow_stale_runner=True),
            run_command=recording_run_command([]),
        )
    assert info["head"] == old
    assert "runner_source_stale" in caplog.text


def test_editable_without_origin_ref_cannot_pass(monkeypatch, tmp_path, caplog):
    """Freshness must be PROVEN from git facts: a checkout whose
    origin/<base> ref cannot be resolved is unverifiable and fails the
    same way (never a silent pass)."""
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    point_module_file(monkeypatch, repo)
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo), run_command=recording_run_command([]),
            )
    assert "runner_source_stale" in caplog.text
    assert "reason=" in caplog.text


# --- non-editable install form (uv tool / PyPI copy) ---------------------------


def _non_editable_install(monkeypatch, tmp_path: Path, version: str) -> Path:
    site_pkg = tmp_path / "site-packages" / "orbi"
    site_pkg.mkdir(parents=True, exist_ok=True)
    (site_pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_source, "module_file", lambda: site_pkg / "__init__.py")
    monkeypatch.setattr(runner, "_orbi_distribution_version", lambda: version)
    return site_pkg


def _tagged_repo(tmp_path: Path) -> Path:
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "tag", "v0.3.4", old)
    git(repo, "tag", "v0.3.5", new)
    return repo


def test_non_editable_version_older_than_latest_tag_fails(
    monkeypatch, tmp_path, caplog,
):
    """Non-editable install (no git checkout at the import path): the
    installed distribution version is compared with the latest release
    tag reachable from the fetched origin/main — an older install fails
    with the structured line (install=non_editable)."""
    repo = _tagged_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.4")
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo), run_command=recording_run_command([]),
            )
    assert "install=non_editable" in caplog.text
    assert "version=0.3.4" in caplog.text
    assert "origin_main=v0.3.5" in caplog.text


def test_non_editable_version_at_latest_tag_passes(monkeypatch, tmp_path):
    repo = _tagged_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.5")
    info = runner.check_runner_source_freshness(
        gate_config(repo), run_command=recording_run_command([]),
    )
    assert info["install"] == "non_editable"
    assert info["origin_main"] == "v0.3.5"


def test_orbi_distribution_version_reads_install_metadata(monkeypatch):
    """The real seam reads importlib.metadata (install metadata, not the
    code's self-reported __version__) — and only for the `orbi` dist."""
    seen: list[str] = []

    def fake_version(name: str) -> str:
        seen.append(name)
        return "9.9.9"

    monkeypatch.setattr("importlib.metadata.version", fake_version)
    assert runner._orbi_distribution_version() == "9.9.9"
    assert seen == ["orbi"]


def test_non_editable_unreadable_version_cannot_pass(
    monkeypatch, tmp_path, caplog,
):
    """No install metadata -> freshness cannot be PROVEN -> the same
    fail fast with a reason= field, never a silent pass."""
    import importlib.metadata

    repo = _tagged_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.5")

    def boom():
        raise importlib.metadata.PackageNotFoundError("orbi")

    monkeypatch.setattr(runner, "_orbi_distribution_version", boom)
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo), run_command=recording_run_command([]),
            )
    assert "runner_source_stale" in caplog.text
    assert "reason=unverifiable_version_state" in caplog.text


# --- engine source tracks (Issue #535) -------------------------------------------


def _tagged_at(repo: Path, commit: str, tag: str, *, annotated: bool = True):
    if annotated:
        git(repo, "tag", "-a", tag, "-m", tag, commit)
    else:
        git(repo, "tag", tag, commit)


def test_fresh_editable_tag_lock_passes(monkeypatch, tmp_path, caplog):
    """`engine_source_track = "tag:vX.Y.Z"`: the expected commit is the
    dereferenced tag (annotated tags resolve to their commit), and a
    checkout exactly at that commit is FRESH even though origin/main is
    ahead — the lock is the channel, not the branch."""
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "reset", "--hard", new)
    _tagged_at(repo, new, "v0.4.2")
    point_module_file(monkeypatch, repo)
    with caplog.at_level("INFO"):
        info = runner.check_runner_source_freshness(
            gate_config(repo, engine_source_track="tag:v0.4.2"),
            run_command=recording_run_command([]),
        )
    assert info["engine_source_track"] == "tag:v0.4.2"
    assert info["resolved"] == "refs/tags/v0.4.2"
    assert info["expected"] == new
    assert "engine_source_track=tag:v0.4.2" in caplog.text


def test_stale_editable_tag_lock_fails_fast(monkeypatch, tmp_path, caplog):
    """HEAD below the locked tag: the Runner refuses to run with the
    structured stale line carrying the lock facts."""
    repo, old, new = build_stale_repo(tmp_path)
    _tagged_at(repo, new, "v0.4.2")
    point_module_file(monkeypatch, repo)
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo, engine_source_track="tag:v0.4.2"),
                run_command=recording_run_command([]),
            )
    assert "runner_source_stale" in caplog.text
    assert "engine_source_track=tag:v0.4.2" in caplog.text
    assert old in caplog.text and new in caplog.text


def test_editable_tag_lock_without_the_tag_is_unverifiable(
    monkeypatch, tmp_path, caplog,
):
    repo, old, new = build_stale_repo(tmp_path)
    point_module_file(monkeypatch, repo)
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo, engine_source_track="tag:v9.9.9"),
                run_command=recording_run_command([]),
            )
    assert "reason=unverifiable_engine_source" in caplog.text


def test_fresh_editable_release_track_ignores_pre_releases(
    monkeypatch, tmp_path,
):
    """`engine_source_track = "release"`: the expected commit is the
    newest OFFICIAL semver tag's commit — a newer pre-release tag does
    not make the checkout stale."""
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "reset", "--hard", new)
    _tagged_at(repo, old, "v0.3.5")
    _tagged_at(repo, new, "v0.4.0")
    # The pre-release can also be a LIGHTWEIGHT tag: the exclusion rule
    # is about the version, not the tag object.
    _tagged_at(repo, new, "v0.5.0-rc.1", annotated=False)
    point_module_file(monkeypatch, repo)
    info = runner.check_runner_source_freshness(
        gate_config(repo, engine_source_track="release"),
        run_command=recording_run_command([]),
    )
    assert info["resolved"] == "refs/tags/v0.4.0"
    assert info["expected"] == new


def test_fresh_editable_sha_lock_passes(monkeypatch, tmp_path):
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "reset", "--hard", new)
    point_module_file(monkeypatch, repo)
    info = runner.check_runner_source_freshness(
        gate_config(repo, engine_source_track=f"sha:{new}"),
        run_command=recording_run_command([]),
    )
    assert info["resolved"] == new
    assert info["expected"] == new


def test_fresh_editable_branch_track_uses_that_branch_ref(
    monkeypatch, tmp_path,
):
    """`branch:<name>`: the expected commit is the fetched
    origin/<name> head — the engine may follow a non-main branch."""
    repo, old, new = build_stale_repo(tmp_path)
    git(repo, "reset", "--hard", new)
    git(repo, "update-ref", "refs/remotes/origin/beta", new)
    point_module_file(monkeypatch, repo)
    info = runner.check_runner_source_freshness(
        gate_config(repo, engine_source_track="branch:beta"),
        run_command=recording_run_command([]),
    )
    assert info["engine_source_branch"] == "beta"
    assert info["expected_ref"] == "refs/remotes/origin/beta"
    assert info["expected"] == new


def test_non_editable_release_track_compares_against_the_resolved_tag(
    monkeypatch, tmp_path, caplog,
):
    repo = _tagged_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.4")
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo, engine_source_track="release"),
                run_command=recording_run_command([]),
            )
    assert "version=0.3.4" in caplog.text
    assert "resolved=refs/tags/v0.3.5" in caplog.text


def test_non_editable_tag_lock_at_the_locked_version_passes(
    monkeypatch, tmp_path,
):
    repo = _tagged_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.4")
    info = runner.check_runner_source_freshness(
        gate_config(repo, engine_source_track="tag:v0.3.4"),
        run_command=recording_run_command([]),
    )
    assert info["install"] == "non_editable"
    assert info["resolved"] == "refs/tags/v0.3.4"


def test_non_editable_sha_lock_is_unverifiable_and_fails_closed(
    monkeypatch, tmp_path, caplog,
):
    """A sha lock cannot be mapped to an install version: fail closed
    (Issue #535: an unverifiable source state never runs)."""
    repo, old, new = build_stale_repo(tmp_path)
    _non_editable_install(monkeypatch, tmp_path, "0.3.5")
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.RunnerSourceStaleError):
            runner.check_runner_source_freshness(
                gate_config(repo, engine_source_track=f"sha:{new}"),
                run_command=recording_run_command([]),
            )
    assert "reason=unverifiable_version_state" in caplog.text





def test_parse_release_version_handles_tags_and_rejects_noise():
    assert runner._parse_release_version("v0.3.4") == (0, 3, 4)
    assert runner._parse_release_version("0.3.10") > (0, 3, 9)
    assert runner._parse_release_version("not-a-version") is None
    assert runner._parse_release_version("") is None


# --- config field ----------------------------------------------------------------


def test_load_config_allow_stale_runner_defaults_false(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    loaded = runner.load_config(config, check_provider_api_keys=False)
    assert loaded.allow_stale_runner is False


def test_load_config_allow_stale_runner_rejects_non_boolean(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nallow_stale_runner = "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allow_stale_runner"):
        runner.load_config(config, check_provider_api_keys=False)


# --- main() wiring ----------------------------------------------------------------


def _write_prompts(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    """The repro helper must fail loudly on a git error, never pass a
    broken setup silently."""
    repo, old, new = build_stale_repo(tmp_path)
    with pytest.raises(AssertionError, match="rc="):
        git(repo, "rev-parse", "--verify", "refs/heads/no-such-branch")


def test_main_source_gate_blocks_claim_before_slot(monkeypatch, tmp_path):
    """The gate is a start invariant: when the running source is stale,
    the tick dies BEFORE any slot is taken and nothing is claimed."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("pick_next_delivery must not run on a stale runner")

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on a stale runner",
    ):
        fail_if_called()
    monkeypatch.setattr(
        runner, "check_runner_source_freshness",
        lambda *a, **k: (_ for _ in ()).throw(
            runner.RunnerSourceStaleError(
                "runner_source_stale head=old origin_main=new",
            ),
        ),
    )
    with pytest.raises(runner.RunnerSourceStaleError, match="runner_source_stale"):
        runner.main(["--config", str(config)])
    assert not (tmp_path / ".orbi" / "slots").exists()


def test_main_source_gate_clean_proceeds_to_claim(monkeypatch, tmp_path):
    """A proven-fresh source logs nothing fatal and the tick proceeds to
    the normal claim flow (slot taken, queue scanned)."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        runner, "check_runner_source_freshness",
        lambda *a, **k: {"install": "editable"},
    )
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert (tmp_path / ".orbi" / "slots" / "slot-1").exists()
