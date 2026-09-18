"""The shipped command-deadline extension's TypeScript units (Issue #1093).

The repo has no JS package harness, so the Issue blesses shelling out to
`node --test`: the extension file carries no runtime import, and node
(>= 22.18) runs the `node:test` suite next to it with TypeScript type
stripping. A failure here means the extension the runner passes to every
Pi session no longer enforces the command-deadline contract — never a
skip: the units are the acceptance evidence, not an optional extra.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = REPO_ROOT / "src" / "orbi" / "pi_extensions"


def test_node_test_suite_of_the_command_deadline_extension():
    result = subprocess.run(
        ["node", "--test", str(EXTENSION_DIR / "command_deadline.test.mjs")],
        capture_output=True,
        text=True,
        # Issue #95: a blocking command is bounded; a timeout here is a
        # broken test path, never ignorable noise.
        timeout=120,
        cwd=REPO_ROOT,
    )
    # node --test exits non-zero when any unit fails: the exit code is
    # the result (Issue #180).
    assert result.returncode == 0, (
        f"node --test failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
