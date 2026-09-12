"""Orbi engine source update channel (Issue #535).

The deploy home — the checkout the installed CLI executes from — follows
exactly ONE update channel, configured by the host/deploy-only key
``engine_source_track`` in the deploy home's ``orbi.toml``:

- absent / ``main``  — track ``origin/main`` and fast-forward (the exact
  pre-#535 dogfood behavior);
- ``branch:<name>``  — track ``origin/<name>`` and fast-forward;
- ``release``        — the newest official semver tag (``vX.Y.Z``;
  pre-releases are excluded), checked out detached; ``stable`` is an
  alias normalized to ``release`` at the parse entry (identical
  behavior, no second resolve path);
- ``tag:<name>``     — one exact tag (annotated tags are dereferenced to
  their commit), checked out detached;
- ``sha:<40-hex>``   — one exact commit, checked out detached.

The service ``ExecStartPre`` runs ``orbi sync-engine-source`` on every
tick (outside the Runner process, under the shared base-sync flock): it
fails closed — a dirty checkout, a missing tag/SHA, an unresolvable
channel or a head that cannot be verified against the resolved commit
never starts the service — and logs one structured
``engine_source_synced`` line on success (the track, the resolved
ref/tag and the HEAD SHA). Rolling back is editing the track back to
the previous tag/SHA; no other state exists.

This module owns only the ENGINE source channel. The delivery target's
``base_branch`` semantics are untouched (Issue #543 owns the isolation).
No database, queue, daemon or fallback: git state and the config key are
the only inputs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from orbi.progress import quote_value

LOGGER = logging.getLogger("orbi.engine_source")

# One git fetch inside one sync; the unit's ExecStartPre timeout (90 s)
# stays the outer bound and a timeout here is a fail-fast, never a retry.
FETCH_TIMEOUT_SECONDS = 60

TRACK_KEY = "engine_source_track"
TRACK_FORMS = (
    "'main', 'branch:<name>', 'release' (alias: 'stable'), "
    "'tag:<name>' or 'sha:<40-hex>'",
)
_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
# A conservative subset of git-check-ref-format: one path-style ref name
# with no whitespace, no glob/range magic and no leading/trailing junk.
_REF_NAME_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
_SEMVER_TAG_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class EngineSourceError(RuntimeError):
    """The engine source channel cannot be resolved or synced (the
    message IS the structured journal line, reason + fix included)."""


def _valid_ref_name(name: str) -> bool:
    """One branch/tag name git accepts without glob or range magic."""
    return (
        bool(name)
        and _REF_NAME_PATTERN.fullmatch(name) is not None
        and ".." not in name
        and "//" not in name
        and not name.startswith(("-", "/", "."))
        and not name.endswith(("/", "."))
        and not name.endswith(".lock")
    )


def normalize_engine_source_track(value: object) -> str:
    """Validate one ``engine_source_track`` config value; absent -> main.

    Anything that is not one of the documented forms raises ``ValueError``
    naming the accepted forms — the config load fails fast (Issue #535).
    """
    if value is None:
        return "main"
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{TRACK_KEY} must be a non-empty string: {TRACK_FORMS[0]}"
        )
    if value in ("main", "release", "stable"):
        return "release" if value == "stable" else value
    for prefix in ("branch:", "tag:"):
        if value.startswith(prefix):
            if not _valid_ref_name(value[len(prefix):]):
                raise ValueError(
                    f"{TRACK_KEY} {value!r}: the name after {prefix!r} must "
                    f"be one valid git ref name; accepted forms are "
                    f"{TRACK_FORMS[0]}"
                )
            return value
    if value.startswith("sha:"):
        if _SHA_PATTERN.fullmatch(value[4:]) is None:
            raise ValueError(
                f"{TRACK_KEY} {value!r}: sha mode requires a full "
                f"40-hex commit SHA; accepted forms are {TRACK_FORMS[0]}"
            )
        return value
    raise ValueError(
        f"{TRACK_KEY} {value!r} is not a valid track; accepted forms are "
        f"{TRACK_FORMS[0]}"
    )


def split_track(track: str) -> tuple[str, str | None]:
    """Split a validated track into (kind, argument).

    Kinds: ``branch`` (``main`` for the plain ``main`` track),
    ``release``, ``tag`` and ``sha``.
    """
    if track == "main":
        return "branch", "main"
    if track == "release":
        return "release", None
    kind, _, argument = track.partition(":")
    return kind, argument


def parse_semver_tag(tag: str) -> tuple[tuple[int, int, int], bool] | None:
    """Parse one ``vX.Y.Z[-pre][+build]`` tag into ((major, minor, patch),
    is_pre_release); None when the tag is not a semver version."""
    match = _SEMVER_TAG_PATTERN.match(tag)
    if match is None:
        return None
    version = (
        int(match["major"]), int(match["minor"]), int(match["patch"]),
    )
    return version, match["pre"] is not None


def is_official_release_tag(tag: str) -> bool:
    """True for a semver tag without a pre-release part (the default
    release-channel selection rule, Issue #535)."""
    parsed = parse_semver_tag(tag)
    return parsed is not None and not parsed[1]


def latest_release_tag(tags: list[str]) -> str | None:
    """The newest OFFICIAL semver tag by version order (numeric, so
    v0.10.0 beats v0.9.0); None when the tags carry no official release."""
    official = [
        (parse_semver_tag(tag)[0], tag)
        for tag in tags
        if is_official_release_tag(tag)
    ]
    if not official:
        return None
    return max(official, key=lambda entry: entry[0])[1]


def _probe(run_command: Callable[..., str], args: list[str],
           cwd: Path) -> str | None:
    """One LOCAL read-only git probe; None when git cannot answer."""
    try:
        return run_command(["git", *args], cwd=cwd, timeout=15)
    except Exception:
        return None


def resolve_expected_head(track: str, cwd: Path, *,
                          run_command: Callable[..., str]) -> dict:
    """Resolve the track's expected commit with LOCAL git reads only.

    The freshness gate (Issue #525) and ``orbi doctor`` judge the source
    through the refs/tags the ExecStartPre sync already fetched — no
    network here. Returns the resolved kind/argument, the display ref
    (or tag / sha) and the expected commit; an unresolvable channel
    raises :class:`EngineSourceError` with a structured reason.
    """
    kind, argument = split_track(track)
    if kind == "branch":
        ref = f"refs/remotes/origin/{argument}"
        expected = _probe(run_command, ["rev-parse", "--verify", ref], cwd)
        if expected is None:
            raise EngineSourceError(
                f"engine_source_unresolved engine_source_track={track} "
                f"reason=branch_ref_missing ref={ref} "
                f"fix=git -C {cwd} fetch --no-auto-maintenance origin "
                f"{argument}"
            )
        return {
            "kind": kind, "argument": argument,
            "resolved": ref, "expected": expected,
        }
    if kind == "release":
        tags = (run_command(
            ["git", "tag", "--list", "v*"], cwd=cwd, timeout=15,
        ) or "").split()
        tag = latest_release_tag(tags)
        if tag is None:
            raise EngineSourceError(
                f"engine_source_unresolved engine_source_track={track} "
                f"reason=no_official_release_tag "
                f"fix=git -C {cwd} fetch --tags origin and publish a "
                "vX.Y.Z release tag"
            )
        return _dereference_tag(track, tag, run_command=run_command, cwd=cwd)
    if kind == "tag":
        return _dereference_tag(
            track, argument, run_command=run_command, cwd=cwd,
        )
    expected = _probe(
        run_command, ["rev-parse", "--verify", f"{argument}^{{commit}}"], cwd,
    )
    if expected is None:
        raise EngineSourceError(
            f"engine_source_unresolved engine_source_track={track} "
            f"reason=sha_not_found sha={argument} fix=verify the SHA is a "
            f"commit reachable from a branch or tag of the engine source"
        )
    return {
        "kind": kind, "argument": argument,
        "resolved": argument, "expected": expected,
    }


def _dereference_tag(track: str, tag: str, *, run_command: Callable[..., str],
                     cwd: Path) -> dict:
    """One annotated tag -> its commit; a missing tag fails closed."""
    commit = _probe(
        run_command,
        ["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], cwd,
    )
    if commit is None:
        raise EngineSourceError(
            f"engine_source_unresolved engine_source_track={track} "
            f"reason=tag_not_found tag={tag} "
            f"fix=git -C {cwd} fetch --no-auto-maintenance origin "
            f"refs/tags/{tag}:refs/tags/{tag}"
        )
    return {
        "kind": split_track(track)[0], "argument": tag,
        "resolved": f"refs/tags/{tag}", "tag": tag, "expected": commit,
    }


def _fail_on_dirty(deploy_home: Path, track: str, *,
                   run_command: Callable[..., str]) -> None:
    """Fail closed on tracked local changes before ANY mutation (the
    same porcelain contract as the doctor's deploy-home check)."""
    status = run_command(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=deploy_home, timeout=15,
    )
    # run_command strips the stdout, so the first porcelain line can lose
    # its leading X field: slice the path from column 2 and strip, never
    # from column 3 of the raw line.
    files = [
        line[2:].strip() for line in status.splitlines()
        if len(line) >= 3 and line[:2] != "??"
    ]
    if files:
        raise EngineSourceError(_deploy_home_dirty_line(deploy_home, files))


def _deploy_home_dirty_line(deploy_home: Path, files: list[str]) -> str:
    fix = (
        f"git -C {deploy_home} stash && "
        "systemctl --user start orbi@1.service"
    )
    return (
        f"deploy_home_dirty files={quote_value(','.join(files))} "
        f"fix={quote_value(fix)}"
    )


def _checkout_locked_head(deploy_home: Path, track: str, expected: str, *,
                          run_command: Callable[..., str]) -> str:
    """Detach onto the resolved commit (idempotent) and return the head."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=deploy_home)
    if head != expected:
        run_command(
            ["git", "checkout", "--detach", expected], cwd=deploy_home,
        )
        head = run_command(["git", "rev-parse", "HEAD"], cwd=deploy_home)
    if head != expected:
        raise EngineSourceError(
            f"engine_source_unverified engine_source_track={track} "
            f"head={head} expected={expected} "
            f"fix=git -C {deploy_home} checkout --detach {expected}"
        )
    return head


def _sync_branch(deploy_home: Path, track: str, branch: str, *,
                 run_command: Callable[..., str]) -> dict:
    """Branch/main mode: fetch, land on the branch, fast-forward, verify."""
    try:
        run_command(
            ["git", "fetch", "--no-auto-maintenance", "origin", branch],
            cwd=deploy_home, timeout=FETCH_TIMEOUT_SECONDS,
        )
    except Exception:
        # A ref the remote does not have fails the FETCH itself; keep the
        # structured reason instead of the raw git error (fail closed).
        if _probe(
            run_command, ["rev-parse", "--verify",
                          f"refs/remotes/origin/{branch}"],
            deploy_home,
        ) is None:
            raise EngineSourceError(
                f"engine_source_unresolved engine_source_track={track} "
                f"reason=branch_ref_missing ref=refs/remotes/origin/{branch} "
                f"fix=check the branch name exists on the engine source "
                f"remote"
            ) from None
        raise
    resolved = resolve_expected_head(track, deploy_home, run_command=run_command)
    expected = resolved["expected"]
    head = run_command(["git", "rev-parse", "HEAD"], cwd=deploy_home)
    if head != expected:
        local_branch = (
            _probe(run_command,
                   ["rev-parse", "--verify", f"refs/heads/{branch}"],
                   deploy_home)
        )
        if local_branch is not None:
            run_command(["git", "checkout", branch], cwd=deploy_home)
            try:
                run_command(
                    ["git", "merge", "--ff-only", resolved["resolved"]],
                    cwd=deploy_home,
                )
            except Exception:
                raise EngineSourceError(
                    f"engine_source_not_fast_forwardable "
                    f"engine_source_track={track} head={head} "
                    f"expected={expected} "
                    f"fix=git -C {deploy_home} reset --hard "
                    f"{resolved['resolved']}"
                ) from None
        else:
            run_command(
                ["git", "checkout", "-b", branch, resolved["resolved"]],
                cwd=deploy_home,
            )
        head = run_command(["git", "rev-parse", "HEAD"], cwd=deploy_home)
    if head != expected:
        raise EngineSourceError(
            f"engine_source_unverified engine_source_track={track} "
            f"head={head} expected={expected} "
            f"fix=git -C {deploy_home} reset --hard "
            f"{resolved['resolved']}"
        )
    LOGGER.info(
        "engine_source_synced engine_source_track=%s ref=%s head=%s",
        track, resolved["resolved"], head,
    )
    return {
        "track": track, "resolved": resolved["resolved"], "head": head,
    }


def _fetch_exact_tag(deploy_home: Path, track: str, tag: str, *,
                     run_command: Callable[..., str]) -> None:
    """Fetch one exact tag; missing remotely vs conflicting locally are
    two distinct structured reasons (both fail closed)."""
    try:
        run_command(
            ["git", "fetch", "--no-auto-maintenance", "origin",
             f"refs/tags/{tag}:refs/tags/{tag}"],
            cwd=deploy_home, timeout=FETCH_TIMEOUT_SECONDS,
        )
    except Exception:
        local = _probe(
            run_command, ["rev-parse", "--verify", f"refs/tags/{tag}"],
            deploy_home,
        )
        reason = "tag_conflict" if local is not None else "tag_not_found"
        raise EngineSourceError(
            f"engine_source_unresolved engine_source_track={track} "
            f"reason={reason} tag={tag} "
            f"fix=git -C {deploy_home} fetch --no-auto-maintenance origin "
            f"refs/tags/{tag}:refs/tags/{tag}"
        ) from None


def sync_engine_source(deploy_home: Path, track: str, *,
                       run_command: Callable[..., str]) -> dict:
    """Bring the deploy home checkout to the configured engine source
    channel and verify the head (the ExecStartPre contract, Issue #535).

    Dirty checkouts fail closed before any mutation; tag/sha channels
    land on a detached HEAD at the exact commit; branch channels land on
    the branch and fast-forward. Returns the structured facts (track,
    resolved ref/tag, head) for the caller's report.
    """
    deploy_home = Path(deploy_home)
    kind, argument = split_track(track)
    _fail_on_dirty(deploy_home, track, run_command=run_command)
    if kind == "branch":
        return _sync_branch(
            deploy_home, track, argument, run_command=run_command,
        )
    if kind == "release":
        run_command(
            ["git", "fetch", "--no-auto-maintenance", "origin",
             "refs/tags/v*:refs/tags/v*"],
            cwd=deploy_home, timeout=FETCH_TIMEOUT_SECONDS,
        )
    elif kind == "tag":
        _fetch_exact_tag(
            deploy_home, track, argument, run_command=run_command,
        )
    else:
        # GitHub rejects fetching unadvertised SHAs (`not our ref`), so
        # the sha channel fetches every advertised ref once and verifies
        # the commit locally; an unreachable SHA fails closed.
        run_command(
            ["git", "fetch", "--no-auto-maintenance", "origin"],
            cwd=deploy_home, timeout=FETCH_TIMEOUT_SECONDS,
        )
    resolved = resolve_expected_head(track, deploy_home, run_command=run_command)
    head = _checkout_locked_head(
        deploy_home, track, resolved["expected"], run_command=run_command,
    )
    fields = (
        f"engine_source_synced engine_source_track={track} "
        f"resolved={resolved['resolved']} head={head}"
    )
    LOGGER.info("%s", fields)
    return {
        "track": track, "resolved": resolved["resolved"], "head": head,
    }


def engine_source_status(deploy_home: Path, track: str, *,
                         run_command: Callable[..., str]) -> dict:
    """Read-only channel status for the doctor report: the track, the
    resolved ref/tag, the HEAD SHA and whether the checkout is on the
    resolved commit. Never fetches; an unresolvable channel reports
    FAILED with the structured reason instead of raising."""
    deploy_home = Path(deploy_home)
    track = normalize_engine_source_track(track)
    head = _probe(run_command, ["rev-parse", "HEAD"], deploy_home)
    try:
        resolved = resolve_expected_head(
            track, deploy_home, run_command=run_command,
        )
    except EngineSourceError as exc:
        return {
            "track": track, "resolved": "-", "expected": None,
            "head": head, "ok": False, "error": str(exc),
        }
    return {
        "track": track,
        "resolved": resolved["resolved"],
        "expected": resolved["expected"],
        "head": head,
        "ok": head == resolved["expected"],
        "error": None,
    }
