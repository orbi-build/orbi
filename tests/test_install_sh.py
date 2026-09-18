"""The install.sh platform gate (Issue #849).

The installer is the FIRST user path on a new machine: on macOS it must
require ``launchctl`` (present by default), on Linux ``systemctl``; on a
machine with neither scheduler it must fail with an honest
platform-limitation message carrying the issue link — never the old
opaque ``required command missing: systemctl``. The tests run the real
script with a stubbed PATH (no network, no real clone), so the gate
logic is exercised exactly as a user's shell would.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

ISSUE_URL = "https://github.com/orbi-build/orbi/issues/849"

# The coreutils the script needs beyond the stubs, symlinked into the
# stub PATH so no real systemctl/launchctl can leak in through the
# host PATH. Each tool is resolved on the HOST (`shutil.which`): macOS
# keeps mkdir/rm/cp/cat/chmod in /bin while Linux's merged /usr puts
# them in /usr/bin — the old hardcoded /usr/bin link dangled on a Mac
# and install.sh died with an opaque 127 at the first direct call
# (evidenced on the hosted runner, Issue #894). `timeout` is never
# symlinked: a vanilla macOS (and the hosted runner, Issue #868) has
# none, and the stub dir always carries the bounded-exec stub below
# instead, on every host.
CORE_TOOLS = (
    "mkdir", "sed", "grep", "mktemp", "rm", "cp", "cat", "uname",
    "chmod",
)
TIMEOUT_STUB = "#!/bin/sh\nshift\nexec \"$@\"\n"


def make_stub_dir(
    tmp_path: Path, stubs: dict[str, str], without: tuple[str, ...] = ()
) -> Path:
    """One PATH entry: stub scripts win, core tools are host symlinks."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    for tool in CORE_TOOLS:
        if tool in stubs or tool in without:
            continue  # a stub with this name wins (e.g. the uname shim)
        host = shutil.which(tool)
        if host is None:
            pytest.fail(
                f"core tool {tool!r} not found on the host PATH: the "
                "stub dir would carry a dangling symlink"
            )
        (bin_dir / tool).symlink_to(host)
    if "timeout" not in stubs and "timeout" not in without:
        stub = bin_dir / "timeout"
        stub.write_text(TIMEOUT_STUB, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def run_install(tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "ORBI_HOME": str(tmp_path / "orbi-home"),
    }
    return subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=env, capture_output=True, text=True, timeout=60,
    )


# The Pi step contract (Issue #1080). Pi is the uv-shaped case: a single
# user-scope CLI the installer provides when missing. Node is its engine
# and stays the user's prerequisite (the way gh is), floor 22.19.0 —
# verified against the real npm registry on 2026-09-18:
# `engines = { node: '>=22.19.0' }` for @earendil-works/pi-coding-agent.
PI_INSTALL_CMD = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
PI_REPO_URL = "https://github.com/earendil-works/pi"
NODE_FLOOR = "22.19.0"
NODE_INSTALL_URL = "https://nodejs.org/en/download"


def pass_stubs() -> dict[str, str]:
    """The commands before the scheduler gate must be present."""
    return {
        "git": "#!/bin/sh\nexit 0\n",
        "gh": "#!/bin/sh\nexit 0\n",
        "curl": "#!/bin/sh\nexit 0\n",
        "uv": "#!/bin/sh\nexit 0\n",
        # The installer provides pi itself (Issue #1080): a working
        # `pi --version` is the skip condition, so the pre-existing
        # scheduler/clone tests carry the stub to keep walking past it.
        "pi": "#!/bin/sh\nexit 0\n",
    }


def test_linux_without_systemctl_reports_platform_limitation(tmp_path):
    # The acceptance scene: a LINUX machine (the uname shim pins the
    # platform — the real `uname` would make this scene host-dependent)
    # with NO systemctl on the machine.
    stubs = pass_stubs()
    stubs["uname"] = "#!/bin/sh\necho Linux\n"
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "systemctl" in result.stderr
    assert "launchd" in result.stderr and "macOS" in result.stderr
    assert ISSUE_URL in result.stderr
    # The old opaque message must not come back.
    assert "required command missing: systemctl" not in result.stderr


def test_macos_without_launchctl_reports_platform_limitation(tmp_path):
    stubs = pass_stubs()
    stubs["uname"] = "#!/bin/sh\necho Darwin\n"
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "launchctl" in result.stderr
    assert ISSUE_URL in result.stderr


def test_macos_with_launchctl_passes_the_scheduler_gate(tmp_path):
    # uname says Darwin, launchctl exists: the gate must pass and the
    # install must move on to the NEXT step (the git clone, stubbed to
    # leave a marker and fail fast — never a real network clone).
    marker = tmp_path / "git-reached"
    stubs = pass_stubs()
    stubs["uname"] = "#!/bin/sh\necho Darwin\n"
    stubs["launchctl"] = "#!/bin/sh\nexit 0\n"
    stubs["git"] = (
        "#!/bin/sh\n"
        f"echo reached > {marker}\n"
        "echo 'stub: clone step (out of scope)' >&2\n"
        "exit 1\n"
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    # A missing marker is an INSTALLER failure scene: the message carries
    # the captured rc/stdout/stderr so the log shows where the walk died.
    assert marker.exists(), (
        f"the installer never reached the clone step: "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r} "
        f"stub_dir={sorted(p.name for p in bin_dir.iterdir())!r}"
    )
    assert marker.read_text().strip() == "reached"
    # The scheduler gate said nothing: no platform-limitation output.
    assert ISSUE_URL not in result.stderr
    assert "required command missing" not in result.stderr


def test_unsupported_platform_names_the_limitation(tmp_path):
    stubs = pass_stubs()
    stubs["uname"] = "#!/bin/sh\necho FreeBSD\n"
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "FreeBSD" in result.stderr
    assert ISSUE_URL in result.stderr


def test_macos_without_timeout_reaches_the_clone_step(tmp_path):
    # Issue #868: a vanilla macOS PATH has no coreutils `timeout`
    # (Homebrew installs it as gtimeout) and no `uv` — uv is exactly
    # what this script installs. After the launchctl gate the install
    # must walk the uv-install branch (the first timed steps) and reach
    # the clone step: `timeout: command not found` under set -e must
    # never surface. HOME is redirected so the stub installer's uv
    # lands in the redirected ~/.local/bin, like the real installer.
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    marker = tmp_path / "git-reached"
    stubs = {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": "#!/bin/sh\nexit 0\n",
        "gh": "#!/bin/sh\nexit 0\n",
        "curl": "#!/bin/sh\nexit 0\n",
        # A working pi skips the Pi step (Issue #1080) — this scene is
        # about the timeout walk, not the Pi gate.
        "pi": "#!/bin/sh\nexit 0\n",
        # The stub installer provides uv the way the real one does.
        "sh": (
            "#!/bin/sh\n"
            "printf '#!/bin/sh\\nexit 0\\n' > \"$HOME/.local/bin/uv\"\n"
            "chmod 755 \"$HOME/.local/bin/uv\"\n"
            "exit 0\n"
        ),
        "git": (
            "#!/bin/sh\n"
            f"echo reached > {marker}\n"
            "exit 1\n"
        ),
        # No uv on PATH (it gets installed) and, via `without`, no timeout.
    }
    bin_dir = make_stub_dir(tmp_path, stubs, without=("timeout",))
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "ORBI_HOME": str(tmp_path / "orbi-home"),
        "HOME": str(home),
    }
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert marker.exists(), (
        f"the installer never reached the clone step: "
        f"rc={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r} "
        f"stub_dir={sorted(p.name for p in bin_dir.iterdir())!r}"
    )
    assert marker.read_text().strip() == "reached"
    assert "command not found" not in result.stderr


def test_sed_in_place_uses_the_bsd_compatible_form():
    # Issue #868: BSD sed (macOS) requires a backup suffix right after
    # -i; the bare GNU form `sed -i "s#…"` makes BSD sed treat the
    # config file as the sed script, so the placeholder edit fails. The
    # portable form is `sed -i.bak … file && rm -f file.bak` — locked here.
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert 'sed -i "' not in body
    assert "sed -i.bak" in body
    assert "rm -f orbi.toml.bak" in body


# ---------------------------------------------------------------------------
# The Pi step (Issue #1080). Pi is the uv-shaped case: the installer
# provides it when missing and names the prerequisite when it cannot.
# The behavioral tests run the real script on the stub PATH above, so a
# "reachable clone" marker doubles as the proof that the Pi step let the
# flow through (the Pi step sits before the clone, and the clone is the
# first step after it that the stubs stop).
# ---------------------------------------------------------------------------


def test_install_sh_declares_the_pi_step_contract():
    # Acceptance 5: the Pi install command string is present, the Node
    # floor 22.19.0 appears with a comparison, and the Pi step precedes
    # the `uv tool install` line. Text contract — no network.
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert PI_INSTALL_CMD in body
    assert f'"{NODE_FLOOR}"' in body, (
        f"the Node floor {NODE_FLOOR} must appear as a quoted constant"
    )
    assert " -lt " in body, (
        "the floor must be enforced by a numeric comparison (the "
        "equal and above branches are behavior-locked by the "
        "exact-floor and above-floor tests)"
    )
    assert body.index(PI_INSTALL_CMD) < body.index("uv tool install"), (
        "the Pi step must precede the `uv tool install` line"
    )


def reached_clone_stubs(
    tmp_path: Path, extra: dict[str, str], with_pi: bool = False
) -> tuple[dict, Path]:
    """pass_stubs (pi removed unless with_pi), plus a git stub marking
    the clone step.

    The platform is pinned to a Linux host WITH systemd (the same uname
    shim pattern as the scheduler-gate tests) so the walk to the clone
    never depends on the machine running the tests.
    """
    marker = tmp_path / "git-reached"
    stubs = pass_stubs()
    if not with_pi:
        del stubs["pi"]  # the caller decides whether pi exists
    stubs["uname"] = "#!/bin/sh\necho Linux\n"
    stubs["systemctl"] = "#!/bin/sh\nexit 0\n"
    stubs["git"] = (
        "#!/bin/sh\n"
        f"echo reached > {marker}\n"
        "exit 1\n"
    )
    stubs.update(extra)
    return stubs, marker


def test_pi_step_skipped_when_pi_already_executes(tmp_path):
    # Acceptance 4: reruns stay idempotent — a working `pi --version`
    # prints a skip line and npm is never invoked.
    npm_called = tmp_path / "npm-called"
    stubs, marker = reached_clone_stubs(
        tmp_path, {"npm": f"#!/bin/sh\necho called > {npm_called}\nexit 1\n"},
        with_pi=True,
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert marker.exists(), (
        f"the installer never passed the Pi step: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "skip" in result.stderr
    assert not npm_called.exists(), "a working pi must not invoke npm"


def test_pi_installed_with_npm_when_missing(tmp_path):
    # Acceptance 1: pi absent → the exact npm command → re-verified
    # `pi --version` → the flow continues to the clone. node here is
    # ABOVE the floor (26.8.1), covering the greater-than branch of the
    # version gate; the exact-floor case has its own test below.
    stubs, marker = reached_clone_stubs(tmp_path, {})
    stubs["node"] = "#!/bin/sh\necho v26.8.1\n"
    npm_args = tmp_path / "npm-args"
    bin_dir = tmp_path / "stubbin"
    stubs["npm"] = (
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {npm_args}\n"
        # The global install provides pi the way the real npm would.
        f"printf '#!/bin/sh\\nexit 0\\n' > {bin_dir}/pi\n"
        f"chmod 755 {bin_dir}/pi\n"
        "exit 0\n"
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert marker.exists(), (
        f"the installer never passed the Pi step: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    args = npm_args.read_text(encoding="utf-8")
    # `npm` itself is argv[0] and never part of "$@" — the tokens that
    # matter are the install flags and the package.
    for token in ("install", "-g", "--ignore-scripts",
                  "@earendil-works/pi-coding-agent"):
        assert token in args, f"npm must be called with {token!r}, got {args!r}"


def test_node_at_the_exact_floor_passes_the_gate(tmp_path):
    # v22.19.0 itself satisfies `>= 22.19.0`: the gate must let the
    # install through to npm (the equality branch of the comparison).
    # The npm stub provides pi so the flow continues to the clone.
    npm_called = tmp_path / "npm-called"
    stubs, marker = reached_clone_stubs(tmp_path, {})
    bin_dir = tmp_path / "stubbin"
    stubs["node"] = f"#!/bin/sh\necho v{NODE_FLOOR}\n"
    stubs["npm"] = (
        "#!/bin/sh\n"
        f"echo called > {npm_called}\n"
        f"printf '#!/bin/sh\\nexit 0\\n' > {bin_dir}/pi\n"
        f"chmod 755 {bin_dir}/pi\n"
        "exit 0\n"
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert marker.exists(), (
        f"node at the exact floor was rejected: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert npm_called.exists()


def test_node_missing_stops_before_any_configuration(tmp_path):
    # Acceptance 2 + the failure path: no node → named exit carrying the
    # floor and the official Node link; the clone (and everything after
    # it, up to the unit-enabling setup) never runs.
    stubs, marker = reached_clone_stubs(tmp_path, {})
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "node" in result.stderr
    assert NODE_FLOOR in result.stderr
    assert NODE_INSTALL_URL in result.stderr
    assert not marker.exists(), "a missing node must stop before the clone"


def test_node_too_old_prints_found_and_required_versions(tmp_path):
    # The debian:trixie scene: distro node 20 installs Pi without error
    # and every later pi call crashes. The gate must exit with BOTH
    # versions before anything is configured.
    stubs, marker = reached_clone_stubs(
        tmp_path, {"node": "#!/bin/sh\necho v20.19.0\n"}
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "20.19.0" in result.stderr, "the found version must be printed"
    assert NODE_FLOOR in result.stderr, "the required version must be printed"
    assert NODE_INSTALL_URL in result.stderr
    assert not marker.exists()


def test_npm_missing_names_npm_and_the_pi_repository(tmp_path):
    # Acceptance 3: npm absent → exit naming npm and the Pi repository
    # link, never a package-manager install attempt.
    stubs, marker = reached_clone_stubs(
        tmp_path, {"node": f"#!/bin/sh\necho v{NODE_FLOOR}\n"}
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "npm" in result.stderr
    assert PI_REPO_URL in result.stderr
    assert not marker.exists()


def test_pi_install_failure_fails_fast_before_the_clone(tmp_path):
    # Acceptance 1's re-verify: an npm "success" that does not produce a
    # working `pi --version` must stop the install (the Debian Node-20
    # trap installs fine and crashes on every call — the re-verify is
    # the guard), not walk into the clone with a broken agent.
    stubs, marker = reached_clone_stubs(
        tmp_path,
        {
            "node": f"#!/bin/sh\necho v{NODE_FLOOR}\n",
            "npm": "#!/bin/sh\nexit 0\n",  # "succeeds", no working pi
        },
    )
    bin_dir = make_stub_dir(tmp_path, stubs)
    result = run_install(tmp_path, bin_dir)
    assert result.returncode == 1
    assert "pi --version" in result.stderr
    assert PI_REPO_URL in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("name", ["install.sh"])
def test_install_script_exists(name):
    assert INSTALL_SH.is_file()


def test_make_stub_dir_fails_loudly_when_the_host_lacks_a_core_tool(
        tmp_path, monkeypatch):
    # A host without one of the core tools must fail the sandbox setup
    # LOUDLY — a dangling symlink would only surface later as an
    # opaque `127: command not found` inside install.sh (the hosted
    # macOS /bin vs /usr/bin scene, Issue #894).
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.fail.Exception, match="core tool 'mkdir'"):
        make_stub_dir(tmp_path, pass_stubs())
