"""The `orbi suggest` seams (Issues #2143, #2277).

`orbi suggest` starts its sessions through ``seam.stream_pi`` and reads
its repository view through ``orbi.draft.draft_repo_view``; a test answers
the sessions with a canned stub and can stand the view in by its fixture
checkout. These patches live here because ``tests/fakes/`` is the adapter
contract and sits outside the patch ratchet (Issue #789) — the `orbi draft`
seams are installed the same way (``tests/fakes/draft.py``).
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from seam import seam


def stub_stream_pi(monkeypatch, answer) -> None:
    """Answer every `orbi suggest` Pi session through `answer`."""
    monkeypatch.setattr(seam, "stream_pi", answer)


class GhRecorder:
    """gh goes to the in-memory GitHub; everything else to the real CLI."""

    def __init__(self, gh) -> None:
        self.gh = gh

    def __call__(self, command, **kwargs):
        argv = list(command)
        if argv and argv[0] == "gh":
            return self.gh(command, **kwargs)
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True,
            cwd=kwargs.get("cwd"),
        )
        return completed.stdout.strip()


def install_gh(monkeypatch, gh) -> None:
    """Route the one subprocess seam to the in-memory GitHub."""
    monkeypatch.setattr(seam, "run_command", GhRecorder(gh))


@contextlib.contextmanager
def local_repository_view(config, *, run_id):
    """The engine's fetched view, stood in by the fixture checkout.

    A run that does not need a real remote reads the checkout the test
    built, exactly like the pre-#2277 engine did; the view's own behavior
    is covered by `tests/test_suggest_repo_view.py` over real git
    repositories (the same fake `orbi draft` uses for Issue #2276).
    """
    yield Path(config.repo_dir)


def stop_repo_view(monkeypatch) -> None:
    """Every run reads the fixture checkout instead of fetching origin.

    Patched through the seam proxy so the boundary patch reaches both
    `orbi.draft` and `orbi.suggest`, which bind the same function
    (Issue #2277).
    """
    monkeypatch.setattr(seam, "draft_repo_view", local_repository_view)
