"""The runner identity marker of every Orbi comment (extracted, Issue #1229).

`progress.py` keeps the marker's format and appends it; this leaf owns the
probe that answers WHICH code is running, so the comment module stays
under the Article 3 size cap without changing the marker or its readers.
`progress.runner_fingerprint` stays the public reading point (the name is
re-exported there, and the marker renders through it, so an existing
`monkeypatch.setattr(progress, "runner_fingerprint", ...)` keeps working).
"""
from __future__ import annotations

import re
import subprocess
from importlib import metadata
from pathlib import Path


def _checkout_root(path: Path) -> Path | None:
    """Return the nearest checkout containing a Git marker."""
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def runner_fingerprint() -> str:
    """Identify the code executing the Runner without fabricating a value.

    An editable install points at a package inside its checkout, whose HEAD
    is the useful deployment identity. A copied/non-editable package has no
    checkout, so its distribution version is the only available identity.
    Any probe failure is deliberately reported as ``unknown``.
    """
    try:
        checkout = _checkout_root(Path(__file__).resolve().parent)
        if checkout is not None:
            result = subprocess.run(
                ["git", "rev-parse", "--short=8", "HEAD"],
                cwd=checkout, check=True, capture_output=True,
                text=True, timeout=5,
            )
            fingerprint = result.stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", fingerprint):
                return fingerprint
            return "unknown"
        version = metadata.version("orbi-cli")
        return version if isinstance(version, str) and version else "unknown"
    except Exception:
        return "unknown"
