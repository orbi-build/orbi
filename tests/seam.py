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
import orbi.github as github
import orbi.gitops as gitops
import orbi.journal as journal
import orbi.progress as progress
import orbi.release as release
import orbi.runner as runner

_MODULES = (journal, github, gitops, progress, cli_source, release, runner)


class Seam:
    """Set/read a seam name across every module that binds it."""

    def __getattr__(self, name: str):
        for module in _MODULES:
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        for module in _MODULES:
            if name in vars(module):
                setattr(module, name, value)


seam = Seam()
