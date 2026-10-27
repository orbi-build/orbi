"""The shared `seam` fan-out target undoes exactly what it overlaid (Issue #1948).

`tests/seam.py` replaces one seam name in EVERY module that binds it, so a
fake intercepts the call whichever module executes the function under test
(Issue #785). pytest 9.2.0 captures a patch's old value from the target's
instance `__dict__` — empty for the proxy — and therefore undoes the patch
with `delattr` instead of `setattr` (`_pytest/monkeypatch.py`,
`MonkeyPatch.setattr`/`undo`). Without a matching undo the first patch made
by the autouse `_default_cli_install_preflight` fixture failed during
teardown (`AttributeError: 'Seam' object has no attribute
'refresh_cli_install'`) and every later test kept that fixture's fake.
"""
import pytest

import orbi.cli as cli
import orbi.gitops as gitops
import orbi.journal as journal
import orbi.runner as runner
from seam import seam

# The modules whose `run_command` binding the fan-out must reach.
BINDING_MODULES = (journal, gitops, runner)

# The binding a fresh process resolves: captured at import, before any test
# patches the seam. The "later tests stay isolated" regression reads it after
# a test that patched the seam.
REAL_RUN_COMMAND = journal.run_command


def bindings(name: str) -> list:
    """The current binding of `name` in each fan-out target module."""
    return [vars(module)[name] for module in BINDING_MODULES]


def test_one_patch_reaches_every_module_that_binds_the_function():
    """The fan-out and its undo: whichever undo path the pytest version
    uses (`delattr` on 9.2.0, `setattr` before), the real bindings return."""
    original = bindings("run_command")
    fake = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seam, "run_command", fake)
        assert seam.run_command is fake
        assert bindings("run_command") == [fake, fake, fake]
    assert bindings("run_command") == original


def test_nested_patches_restore_the_original_bindings():
    """Repeated patches — the autouse preflight fixture patches the same name
    twice — leave the real bindings once the overlays are undone, and a
    second undo has nothing left to restore."""
    original = bindings("run_command")
    first, second = object(), object()
    seam.run_command = first
    seam.run_command = second
    assert seam.run_command is second
    delattr(seam, "run_command")
    assert bindings("run_command") == original
    delattr(seam, "run_command")
    assert bindings("run_command") == original


def test_a_manual_setattr_restore_undoes_the_patch():
    """`seam.x = original` is the restore `tests/test_github.py` and
    `tests/test_run_id_fixture.py` perform; it ends the overlay instead of
    leaving one a later teardown would fan a stale fake back from."""
    original = bindings("run_command")
    fake = object()
    seam.run_command = fake
    seam.run_command = original[0]
    assert bindings("run_command") == original
    delattr(seam, "run_command")
    assert bindings("run_command") == original


def test_a_name_no_module_binds_is_a_no_op():
    """Writing a name the proxy cannot resolve changes nothing, so undoing it
    changes nothing either."""
    seam.no_such_seam_name = object()
    assert not hasattr(seam, "no_such_seam_name")


def test_fan_out_leaves_unrelated_same_named_objects_alone():
    """Issue #1576: only modules bound to the SAME object are rewritten —
    `gitops.LOGGER` and `cli.LOGGER` are their own loggers."""
    bootstrap_logger = journal.LOGGER
    assert runner.LOGGER is bootstrap_logger
    gitops_logger, cli_logger = gitops.LOGGER, cli.LOGGER
    assert gitops_logger is not bootstrap_logger
    assert cli_logger is not bootstrap_logger
    fake = object()
    seam.LOGGER = fake
    try:
        assert journal.LOGGER is fake
        assert runner.LOGGER is fake
        assert gitops.LOGGER is gitops_logger
        assert cli.LOGGER is cli_logger
    finally:
        delattr(seam, "LOGGER")
    assert journal.LOGGER is bootstrap_logger
    assert runner.LOGGER is bootstrap_logger


def test_a_patched_seam_is_gone_for_the_next_test():
    """A seam patch is active for one test only (the next test asserts the
    real binding): `MonkeyPatch.undo` is the code the `monkeypatch`
    fixture teardown runs."""
    fake = object()
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(seam, "run_command", fake)
        assert bindings("run_command") == [fake, fake, fake]
    finally:
        patch.undo()


def test_the_next_test_sees_the_original_bindings():
    """The isolation the CI teardown error robbed: the previous test's fake
    does not outlive it."""
    assert bindings("run_command") == [REAL_RUN_COMMAND] * 3
