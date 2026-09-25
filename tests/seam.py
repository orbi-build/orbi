"""Fan-out patch target for the subprocess seam (Issue #785).

Since Issue #785 the seam primitives (`run_command`,
`run_git_network_command`, `run_gh_read_command`) live in `orbi.journal`
/ `orbi.github` / `orbi.gitops`, and every module binds them into its own
namespace. A fake must therefore replace the name in EVERY module that
binds it, so it intercepts the seam no matter which module executes the
function under test. ``monkeypatch.setattr(seam, "run_command", fake)``
does exactly that: reading resolves the current binding (monkeypatch's
old-value capture) and writing fans the value out to all bindings, so
monkeypatch's teardown fans the original back.
"""
import orbi.cli_source as cli_source
import orbi.clarify as clarify
import orbi.claim as claim
import orbi.github as github
import orbi.gitops as gitops
import orbi.journal as journal
import orbi.milestone as milestone
import orbi.milestone_command as milestone_command
import orbi.ticket_command as ticket_command
import orbi.progress as progress
import orbi.repo_config as repo_config
import orbi.release as release
import orbi.release_git as release_git
import orbi.pi_session as pi_session
import orbi.runner as runner

_MODULES = (
    journal, github, gitops, milestone, milestone_command, ticket_command,
    progress, cli_source, release, release_git, pi_session, runner, claim,
    repo_config, clarify,
)

# Issue #1366: `run_git` prefixes every Runner-side git command with the
# overrides that disable repository-selected programs. The fakes below
# were written against the plain argv, so the fan-out normalises them
# back before a fake sees the command.
_SAFETY_CONFIG_KEYS = frozenset(key for key, _ in journal._GIT_SAFETY_CONFIG)
_SAFETY_FLAGS = frozenset({"--no-textconv", "--no-ext-diff"})
_ORIGINAL_RUN_COMMAND = journal.run_command
_ORIGINAL_RUN_GIT_NETWORK_COMMAND = journal.run_git_network_command
_WRAPPED_FAKES: list[tuple] = []


def _is_safety_config(assignment: str) -> bool:
    key = assignment.split("=", 1)[0]
    return key in _SAFETY_CONFIG_KEYS or key.startswith("filter.")


def strip_safety(command):
    """Remove `run_git`'s neutralising options from a recorded command."""
    if not isinstance(command, list) or command[:1] != ["git"]:
        return command
    stripped = ["git"]
    index = 1
    while index < len(command):
        token = command[index]
        if (token == "-c" and index + 1 < len(command)
                and _is_safety_config(command[index + 1])):
            index += 2
            continue
        if token in _SAFETY_FLAGS:
            index += 1
            continue
        stripped.append(token)
        index += 1
    return stripped


def _normalise_fake(value):
    """Wrap a seam fake so it sees the plain git argv (idempotent)."""
    for original, wrapped in _WRAPPED_FAKES:
        if original is value:
            return wrapped

    def normalised(*args, **kwargs):
        if args:
            args = (strip_safety(args[0]), *args[1:])
        return value(*args, **kwargs)

    _WRAPPED_FAKES.append((value, normalised))
    return normalised


class Seam:
    """Set/read a seam name across every module that binds it."""

    def __getattr__(self, name: str):
        for module in _MODULES:
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        if (name == "run_command" and value is not _ORIGINAL_RUN_COMMAND) or (
            name == "run_git_network_command"
            and value is not _ORIGINAL_RUN_GIT_NETWORK_COMMAND
        ):
            value = _normalise_fake(value)
        for module in _MODULES:
            if name in vars(module):
                setattr(module, name, value)


seam = Seam()


def _runner_hook(name: str):
    """Resolve one runner-owned resume hook at call time."""
    def call(*args, **kwargs):
        return getattr(runner, name)(*args, **kwargs)
    return call


def resume_deps() -> claim.ResumeHooks:
    """The resume hooks `runner.main` injects into the claim scans.

    `orbi.claim` never imports `orbi.runner` (Article 3.3): the
    delivery-side scene helpers arrive as `ResumeHooks`. A test that calls
    a claim scan directly wires the same callables here; the lookup
    happens per call, so a `monkeypatch.setitem(runner.__dict__, ...)`
    still intercepts the real delivery path. `comment_pr` is in the bag
    because `reconcile_orphan_prs` reports through the runner's writer.
    """
    return claim.ResumeHooks(
        resume_scene=_runner_hook("resume_scene"),
        route_external_pr_ticket=_runner_hook("_route_external_pr_ticket"),
        block_scene_failure=_runner_hook("block_scene_failure"),
        recover_missing_pr_scene=_runner_hook("_recover_missing_pr_scene"),
        has_recoverable_pr_scene=_runner_hook("_has_recoverable_pr_scene"),
        comment_pr=_runner_hook("comment_pr"),
    )
