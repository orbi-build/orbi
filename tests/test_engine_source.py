"""Orbi engine source update channel (Issue #535).

The deploy home follows exactly one ``engine_source_track`` channel:
absent/``main`` fast-forwards ``origin/main`` (the pre-#535 dogfood
behavior), ``branch:<name>`` fast-forwards one branch, ``release``
follows the newest OFFICIAL semver tag (pre-releases excluded),
``tag:<name>`` and ``sha:<40-hex>`` lock one exact commit (annotated
tags dereferenced, detached HEAD allowed). Every unresolvable state —
a dirty checkout, a missing tag/SHA, a missing branch ref, a
non-fast-forwardable branch, a conflicting local tag — fails closed
with a structured line and a fix.

The repro paths build REAL git repositories in tmp_path (a bare origin,
a deploy-home clone and a dev clone for remote advances), so every
assertion below exercises the same git operations the service
``ExecStartPre`` runs through ``orbi sync-engine-source``.
"""
import subprocess
from types import SimpleNamespace

import pytest

from orbi import engine_source


def _run(args: list[str]) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def git(repo, *args: str) -> str:
    return _run(["git", "-C", str(repo), *args])


def git_ok(repo, *args: str) -> str | None:
    """A git probe: None when git exits non-zero (e.g. `symbolic-ref -q`
    reports a detached HEAD with rc=1 — an answer, not an error)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def real_run_command(command, *, cwd=None, timeout=None, **kwargs):
    """A run_command executing real git (the production contract)."""
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        timeout=timeout or 60,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command,
            output=result.stdout, stderr=result.stderr,
        )
    return result.stdout.strip()


def commit_file(repo: str, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def engine_repo(tmp_path):
    """A bare origin plus a clean deploy-home clone carrying: annotated
    official tags v0.4.2/v0.4.8, a pre-release tag v0.5.0-rc.1, a
    lightweight non-semver tag v1, and three commits (c1 < c2 < c3=tip)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    home = tmp_path / "deploy-home"
    _run(["git", "clone", str(origin), str(home)])
    git(home, "config", "user.email", "pilot@test.local")
    git(home, "config", "user.name", "Pilot")
    c1 = commit_file(home, "a.txt")
    git(home, "tag", "-a", "v0.4.2", "-m", "v0.4.2", c1)
    c2 = commit_file(home, "b.txt")
    git(home, "tag", "-a", "v0.4.8", "-m", "v0.4.8", c2)
    c3 = commit_file(home, "c.txt")
    git(home, "tag", "-a", "v0.5.0-rc.1", "-m", "pre", c3)
    git(home, "tag", "v1", c1)
    git(home, "push", "origin", "main", "--tags")
    return SimpleNamespace(
        origin=origin, home=home, c1=c1, c2=c2, c3=c3,
    )


def make_dev(engine_repo, tmp_path):
    """A second clone that advances the remote (the release/train role)."""
    dev = tmp_path / "dev"
    _run(["git", "clone", str(engine_repo.origin), str(dev)])
    git(dev, "config", "user.email", "pilot@test.local")
    git(dev, "config", "user.name", "Pilot")
    return dev


# --- track parsing (config fail-fast) -------------------------------------------


def test_normalize_track_defaults_to_main():
    assert engine_source.normalize_engine_source_track(None) == "main"


@pytest.mark.parametrize(
    "value",
    ["main", "release", "branch:release-candidate", "tag:v0.4.2",
     "sha:" + "a" * 40],
)
def test_normalize_track_accepts_the_documented_forms(value):
    assert engine_source.normalize_engine_source_track(value) == value


def test_normalize_track_stable_alias_resolves_to_release():
    """Issue #756: `stable` is an alias of `release` — the identical
    newest-official-release channel, normalized at the single parse
    entry so no second resolve path exists."""
    assert engine_source.normalize_engine_source_track("stable") == "release"


def test_invalid_track_error_message_names_the_stable_alias():
    """The accepted-forms error must surface the stable alias (a user
    typing a wrong value discovers it); `latest` stays invalid."""
    with pytest.raises(ValueError, match=r"alias: 'stable'"):
        engine_source.normalize_engine_source_track("bogus")


@pytest.mark.parametrize(
    "value",
    ["", 123, "latest", "MAIN", "branch:", "branch:bad name", "branch:-x",
     "branch:x..y", "branch:x//y", "tag:", "tag:a.lock", "sha:abc",
     "sha:" + "a" * 41, "main:extra"],
)
def test_normalize_track_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="engine_source_track"):
        engine_source.normalize_engine_source_track(value)


def test_split_track_kinds():
    assert engine_source.split_track("main") == ("branch", "main")
    assert engine_source.split_track("release") == ("release", None)
    assert engine_source.split_track("tag:v0.4.2") == ("tag", "v0.4.2")
    assert engine_source.split_track("sha:" + "a" * 40) == (
        "sha", "a" * 40,
    )


# --- semver tag selection --------------------------------------------------------


def test_parse_semver_tag_versions_and_pre_releases():
    assert engine_source.parse_semver_tag("v0.10.0") == ((0, 10, 0), False)
    assert engine_source.parse_semver_tag("v0.5.0-rc.1") == ((0, 5, 0), True)
    assert engine_source.parse_semver_tag("1.2.3+build.7") == ((1, 2, 3), False)
    assert engine_source.parse_semver_tag("v1.2") is None
    assert engine_source.parse_semver_tag("nightly") is None


def test_latest_release_tag_orders_numerically_and_excludes_pre_releases():
    tags = ["v0.9.0", "v0.10.0", "v0.10.0-rc.1", "v1", "v0.4.8"]
    assert engine_source.latest_release_tag(tags) == "v0.10.0"


def test_latest_release_tag_without_an_official_tag_is_none():
    assert engine_source.latest_release_tag(["v0.5.0-rc.1"]) is None
    assert engine_source.latest_release_tag([]) is None


# --- main track (pre-#535 dogfood behavior, compatible) --------------------------


def test_sync_main_track_fast_forwards_and_stays_on_the_branch(
    engine_repo, caplog,
):
    home = engine_repo.home
    with caplog.at_level("INFO"):
        result = engine_source.sync_engine_source(
            home, "main", run_command=real_run_command,
        )
    assert result["resolved"] == "refs/remotes/origin/main"
    assert result["head"] == engine_repo.c3
    assert git(home, "symbolic-ref", "--short", "HEAD") == "main"
    assert "engine_source_synced" in caplog.text
    assert "engine_source_track=main" in caplog.text
    assert engine_repo.c3 in caplog.text


def test_sync_main_track_picks_up_a_remote_advance(engine_repo, tmp_path):
    home = engine_repo.home
    dev = make_dev(engine_repo, tmp_path)
    c4 = commit_file(dev, "d.txt")
    git(dev, "push", "origin", "main")
    engine_source.sync_engine_source(home, "main", run_command=real_run_command)
    assert git(home, "rev-parse", "HEAD") == c4


def test_sync_fails_closed_on_a_dirty_checkout(engine_repo):
    home = engine_repo.home
    (home / "a.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            home, "main", run_command=real_run_command,
        )
    message = str(excinfo.value)
    assert "deploy_home_dirty" in message
    assert "files=a.txt" in message
    assert "fix=" in message
    # Fail closed BEFORE any mutation: the head is unchanged.
    assert git(home, "rev-parse", "HEAD") == engine_repo.c3


def test_sync_fails_closed_on_a_diverged_main(engine_repo, tmp_path):
    home = engine_repo.home
    commit_file(home, "local.txt")
    dev = make_dev(engine_repo, tmp_path)
    commit_file(dev, "remote.txt")
    git(dev, "push", "origin", "main")
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            home, "main", run_command=real_run_command,
        )
    message = str(excinfo.value)
    assert "engine_source_not_fast_forwardable" in message
    assert "fix=" in message
    assert git(home, "rev-parse", "HEAD") != engine_repo.c3


# --- branch track -----------------------------------------------------------------


def test_sync_branch_track_creates_and_fast_forwards(engine_repo, tmp_path):
    home = engine_repo.home
    dev = make_dev(engine_repo, tmp_path)
    git(dev, "checkout", "-b", "release-candidate", engine_repo.c3)
    b1 = commit_file(dev, "b1.txt")
    git(dev, "push", "origin", "release-candidate")
    result = engine_source.sync_engine_source(
        home, "branch:release-candidate", run_command=real_run_command,
    )
    assert result["resolved"] == "refs/remotes/origin/release-candidate"
    assert result["head"] == b1
    assert git(home, "symbolic-ref", "--short", "HEAD") == "release-candidate"
    b2 = commit_file(dev, "b2.txt")
    git(dev, "push", "origin", "release-candidate")
    engine_source.sync_engine_source(
        home, "branch:release-candidate", run_command=real_run_command,
    )
    assert git(home, "rev-parse", "HEAD") == b2


def test_sync_branch_track_fails_closed_when_the_branch_is_missing(
    engine_repo,
):
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            engine_repo.home, "branch:ghost", run_command=real_run_command,
        )
    assert "reason=branch_ref_missing" in str(excinfo.value)


# --- release track -----------------------------------------------------------------


def test_sync_release_track_picks_the_newest_official_tag_and_dereferences(
    engine_repo, caplog,
):
    home = engine_repo.home
    with caplog.at_level("INFO"):
        result = engine_source.sync_engine_source(
            home, "release", run_command=real_run_command,
        )
    # v0.5.0-rc.1 is a pre-release and v1 is not semver: v0.4.8 wins.
    assert result["resolved"] == "refs/tags/v0.4.8"
    assert result["head"] == engine_repo.c2
    # An annotated tag dereferences to its COMMIT, not the tag object.
    assert git(home, "rev-parse", "v0.4.8") != engine_repo.c2
    # Detached: `symbolic-ref -q` answers rc=1 (the helper maps it to None).
    assert git_ok(home, "symbolic-ref", "-q", "--short", "HEAD") is None
    assert "engine_source_synced engine_source_track=release" in caplog.text


def test_sync_release_track_follows_a_new_release(engine_repo, tmp_path):
    home = engine_repo.home
    dev = make_dev(engine_repo, tmp_path)
    git(dev, "checkout", "--detach", engine_repo.c3)
    v050 = commit_file(dev, "d.txt")
    git(dev, "tag", "-a", "v0.5.0", "-m", "v0.5.0", v050)
    git(dev, "push", "origin", "--tags")
    result = engine_source.sync_engine_source(
        home, "release", run_command=real_run_command,
    )
    assert result["head"] == v050


def test_sync_release_track_fails_closed_without_release_tags(tmp_path):
    origin = tmp_path / "bare.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    home = tmp_path / "home"
    _run(["git", "clone", str(origin), str(home)])
    git(home, "config", "user.email", "pilot@test.local")
    git(home, "config", "user.name", "Pilot")
    commit_file(home, "a.txt")
    git(home, "push", "origin", "main")
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            home, "release", run_command=real_run_command,
        )
    assert "reason=no_official_release_tag" in str(excinfo.value)


# --- tag track (exact lock, rollback) -----------------------------------------------


def test_sync_tag_track_locks_and_rolls_back_to_the_exact_commit(
    engine_repo,
):
    home = engine_repo.home
    result = engine_source.sync_engine_source(
        home, "tag:v0.4.8", run_command=real_run_command,
    )
    assert result["head"] == engine_repo.c2
    result = engine_source.sync_engine_source(
        home, "tag:v0.4.2", run_command=real_run_command,
    )
    assert result["head"] == engine_repo.c1
    assert git(home, "rev-parse", "HEAD") == engine_repo.c1


def test_sync_tag_track_fails_closed_when_the_tag_does_not_exist(engine_repo):
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            engine_repo.home, "tag:v9.9.9", run_command=real_run_command,
        )
    assert "reason=tag_not_found" in str(excinfo.value)


def test_sync_tag_track_fails_closed_on_a_conflicting_local_tag(engine_repo):
    home = engine_repo.home
    git(home, "tag", "-f", "-a", "v0.4.2", "-m", "moved", engine_repo.c3)
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            home, "tag:v0.4.2", run_command=real_run_command,
        )
    assert "reason=tag_conflict" in str(excinfo.value)


# --- sha track (exact lock) ----------------------------------------------------------


def test_sync_sha_track_locks_the_exact_commit(engine_repo):
    home = engine_repo.home
    result = engine_source.sync_engine_source(
        home, f"sha:{engine_repo.c1}", run_command=real_run_command,
    )
    assert result["head"] == engine_repo.c1
    assert git(home, "rev-parse", "HEAD") == engine_repo.c1


def test_sync_sha_track_fails_closed_for_an_unreachable_sha(engine_repo):
    missing = "1" * 40
    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            engine_repo.home, f"sha:{missing}", run_command=real_run_command,
        )
    assert "reason=sha_not_found" in str(excinfo.value)
    assert missing in str(excinfo.value)


def test_sync_returns_to_the_main_branch_from_a_lock(engine_repo):
    home = engine_repo.home
    engine_source.sync_engine_source(
        home, f"sha:{engine_repo.c1}", run_command=real_run_command,
    )
    engine_source.sync_engine_source(
        home, "main", run_command=real_run_command,
    )
    assert git(home, "rev-parse", "HEAD") == engine_repo.c3
    assert git(home, "symbolic-ref", "--short", "HEAD") == "main"


def test_sync_tag_track_is_idempotent_when_already_at_the_commit(
    engine_repo,
):
    """The second sync of the same lock skips the checkout (the head is
    already the resolved commit) and succeeds unchanged."""
    home = engine_repo.home
    first = engine_source.sync_engine_source(
        home, "tag:v0.4.2", run_command=real_run_command,
    )
    second = engine_source.sync_engine_source(
        home, "tag:v0.4.2", run_command=real_run_command,
    )
    assert second == first
    assert git(home, "rev-parse", "HEAD") == engine_repo.c1


def test_sync_fails_closed_when_the_head_cannot_be_verified(engine_repo):
    """A checkout that lands somewhere else than the resolved commit is
    `engine_source_unverified` (the last-resort fail-closed line)."""

    def lying_head_run(command, *, cwd=None, timeout=None, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return engine_repo.c3
        return real_run_command(command, cwd=cwd, timeout=timeout, **kwargs)

    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            engine_repo.home, "tag:v0.4.2", run_command=lying_head_run,
        )
    message = str(excinfo.value)
    assert "engine_source_unverified" in message
    assert f"expected={engine_repo.c1}" in message


def test_sync_branch_reraises_a_fetch_failure_when_the_ref_exists(
    engine_repo, tmp_path,
):
    """A fetch that fails while the remote-tracking ref IS present (a
    network failure, not a missing branch) keeps the raw failure —
    fail fast, no invented reason."""

    def failing_fetch_run(command, *, cwd=None, timeout=None, **kwargs):
        if command[:2] == ["git", "fetch"]:
            raise subprocess.CalledProcessError(128, command)
        return real_run_command(command, cwd=cwd, timeout=timeout, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        engine_source.sync_engine_source(
            engine_repo.home, "main", run_command=failing_fetch_run,
        )





def test_git_helper_fails_fast_on_nonzero_exit(engine_repo):
    """The fixture git helper must fail loudly on a git error, never
    pass a broken setup silently."""
    with pytest.raises(AssertionError, match="rc="):
        git(engine_repo.home, "rev-parse", "--verify", "refs/heads/none")


def test_sync_branch_track_fails_closed_when_the_head_cannot_be_verified(
    engine_repo,
):
    """The branch channel has the same last-resort fail-closed line as
    the lock channels: a head that cannot be verified against the
    fetched ref is engine_source_unverified."""

    def lying_head_run(command, *, cwd=None, timeout=None, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0" * 40
        return real_run_command(command, cwd=cwd, timeout=timeout, **kwargs)

    with pytest.raises(engine_source.EngineSourceError) as excinfo:
        engine_source.sync_engine_source(
            engine_repo.home, "main", run_command=lying_head_run,
        )
    assert "engine_source_unverified" in str(excinfo.value)


# --- read-only status (doctor / freshness inputs) -------------------------------------


def test_engine_source_status_reports_track_resolved_and_head(engine_repo):
    home = engine_repo.home
    engine_source.sync_engine_source(
        home, "tag:v0.4.2", run_command=real_run_command,
    )
    status = engine_source.engine_source_status(
        home, "tag:v0.4.2", run_command=real_run_command,
    )
    assert status["ok"] is True
    assert status["resolved"] == "refs/tags/v0.4.2"
    assert status["head"] == engine_repo.c1
    assert status["expected"] == engine_repo.c1


def test_engine_source_status_reports_out_of_sync_without_raising(
    engine_repo,
):
    status = engine_source.engine_source_status(
        engine_repo.home, "tag:v0.4.2", run_command=real_run_command,
    )
    assert status["ok"] is False
    assert status["head"] == engine_repo.c3
    assert status["expected"] == engine_repo.c1


def test_engine_source_status_reports_the_unresolved_reason(engine_repo):
    status = engine_source.engine_source_status(
        engine_repo.home, "tag:v9.9.9", run_command=real_run_command,
    )
    assert status["ok"] is False
    assert "engine_source_unresolved" in status["error"]
    assert status["resolved"] == "-"


def test_engine_source_status_reports_a_missing_branch_ref(engine_repo):
    """`branch:<name>` with no fetched origin/<name> ref is unresolved
    (read-only status: the structured reason, no raise)."""
    status = engine_source.engine_source_status(
        engine_repo.home, "branch:ghost", run_command=real_run_command,
    )
    assert status["ok"] is False
    assert "reason=branch_ref_missing" in status["error"]
