"""Runner execution-source freshness preflight (Issue #525; extracted by
Issue #1650).

The 2026-09-07 incident — the editable install resolved into an OLD
issue worktree while the ExecStartPre preflight kept the deployment
checkout fresh — showed that "the checkout gets synced" and "the
process executes that checkout" are different facts. This gate judges
the IMPORT SOURCE of the running process (``cli_source.module_file``),
never the WorkingDirectory. All probes are LOCAL git reads: the
freshness of ``refs/remotes/origin/main`` is supplied by the
ExecStartPre fetch (a linked worktree shares the deployment checkout's
refs), so a fresh checkout costs zero network requests. The self-check
can only protect versions that carry it — the outermost defense stays
the shell-layer ExecStartPre preflight, which does not depend on the
runner version (docs/operations.mdx, the defense-layer section).

This module owns the whole preflight: the timeout, the failure
exception, the channel/version/git probes, the structured line and
``check_runner_source_freshness``. ``orbi.runner`` imports it as a
module object and calls the gate before any slot or claim; the module
imports NOTHING from ``orbi.runner`` (the delivery orchestration stays
out of the startup invariant). Source-channel resolution itself stays
in ``orbi.engine_source`` and the install facts in ``orbi.cli_source``.

No database, queue, daemon or fallback: local git reads and the
installed distribution metadata are the only inputs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import engine_source
from orbi.journal import event, quote_value

if TYPE_CHECKING:
    # Annotation-only: `orbi.runner` imports this module at runtime.
    from orbi import config as config_domain

# One local git read; a probe that cannot answer in this window is an
# expected "unverifiable" result, never a retry.
RUNNER_SOURCE_TIMEOUT_SECONDS = 30


class RunnerSourceStaleError(RuntimeError):
    """The running CLI source is not proven to be the configured engine
    source channel's commit (fail fast, before any slot or claim)."""


def _resolve_engine_source(track: str, cwd: Path, *,
                           run_command) -> dict | None:
    """Resolve one lock/release channel locally; None when unresolvable
    (an expected probe result inside the freshness gate)."""
    try:
        return engine_source.resolve_expected_head(
            track, cwd, run_command=run_command,
        )
    except engine_source.EngineSourceError:
        return None


def _parse_release_version(value: str) -> tuple[int, ...] | None:
    """Parse a ``vX.Y.Z`` release version into a comparable tuple.

    Returns None for anything else (the comparison then cannot prove
    freshness and the gate fails — never guesses).
    """
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts = text.split(".")
    if not text or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _orbi_distribution_version() -> str:
    """The installed ``orbi-cli`` distribution version (install
    metadata, not the code's self-reported ``__version__``; Issue #874
    renamed the PyPI distribution — the console script stays ``orbi``).
    Test seam: the non-editable form monkeypatches this module global."""
    import importlib.metadata
    return importlib.metadata.version("orbi-cli")


def _runner_source_git(args: list[str], cwd: Path, *, run_command) -> str | None:
    """One LOCAL read-only git probe; None when git cannot answer
    (an expected probe result, logged at DEBUG by run_command)."""
    try:
        return run_command(
            ["git", *args], cwd=cwd,
            timeout=RUNNER_SOURCE_TIMEOUT_SECONDS,
            failure_log_level=logging.DEBUG,
        )
    except Exception:
        return None


def _runner_source_stale_line(facts: dict, *, allowed: bool, fix: str) -> str:
    fields = " ".join(
        f"{key}={quote_value(str(value))}" for key, value in facts.items()
    )
    return (
        f"runner_source_stale {fields} "
        f"allowed={str(allowed).lower()} fix={quote_value(fix)}"
    )


def check_runner_source_freshness(config: config_domain.RunnerConfig, *, run_command) -> dict:
    """Startup invariant: prove that the code THIS process
    executes is the configured engine source channel's exact commit
    BEFORE any slot or claim (the channel is the
    ``engine_source_track`` — ``origin/main`` by default — never the
    delivery target's ``base_branch``). A stale (or unverifiable) source
    fails fast with the structured ``runner_source_stale`` line (facts +
    the exact fix command, the ``deploy_home_dirty`` style); the explicit
    ``allow_stale_runner`` config downgrades the same line to a warning.

    Two install forms, both judged from git/install metadata facts:

    - editable: ``git rev-parse --show-toplevel`` at the import source
      resolves a checkout, and the checkout carries the src-layout
      package path (a $HOME dotfiles repo never matches ``src/orbi``,
      so it cannot fake an editable install) — then the checkout's
      ``HEAD`` must equal the channel's expected commit: the fetched
      ``refs/remotes/origin/<branch>`` head for the main/branch tracks,
      or the resolved tag commit / exact SHA for the lock tracks. The
      09-07 scene (an editable install bound to an old issue worktree)
      fails here: worktrees share the fetched refs.
    - non-editable: the installed distribution version
      (importlib.metadata) must not be older than the channel's release
      tag — the latest tag reachable from the tracked branch ref, or the
      locked release/tag itself (resolved in the deployment home). A
      ``sha:`` lock cannot be mapped to a version and fails closed as
      unverifiable. Version equality or newer passes (a dev install
      ahead of the tags is not stale).

    Whatever cannot be PROVEN fresh (missing origin ref, no release tag,
    unresolvable import source) fails the same way with a ``reason=``
    field — never a silent pass. Returns the fresh-facts dict; raises
    ``RunnerSourceStaleError`` unless ``allow_stale_runner`` is set.
    """
    # The engine checkout follows the configured ENGINE source channel;
    # ``base_branch`` belongs to the delivery target and must not affect
    # this independent freshness check.
    track = engine_source.normalize_engine_source_track(
        config.engine_source_track,
    )
    kind, argument = engine_source.split_track(track)
    delivery_base_branch = config.base_branch
    deploy_home = Path(config.deploy_home)
    # The import-source probe reads the EXECUTING package
    # (cli_source.module_file), never this module's own __file__: a
    # non-editable copy would otherwise compare its site-packages path
    # with a checkout and prove nothing. Resolved at call time, so this
    # module's import graph stays engine_source + journal.
    from orbi import cli_source
    module_path = cli_source.module_file()
    package_dir = module_path.parent

    toplevel_raw = _runner_source_git(
        ["rev-parse", "--show-toplevel"], package_dir, run_command=run_command,
    )
    toplevel = Path(toplevel_raw).resolve() if toplevel_raw else None
    editable = (
        toplevel is not None
        and toplevel / cli_source.PACKAGE_DIR == package_dir.resolve()
    )

    # For the plain main track the facts keep the pre-#535 field names
    # (engine_source_branch / origin_main); every other channel names
    # what it resolved (expected ref, tag or SHA).
    def _branch_facts(extra: dict) -> dict:
        fields = {"engine_source_branch": argument}
        if argument == "main":
            fields["origin_main"] = extra["origin_main"]
        else:
            fields["expected_ref"] = extra["expected_ref"]
            fields["expected"] = extra["expected"]
        return fields

    if editable:
        head = _runner_source_git(
            ["rev-parse", "HEAD"], toplevel, run_command=run_command,
        )
        if kind == "branch":
            expected_ref = f"refs/remotes/origin/{argument}"
            expected = _runner_source_git(
                ["rev-parse", "--verify", expected_ref], toplevel,
                run_command=run_command,
            )
            if head and expected:
                facts = {
                    "install": "editable", "source": str(toplevel),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "head": head,
                    **_branch_facts({
                        "origin_main": expected,
                        "expected_ref": expected_ref,
                        "expected": expected,
                    }),
                }
                stale_reason = (
                    None if head == expected else "head_is_not_engine_source"
                )
            else:
                facts = {
                    "install": "editable",
                    "source": str(toplevel or package_dir),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "reason": "unverifiable_git_state",
                }
                stale_reason = "unverifiable_git_state"
        else:
            resolved = _resolve_engine_source(
                track, toplevel, run_command=run_command,
            )
            if resolved is not None and head:
                facts = {
                    "install": "editable", "source": str(toplevel),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "head": head,
                    "resolved": resolved["resolved"],
                    "expected": resolved["expected"],
                }
                stale_reason = (
                    None if head == resolved["expected"]
                    else "head_is_not_engine_source"
                )
            else:
                facts = {
                    "install": "editable",
                    "source": str(toplevel or package_dir),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "reason": "unverifiable_engine_source",
                }
                stale_reason = "unverifiable_engine_source"
    else:
        try:
            version = _orbi_distribution_version()
        except Exception:
            version = None
        parsed_version = (
            _parse_release_version(version) if version else None
        )
        resolved = None
        if kind == "branch":
            expected_tag = _runner_source_git(
                [
                    "describe", "--tags", "--match", "v*", "--abbrev=0",
                    f"refs/remotes/origin/{argument}",
                ],
                deploy_home, run_command=run_command,
            )
        elif kind in ("release", "tag"):
            resolved = _resolve_engine_source(
                track, deploy_home, run_command=run_command,
            )
            expected_tag = resolved["tag"] if resolved else None
        else:
            # A sha: lock has no version mapping: unverifiable -> fail
            # closed.
            expected_tag = None
        parsed_tag = (
            _parse_release_version(expected_tag) if expected_tag else None
        )
        if parsed_version and parsed_tag:
            if kind == "branch" and argument == "main":
                comparison = {"origin_main": expected_tag}
            else:
                comparison = {
                    "resolved": (
                        resolved["resolved"] if resolved else expected_tag
                    ),
                }
            facts = {
                "install": "non_editable", "source": str(module_path),
                "engine_source_track": track,
                "delivery_base_branch": delivery_base_branch,
                "version": version,
                **comparison,
            }
            stale_reason = (
                None if parsed_version >= parsed_tag
                else "version_older_than_latest_tag"
            )
        else:
            facts = {
                "install": "non_editable", "source": str(module_path),
                "engine_source_track": track,
                "delivery_base_branch": delivery_base_branch,
                "reason": "unverifiable_version_state",
            }
            stale_reason = "unverifiable_version_state"

    if stale_reason is None:
        event("runner_source", result="fresh", **facts)
        return facts
    allowed = bool(config.allow_stale_runner)
    # The fix string names the editable reinstall, so building it decides
    # the compatible interpreter (a subprocess probe). It is built HERE —
    # only on the stale path that reports it — so a HEALTHY tick never
    # spawns a probe: the interpreter is decided when a reinstall is
    # actually asked for, never on every Runner start (Issue #2001).
    fix = cli_source.reinstall_command(deploy_home)
    event(
        "runner_source_stale",
        **facts, allowed=str(allowed).lower(), fix=fix,
        level=logging.WARNING if allowed else logging.ERROR,
    )
    if not allowed:
        raise RunnerSourceStaleError(
            _runner_source_stale_line(facts, allowed=allowed, fix=fix),
        )
    return facts
