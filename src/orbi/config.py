"""Host configuration parsing and validation.

This module deliberately has no dependency on the delivery runner.
"""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from orbi import engine_source
from orbi.pi_process import PI_MODEL_WAIT_DEAD_SECONDS, PI_MODEL_WAIT_PROBE_SECONDS
from orbi.pilot_slots import slot_dir_for
from orbi.release import RELEASE_CI_WAIT_SECONDS, RELEASE_DELIVERIES_WAIT_SECONDS
from orbi.release_git import RELEASE_VERSION_FILE_OPTIONS
from orbi.repo_config import REPO_CONFIG_PATH
from orbi.scheduler import MAX_RUNNER_INSTANCES

ISSUE_COMMENTS_LIMIT = 200
WORKTREE_RETAIN_HOURS = 72

def _config_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def _prompt_config_path(value: str, base: Path, legacy_name: str) -> Path:
    """Resolve a prompt path, retaining explicit legacy basename configs."""
    path = _config_path(value, base)
    if Path(value).as_posix() == legacy_name and not path.exists():
        migrated = _config_path(f"prompts/{legacy_name}", base)
        if migrated.exists():
            return migrated
    return path


def _load_deploy_env_file(deploy_home: Path) -> None:
    """Merge `<deploy_home>/.orbi/env` into the process environment.

    The documented env-file-first flow (getting-started step 4) writes the
    provider key to this gitignored file, and the installed unit loads it
    via `EnvironmentFile` at service start — the CLI process does not, so
    `orbi setup` and friends must read it themselves or the documented
    step 4 -> 5 flow fails verbatim. Plain systemd EnvironmentFile syntax:
    `KEY=VALUE` lines, optional `export ` prefix, matching single/double
    quotes stripped, blank lines and `#` comments skipped. A variable
    already exported in the shell wins (`setdefault`): the shell export is
    the documented override for manual ticks. A missing file is a no-op
    (a keyless local server needs no env file at all); a line without `=`
    is a misconfiguration and fails fast.
    """
    env_file = deploy_home / ".orbi" / "env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        if "=" not in stripped:
            raise ValueError(
                f"malformed line in env file {env_file}: {stripped!r} "
                "(expected KEY=VALUE)"
            )
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('"', "'")
        ):
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class RunnerConfig:
    """The typed host configuration.

    :func:`load_config` is the ONLY constructor from raw TOML; derived
    instances (the per-run values and the repository-policy overlay) are
    produced with :func:`dataclasses.replace`, never by re-assembly.
    Every module consumes it through attribute access. Fields the host
    config does not carry carry the same defaults the former bare dict's
    ``config.get(key, default)`` fallbacks used, so a hand-built (test)
    config keeps the exact pre-#790 behavior.
    """

    # Per-run delivery values, bound by the tick/process_issue with
    # `replace` right before the implementer/review session runs; ""
    # is the unbound placeholder (never read before the binding).
    run_id: str = ""
    base_sha: str = ""
    # Repository-policy overlay: written only by
    # `repo_config.resolve_policy` — `dispatch_label` is a
    # repository-declared key with no host equivalent.
    repo_context_files: tuple[str, ...] = ()
    dispatch_label: str | None = None
    # Host config (load_config output). The Path fields and the two
    # string identity fields below carry placeholder defaults ("." /
    # "main" / "ssh") ONLY so a hand-built partial config stays
    # constructible: load_config — the sole real constructor — sets every
    # one of them explicitly, and a path that reads a field a hand-built
    # config never set failed with a KeyError before #790.
    config_path: Path = Path(".")
    source_repos: tuple[str, ...] = ()
    repo_dir: Path = Path(".")
    deploy_home: Path = Path(".")
    unit_name: str | None = None
    health_alert_repo: str | None = None
    workspace_root: Path = Path(".")
    prompt: Path = Path(".")
    prompt_review: Path = Path(".")
    skills: tuple[Path, ...] = ()
    context_files: tuple[Path, ...] = ()
    base_branch: str = "main"
    git_transport: str = "ssh"
    engine_source_track: str | None = None
    active_milestone: str | None = None
    auto_next_milestone: bool = True
    # ``/milestone`` release-ticket generation: the version file the release
    # state machine bumps. Host-only (a repository policy cannot route a
    # release), absent -> detected in the repository -> when detection finds
    # none the release-ticket step fails with a receipt naming the fix instead
    # of guessing ``pyproject.toml``.
    version_file: str | None = None
    max_concurrency: int = 1
    allow_stale_runner: bool = False
    human_review_gate: bool = False
    attribution_footer: bool = True
    slot_dir: Path | None = None
    pi_provider: str | None = None
    pi_model: str | None = None
    pi_thinking: str | None = None
    review_pi_provider: str | None = None
    review_pi_model: str | None = None
    review_pi_thinking: str | None = None
    pi_extensions: tuple[dict, ...] = ()
    model_wait_dead_seconds: float = PI_MODEL_WAIT_DEAD_SECONDS
    issue_comments_limit: int = ISSUE_COMMENTS_LIMIT
    worktree_retain_hours: float = WORKTREE_RETAIN_HOURS
    model_wait_probe_url: str | None = None
    model_wait_probe_seconds: float = PI_MODEL_WAIT_PROBE_SECONDS
    steering_enabled: bool = True
    steering_poll_seconds: float = 60.0
    steering_max_rounds: int = 3
    release_ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS
    release_deliveries_wait_seconds: float = RELEASE_DELIVERIES_WAIT_SECONDS
    pi_providers: Path | None = None
    pi_providers_data: dict | None = None
    pi_provider_key_finding: dict | None = None
    # Multi-repo registry: the explicit per-repo entries
    # (name, path, github, base_branch). Empty -> the single-repo config.
    repositories: tuple[dict, ...] = ()


class ConfigFileMissingError(FileNotFoundError):
    """The configured ``orbi.toml`` itself does not exist."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        super().__init__(self.path)


def load_config(path: Path, *, check_provider_api_keys: bool = True,
                allow_missing_pi_providers: bool = False) -> RunnerConfig:
    """Load the human-maintained TOML config and resolve its paths.

    ``doctor`` disables the selected provider-key gate so it can report the
    configuration finding instead of being stopped by it.
    """
    base = path.resolve().parent
    if not path.exists():
        raise ConfigFileMissingError(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    source_repos = data.get("source_repos")
    if not isinstance(source_repos, list) or not source_repos:
        raise ValueError("source_repos must be a non-empty list")
    if not all(isinstance(repo, str) and repo for repo in source_repos):
        raise ValueError("source_repos must contain non-empty strings")
    unit_name = data.get("unit_name")
    if unit_name is not None and (
        not isinstance(unit_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", unit_name)
    ):
        raise ValueError("unit_name must contain only letters, numbers, '-' or '_'")
    base_branch = data.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("base_branch must be a non-empty string")
    # Delivery transport: how the checkout's git data
    # operations (fetch/push) authenticate. Absent -> "ssh" (the exact
    # pre-#580 contract); "https" keeps the origin on the HTTPS URL and
    # authenticates via the gh credential helper (the token-only
    # sandbox path). Anything else is a misconfiguration: fail fast.
    git_transport = data.get("git_transport", "ssh")
    if git_transport not in ("ssh", "https"):
        raise ValueError("git_transport must be 'ssh' or 'https'")
    # Claim scope: the active Milestone is an EXPLICIT
    # version scope for the fresh-claim scans — it is never guessed
    # from the repo's Milestone list. Absent (None) keeps the current
    # behavior exactly (compat); present it must be a non-empty
    # string, otherwise the config is a misconfiguration and the start
    # fails fast.
    active_milestone = data.get("active_milestone")
    if active_milestone is not None and (
        not isinstance(active_milestone, str) or not active_milestone
    ):
        raise ValueError("active_milestone must be a non-empty string")
    auto_next_milestone = data.get("auto_next_milestone", True)
    if not isinstance(auto_next_milestone, bool):
        raise ValueError("auto_next_milestone must be a boolean")
    # `/milestone` release-ticket generation: the version file the release
    # state machine bumps. Absent -> detected in the repository, then the
    # release-ticket step fails when detection finds none (Issue #1307, never
    # a `pyproject.toml` guess). A declared value must be one the release
    # parser knows.
    version_file = data.get("version_file")
    if version_file is not None and version_file not in RELEASE_VERSION_FILE_OPTIONS:
        raise ValueError(
            "version_file must be one of "
            + ", ".join(RELEASE_VERSION_FILE_OPTIONS)
        )
    # Startup source freshness: the Runner refuses to claim
    # when the code it executes is not the origin/main head.
    # This flag is the EXPLICIT degraded mode for offline/restricted-
    # network deployments — it only downgrades the gate to a warning,
    # never skips it. Default False = fail fast.
    allow_stale_runner = data.get("allow_stale_runner", False)
    if not isinstance(allow_stale_runner, bool):
        raise ValueError("allow_stale_runner must be a boolean")
    # Human acceptance gate: when true, every delivery's
    # PR carries an acceptance checklist on the Issue and the review
    # round waits for the human-only `ai-human-review` label while the
    # checklist's column 2 (the machine-unverifiable minimum) is
    # non-empty. HOST-only like `allow_stale_runner`: the gate is the
    # deployment operator's trust decision (who pays for the pipeline
    # decides when a person must look), never a repository-writable
    # policy. Default False = the exact pre-#763 behavior.
    human_review_gate = data.get("human_review_gate", False)
    if not isinstance(human_review_gate, bool):
        raise ValueError("human_review_gate must be a boolean")
    attribution_footer = data.get("attribution_footer", True)
    if not isinstance(attribution_footer, bool):
        raise ValueError("attribution_footer must be a boolean")
    # Engine source update channel: what the deploy home
    # checkout follows at the next start — origin/main by default (the
    # exact pre-#535 dogfood behavior), a branch, the newest official
    # release tag, one exact tag or one exact commit. Host/deploy-only:
    # a repository's .github/orbi.toml can never carry it. An invalid
    # value fails the config load fast.
    engine_source_track = engine_source.normalize_engine_source_track(
        data.get("engine_source_track"),
    )
    # Concurrency cap: the local machine can only serve a
    # limited number of concurrent tasks, so the default is 1. Any other
    # value must be a positive integer within the MAX_RUNNER_INSTANCES
    # declaration cap (Issue #827 — the cap is a constant, never derived
    # from a unit-name list); fail fast on anything else.
    max_concurrency = data.get("max_concurrency", 1)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= MAX_RUNNER_INSTANCES
    ):
        # Issue #829: the error names the CURRENT value too — the
        # operator must see what the config wrote, not just the range.
        raise ValueError(
            "max_concurrency must be a positive integer no greater than "
            f"{MAX_RUNNER_INSTANCES} (MAX_RUNNER_INSTANCES); "
            f"got {max_concurrency!r}"
        )
    # Optional Pi model selection: each key is absent -> None
    # (the Pi flag is not passed, Pi keeps its own default) or a non-empty
    # string passed to Pi verbatim. Anything else fails fast.
    pi_provider = _optional_pi_string(data, "pi_provider")
    pi_model = _optional_pi_string(data, "pi_model")
    pi_thinking = _optional_pi_string(data, "pi_thinking")
    review_pi_provider = _optional_pi_string(data, "review_pi_provider")
    review_pi_model = _optional_pi_string(data, "review_pi_model")
    review_pi_thinking = _optional_pi_string(data, "review_pi_thinking")
    pi_extensions = _load_pi_extensions(data.get("pi_extensions"), base)
    # Hung-model-request threshold: the model_wait dead
    # silence is configurable; omitted -> PI_MODEL_WAIT_DEAD_SECONDS
    # (default 1800 s, 30 minutes). It measures silence between
    # complete session events, never token-level model progress.
    model_wait_dead_seconds = _positive_seconds(
        data, "model_wait_dead_seconds", PI_MODEL_WAIT_DEAD_SECONDS,
    )
    # Trusted-comment injection cap: how many of the
    # Issue's trusted comments enter the agent's task context.
    issue_comments_limit = _issue_comments_limit(data)
    # Task-worktree reclamation: how long a closed Issue's
    # worktree stays inspectable before the tick start removes it.
    worktree_retain_hours = _worktree_retain_hours(data)
    # Swallowed-model-request probe: the /slots endpoint
    # (optional) and its sustained-idle grace (default 60 s). Absent URL
    # -> the probe is disabled (the exact pre-#233 behavior: the run is
    # bounded by model_wait_dead_seconds only).
    model_wait_probe_url = _model_wait_probe_url(data)
    model_wait_probe_seconds = _positive_seconds(
        data, "model_wait_probe_seconds", PI_MODEL_WAIT_PROBE_SECONDS,
    )
    steering_enabled = data.get("steering_enabled", True)
    if not isinstance(steering_enabled, bool):
        raise ValueError("steering_enabled must be a boolean")
    steering_poll_seconds = _positive_seconds(data, "steering_poll_seconds", 60.0)
    steering_max_rounds = data.get("steering_max_rounds", 3)
    if (isinstance(steering_max_rounds, bool)
            or not isinstance(steering_max_rounds, int)
            or steering_max_rounds < 0):
        raise ValueError("steering_max_rounds must be a non-negative integer")
    # Release CI wait: the release gate's in-tick upper
    # bound for pending checks on the release commit. The DELIVERY path
    # has no CI wait anymore: a pending check defers the
    # delivery to the next tick, so this bound is the release state
    # machine's pure cap, never a delivery-wait mechanism.
    release_ci_wait_seconds = _positive_seconds(
        data, "release_ci_wait_seconds", RELEASE_CI_WAIT_SECONDS,
    )
    release_deliveries_wait_seconds = _positive_seconds(
        data, "release_deliveries_wait_seconds",
        RELEASE_DELIVERIES_WAIT_SECONDS,
    )
    # Runner-self health alert routing: the orbi repo that
    # receives the watchdog's crash_loop / stale_pickup Issues. Absent ->
    # None (the Runner derives the orbi repo from the deploy home's git
    # origin); present -> must be a non-empty `owner/repo` string, used
    # verbatim for fork/private deployments.
    health_alert_repo = _optional_pi_string(data, "health_alert_repo")
    repo_dir = _config_path(data.get("repo_dir", "."), base)
    # Deployment home: the orbi source checkout — the editable
    # CLI install source, the systemd/ unit templates, labels.toml and the
    # prompt defaults. Absent -> repo_dir (the orbi-bootstrap deployment,
    # home == delivery checkout, keeps its exact behavior). Present -> must
    # be a non-empty string, resolved like every other config path; the
    # delivery checkout (repo_dir) is then decoupled from the CLI
    # self-update and the startup gates act on the home only.
    deploy_home_raw = data.get("deploy_home")
    if deploy_home_raw is not None and (
        not isinstance(deploy_home_raw, str) or not deploy_home_raw
    ):
        raise ValueError("deploy_home must be a non-empty string")
    deploy_home = (
        _config_path(deploy_home_raw, base)
        if deploy_home_raw is not None
        else repo_dir
    )
    # The deploy-home env file: step 4 of getting-started
    # writes the provider key to `<deploy_home>/.orbi/env` and the
    # installed unit loads it via `EnvironmentFile` at service start —
    # the CLI process does not. Load it here so `orbi setup` (and every
    # other CLI entry) validates the key exactly like the unit would.
    _load_deploy_env_file(deploy_home)
    # Optional Pi provider file: the provider metadata
    # (baseUrl / api / apiKey / models) lives in a separate JSON file in
    # Pi's own `models.json` shape; `orbi.toml` only selects the
    # provider/model/thinking used at runtime. Absent key -> None (Pi
    # keeps using its own agent dir, the exact pre-#157 behavior).
    pi_providers = _optional_pi_string(data, "pi_providers")
    pi_providers_path = (
        _config_path(pi_providers, base) if pi_providers is not None
        else None
    )
    _load_pi_providers.last_key_finding = None
    pi_providers_data = None
    if pi_providers_path is not None:
        try:
            env_file = deploy_home / ".orbi" / "env"
            pi_providers_data = _load_pi_providers(
                pi_providers_path, pi_provider, pi_model, env_file,
                check_api_key=check_provider_api_keys,
            )
            try:
                _load_pi_providers(
                    pi_providers_path,
                    review_pi_provider or pi_provider,
                    review_pi_model or pi_model,
                    env_file,
                    check_api_key=check_provider_api_keys,
                )
            except ValueError as exc:
                raise ValueError(f"review provider selection invalid: {exc}") from exc
        except FileNotFoundError:
            # A missing path is the setup/doctor diagnostic case.  Preserve
            # fail-fast behavior for an existing non-file path (for example,
            # a directory), which is an invalid provider configuration.
            if not allow_missing_pi_providers or pi_providers_path.exists():
                raise
            _load_pi_providers.last_key_finding = {
                "provider": pi_provider or "-",
                "variable": "-",
                "path": pi_providers_path,
                "env_file": deploy_home / ".orbi" / "env",
                "state": "file missing",
            }
    return RunnerConfig(
        config_path=path.resolve(),
        source_repos=tuple(source_repos),
        repo_dir=repo_dir,
        deploy_home=deploy_home,
        unit_name=unit_name,
        health_alert_repo=health_alert_repo,
        workspace_root=_config_path(data.get("workspace_root", ".."), base),
        # When deploy_home is EXPLICIT the prompt defaults
        # live in the deployment home (the delivery checkout may be a
        # foreign repo without them); an explicit prompt path still
        # resolves against the config file dir. deploy_home absent ->
        # the original config-file-dir resolution (bootstrap unchanged).
        prompt=_prompt_config_path(
            data.get("prompt", "prompts/prompt.md"),
            base if "prompt" in data
            else (deploy_home if deploy_home_raw is not None else base),
            "prompt.md",
        ),
        prompt_review=_prompt_config_path(
            data.get("prompt_review", "prompts/prompt_review.md"),
            base if "prompt_review" in data
            else (deploy_home if deploy_home_raw is not None else base),
            "prompt_review.md",
        ),
        skills=tuple(_config_path(item, base) for item in data.get("skills", [])),
        context_files=tuple(
            _config_path(item, base) for item in data.get("context_files", [])
        ),
        base_branch=base_branch,
        git_transport=git_transport,
        engine_source_track=engine_source_track,
        active_milestone=active_milestone,
        auto_next_milestone=auto_next_milestone,
        version_file=version_file,
        max_concurrency=max_concurrency,
        allow_stale_runner=allow_stale_runner,
        human_review_gate=human_review_gate,
        attribution_footer=attribution_footer,
        slot_dir=slot_dir_for(repo_dir),
        pi_provider=pi_provider,
        pi_model=pi_model,
        pi_thinking=pi_thinking,
        review_pi_provider=review_pi_provider,
        review_pi_model=review_pi_model,
        review_pi_thinking=review_pi_thinking,
        pi_extensions=tuple(pi_extensions),
        model_wait_dead_seconds=model_wait_dead_seconds,
        issue_comments_limit=issue_comments_limit,
        worktree_retain_hours=worktree_retain_hours,
        model_wait_probe_url=model_wait_probe_url,
        model_wait_probe_seconds=model_wait_probe_seconds,
        steering_enabled=steering_enabled,
        steering_poll_seconds=steering_poll_seconds,
        steering_max_rounds=steering_max_rounds,
        release_ci_wait_seconds=release_ci_wait_seconds,
        release_deliveries_wait_seconds=release_deliveries_wait_seconds,
        pi_providers=pi_providers_path,
        pi_providers_data=pi_providers_data,
        pi_provider_key_finding=getattr(
            _load_pi_providers, "last_key_finding", None,
        ),
        # Multi-repo registry: the explicit per-repo entries
        # (name, path, github, base_branch). Absent section -> () so the
        # single-repo config keeps its exact shape and flow.
        repositories=tuple(
            parse_repositories(data.get("repositories", []), base)
        ),
    )


def _optional_pi_string(data: dict, key: str) -> str | None:
    """Read one optional Pi model key.

    Absent -> None (the corresponding `pi --provider/--model/--thinking`
    flag is not passed and Pi keeps its own default). Present -> must be a
    non-empty string, passed to Pi verbatim; anything else fails fast.
    """
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _load_pi_extensions(value: object, base: Path) -> list[dict]:
    """Validate the extensions owned by an Orbi Pi run.

    Package sources must be reproducible: npm sources end in a concrete
    semver and git sources carry a non-empty ref after ``#``.  Other sources
    are repository-relative local files/directories.  Values are normalized
    once at config load so implement and review cannot diverge.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("pi_extensions must be an array of tables")
    result: list[dict] = []
    seen: set[str] = set()
    env_values: dict[str, str] = {}
    semver = re.compile(r"@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"pi_extensions[{index}] must be a table")
        source = item.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError(f"pi_extensions[{index}].source must be a non-empty string")
        if source.startswith("npm:"):
            if not semver.search(source):
                raise ValueError(f"pi_extensions[{index}] npm source must pin a version")
            normalized = source
        elif source.startswith("git:") or source.startswith(
            ("http://", "https://", "ssh://", "git://")
        ):
            # Pi's documented git source syntax uses an @ separator for
            # the pinned ref (for example git:github.com/org/repo@v1).
            # Require that same syntax here so validation does not accept a
            # source that Pi cannot resolve as a git package.
            if "@" not in source or not source.rsplit("@", 1)[1]:
                raise ValueError(
                    f"pi_extensions[{index}] git source must pin a ref with @"
                )
            normalized = source
        else:
            local = _config_path(source, base)
            if not local.exists():
                raise ValueError(f"pi_extensions[{index}] local source does not exist: {local}")
            normalized = str(local)
        if normalized in seen:
            raise ValueError(f"pi_extensions contains duplicate source: {source}")
        seen.add(normalized)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"pi_extensions[{index}].enabled must be a boolean")
        env = item.get("env", {})
        if not isinstance(env, dict):
            raise ValueError(f"pi_extensions[{index}].env must be a table")
        clean_env: dict[str, str] = {}
        for name, env_value in env.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"pi_extensions[{index}].env has invalid variable name {name!r}")
            if not isinstance(env_value, str):
                raise ValueError(f"pi_extensions[{index}].env.{name} must be a string")
            previous = env_values.get(name)
            if previous is not None and previous != env_value:
                raise ValueError(f"pi_extensions env conflict for {name}")
            env_values[name] = env_value
            clean_env[name] = env_value
        result.append({"source": normalized, "enabled": enabled, "env": clean_env})
    return result


def _positive_seconds(data: dict, key: str, default: float) -> float:
    """Load and validate a finite positive number of seconds.

    Omitted -> `default`. Present -> must be an int or float that is
    finite and > 0; booleans, non-numeric values, NaN, infinity, zero and
    negatives fail fast at config load with the field name and the
    concrete reason. The single validator for every "seconds" host
    setting (Issue #610).
    """
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ValueError(
            f"{key} must be a number, not a boolean (got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"{key} must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"{key} must be a finite number of seconds (got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            f"{key} must be a positive number of seconds (got {value!r})"
        )
    return number


def _model_wait_probe_url(data: dict) -> str | None:
    """Load and validate the optional `model_wait_probe_url`.

    Omitted -> None (the /slots probe is disabled: the run is bounded by
    `model_wait_dead_seconds` only, the exact pre-#233 behavior). Present
    -> must be a non-empty `http://` or `https://` URL (the model's
    `/slots` endpoint, e.g. `http://127.0.0.1:18082/slots`); anything else
    fails fast at config load with the field name and the concrete reason.
    """
    value = data.get("model_wait_probe_url")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(
            "model_wait_probe_url must be a non-empty string "
            f"(got {type(value).__name__} {value!r})"
        )
    if not value.startswith(("http://", "https://")):
        raise ValueError(
            "model_wait_probe_url must be an http:// or https:// URL "
            f"(got {value!r})"
        )
    return value


def _worktree_retain_hours(data: dict) -> float:
    """Load and validate the optional `worktree_retain_hours` (Issue
    #760).

    Omitted -> `WORKTREE_RETAIN_HOURS` (default 72 hours): a closed
    Issue's scene stays inspectable for three days before the tick-start
    reclamation removes it. Present -> must be a finite positive number
    (int or float); booleans, zero, negative, NaN/infinity and
    non-numeric values fail fast at config load with the field name and
    the concrete reason.
    """
    value = data.get("worktree_retain_hours", WORKTREE_RETAIN_HOURS)
    if isinstance(value, bool):
        raise ValueError(
            "worktree_retain_hours must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "worktree_retain_hours must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            "worktree_retain_hours must be a finite number of hours "
            f"(got {value!r})"
        )
    if math.isinf(number):
        raise ValueError(
            "worktree_retain_hours must be a finite number of hours "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "worktree_retain_hours must be a positive number of hours "
            f"(got {value!r})"
        )
    return number


def _issue_comments_limit(data: dict) -> int:
    """Load and validate the optional `issue_comments_limit` (Issue
    #745).

    Omitted -> `ISSUE_COMMENTS_LIMIT` (default 200). Present -> must be
    a positive integer; booleans, fractional and non-numeric values
    fail fast at config load with the field name and the concrete
    reason.
    """
    value = data.get("issue_comments_limit", ISSUE_COMMENTS_LIMIT)
    if isinstance(value, bool):
        raise ValueError(
            "issue_comments_limit must be a positive integer, not a "
            f"boolean (got {value!r})"
        )
    if not isinstance(value, int):
        raise ValueError(
            "issue_comments_limit must be a positive integer "
            f"(got {type(value).__name__} {value!r})"
        )
    if value <= 0:
        raise ValueError(
            "issue_comments_limit must be a positive integer "
            f"(got {value!r})"
        )
    return value


def _load_pi_providers(path: Path, pi_provider: str | None,
                       pi_model: str | None, env_file: Path, *,
                       check_api_key: bool = True) -> dict:
    """Load and validate the Pi provider file.

    The file uses Pi's own `models.json` shape (`{"providers": {id:
    {baseUrl, api, apiKey, models: [...]}}}`) — verified against the
    installed Pi 0.84.3 docs (`docs/models.md`). Fail fast with a
    specific message: file missing, invalid JSON, missing `providers`
    object, a provider entry with `models` but no `baseUrl` or no
    `api` (provider- or model-level — Pi's own schema requirement),
    the selected provider/model not defined in the file, or an
    `apiKey` env-var reference (`$VAR` / `${VAR}`) whose variable is
    missing or empty. The key value itself is never logged; only the
    variable name is named in the error.
    """
    _load_pi_providers.last_key_finding = None
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"pi_providers file {path} is not valid JSON: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    for provider_id, entry in providers.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"pi_providers file {path}: provider {provider_id!r} "
                "must be an object"
            )
        models = entry.get("models")
        if models is not None:
            if not isinstance(models, list) or not models:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "models must be a non-empty list"
                )
            if not isinstance(entry.get("baseUrl"), str) or not entry["baseUrl"]:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "is missing baseUrl"
                )
            if not isinstance(entry.get("api"), str) or not entry["api"]:
                if not all(
                    isinstance(model, dict)
                    and isinstance(model.get("api"), str)
                    and model["api"]
                    for model in models
                ):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} is missing api"
                    )
            for model in models:
                if not isinstance(model, dict) or not model.get("id"):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} has a model without an id"
                    )
    if pi_provider is not None:
        if pi_provider not in providers:
            raise ValueError(
                f"pi_provider {pi_provider!r} is not defined in "
                f"pi_providers file {path}"
            )
        entry = providers[pi_provider]
        # Only the SELECTED provider's key must resolve: an unselected
        # provider with a missing key just stays unavailable in Pi
        # (verified against real Pi 0.84.3), it never breaks the run.
        finding = _pi_provider_api_key_finding(
            path, pi_provider, entry, env_file,
        )
        _load_pi_providers.last_key_finding = finding
        if finding and check_api_key:
            raise ValueError(finding["error"])
        if pi_model is not None:
            model_ids = [
                model["id"] for model in entry.get("models", [])
            ] if isinstance(entry.get("models"), list) else []
            if pi_model not in model_ids:
                raise ValueError(
                    f"pi_model {pi_model!r} is not defined for provider "
                    f"{pi_provider!r} in pi_providers file {path}"
                )
    return data


def _pi_provider_api_key_finding(path: Path, provider_id: str,
                                  entry: dict, env_file: Path) -> dict | None:
    """Return an unresolved selected-provider key finding, without key data."""
    api_key = entry.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        return None
    for match in re.finditer(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        api_key,
    ):
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            state = "is not set"
        elif os.environ[name] == "":
            state = "is set but empty"
        else:
            continue
        return {
            "provider": provider_id, "variable": name, "path": path,
            "env_file": env_file,
            "state": state,
            "error": (
                f"API key for provider {provider_id!r} references "
                f"environment variable {name} {state} "
                f"(pi_providers file {path}). Export {name} in your "
                f"shell or add it to {env_file} (the unit's EnvironmentFile)."
            ),
        }
    return None


def _expand_pi_api_key_refs(api_key: str) -> str:
    """Resolve `$VAR` / `${VAR}` references in an `apiKey`.

    Same reference syntax `_pi_provider_api_key_finding` validates (Pi's
    `docs/models.md`): every reference whose environment variable is
    set and non-empty is replaced by the real value; a reference whose
    variable is missing or empty — only possible for a non-selected
    provider, the selected one already failed config load otherwise —
    stays verbatim (that provider stays unavailable in Pi, the exact
    pre-#303 behavior). The value itself is never logged.
    """
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: os.environ.get(match.group(1) or match.group(2))
        or match.group(0),
        api_key,
    )


def parse_repositories(entries: object, base: Path) -> list[dict]:
    """Parse the explicit multi-repo registry.

    Each entry is a TOML table with the required string fields `name`,
    `path`, `github` and `base_branch`; `path` is resolved relative to
    the config file's directory. A missing/empty/non-string field, a
    non-table entry or a duplicate `name` fails fast — the existence and
    Git-checkout checks happen in `validate_config`.
    """
    if not isinstance(entries, list):
        raise ValueError("repositories must be a list of tables")
    repos: list[dict] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"repositories[{index}] must be a table")
        missing = [
            field for field in ("name", "path", "github", "base_branch")
            if not isinstance(entry.get(field), str) or not entry.get(field)
        ]
        if missing:
            raise ValueError(
                f"repositories[{index}] is missing required field(s): "
                + ", ".join(missing)
            )
        if any(repo["name"] == entry["name"] for repo in repos):
            raise ValueError(
                f"duplicate repositories name: {entry['name']!r}"
            )
        # The optional repository config path (default
        # `.github/orbi.toml`) — a string, no path resolution (it is a
        # repository-relative path read through the GitHub contents API,
        # never the local checkout).
        config_path = entry.get("config_path", REPO_CONFIG_PATH)
        if not isinstance(config_path, str) or not config_path:
            raise ValueError(
                f"repositories[{index}].config_path must be a non-empty "
                "string"
            )
        repos.append({
            "name": entry["name"],
            "path": _config_path(entry["path"], base),
            "github": entry["github"],
            "base_branch": entry["base_branch"],
            "config_path": config_path,
        })
    return repos


def repository_config_path(config: RunnerConfig, source_repo: str) -> str:
    """The repository config path of one source repo.

    The optional `[[repositories]].config_path` wins when its `github`
    entry matches the source repo; otherwise the single default location
    `.github/orbi.toml` applies.
    """
    for repo in config.repositories:
        if repo.get("github") == source_repo:
            return repo.get("config_path", REPO_CONFIG_PATH)
    return REPO_CONFIG_PATH
