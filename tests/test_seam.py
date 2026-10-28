"""The shared `seam` fan-out target undoes exactly what it overlaid (Issue #1948).

`tests/seam.py` replaces one seam name in EVERY module that binds it, so a
fake intercepts the call whichever module executes the function under test
(Issue #785). pytest 9.2.0's `MonkeyPatch.setattr` reads a patch's old value
from the target's instance `__dict__` when the name is not a data descriptor
(`_pytest/monkeypatch.py`), and `undo()` then deletes an attribute it found no
entry for. The proxy therefore keeps NO instance state: `Seam.__slots__ = ()`
leaves pytest on the `setattr` undo path every pytest version takes, and
`Seam.__setattr__` is the whole undo — it fans the value that was current back
out to every module bound to it. A proxy that remembers its originals instead
(#1948's `__delattr__`) restores the FIRST recorded original, so an inner
`MonkeyPatch.context()` exit removes the still-active outer fake (Issue #1953).
"""
import pytest

import orbi.cli as cli
import orbi.gitops as gitops
import orbi.journal as journal
import orbi.release_run as release_run
import orbi.runner as runner
from seam import seam

# The modules whose `run_command` binding the fan-out must reach.
BINDING_MODULES = (journal, gitops, runner)

# The binding a fresh process resolves: captured at import, before any test
# patches the seam. The "later tests stay isolated" regression reads it after
# a test that patched the seam.
REAL_RUN_COMMAND = journal.run_command

# The real editable-install refresh, the name the autouse
# `_default_cli_install_preflight` fixture stubs.
REAL_REFRESH_CLI_INSTALL = runner.refresh_cli_install


def bindings(name: str) -> list:
    """The current binding of `name` in each fan-out target module."""
    return [vars(module)[name] for module in BINDING_MODULES]


@pytest.fixture(autouse=True, scope="module")
def _seam_isolation_after_the_module():
    """The last test's teardown — the autouse `_default_cli_install_preflight`
    fixture undoes its two `refresh_cli_install` patches with
    `MonkeyPatch.undo` — leaves the real bindings (Issue #1953). A module
    fixture tears down after every function fixture of its last test."""
    yield
    assert bindings("run_command") == [REAL_RUN_COMMAND] * 3
    assert release_run.refresh_cli_install is REAL_REFRESH_CLI_INSTALL
    assert runner.refresh_cli_install is REAL_REFRESH_CLI_INSTALL


def test_one_patch_reaches_every_module_that_binds_the_function():
    """The fan-out and its undo: whichever undo path the pytest version
    uses, the real bindings return."""
    original = bindings("run_command")
    fake = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seam, "run_command", fake)
        assert seam.run_command is fake
        assert bindings("run_command") == [fake, fake, fake]
    assert bindings("run_command") == original


def test_nested_contexts_restore_the_outer_fake_then_the_original():
    """Issue #1953: an inner context restores the OUTER fake — the outer
    patch is still active — and only the outer exit restores the real
    binding. pytest 9.2.0's `undo()` runs `setattr` here, the delete path of
    a stateful proxy restored the original too early."""
    original = bindings("run_command")
    assert original == [REAL_RUN_COMMAND] * 3
    first, second = object(), object()
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(seam, "run_command", first)
        assert bindings("run_command") == [first, first, first]
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(seam, "run_command", second)
            assert bindings("run_command") == [second, second, second]
        assert bindings("run_command") == [first, first, first]
    assert bindings("run_command") == original


def test_an_inner_context_leaves_the_autouse_preflight_fake_active(
        _default_cli_install_preflight):
    """The real autouse preflight fixture is the outer patch: its fake (two
    `refresh_cli_install` patches in one `monkeypatch` fixture) is still on
    every binding module after an inner context exits (Issue #1953)."""
    fixture_fake = seam.refresh_cli_install
    assert fixture_fake is not REAL_REFRESH_CLI_INSTALL
    assert release_run.refresh_cli_install is fixture_fake
    assert runner.refresh_cli_install is fixture_fake
    nested = object()
    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(seam, "refresh_cli_install", nested)
        assert seam.refresh_cli_install is nested
        assert release_run.refresh_cli_install is nested
        assert runner.refresh_cli_install is nested
    assert seam.refresh_cli_install is fixture_fake
    assert release_run.refresh_cli_install is fixture_fake
    assert runner.refresh_cli_install is fixture_fake


def test_repeated_patches_in_one_context_restore_the_original():
    """Repeated patches in ONE `MonkeyPatch` instance — the autouse preflight
    fixture patches the same name twice — leave the real bindings once the
    instance undoes them in reverse."""
    original = bindings("run_command")
    first, second = object(), object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seam, "run_command", first)
        patch.setattr(seam, "run_command", second)
        assert bindings("run_command") == [second, second, second]
    assert bindings("run_command") == original


def test_a_manual_setattr_restore_leaves_the_original_bindings():
    """`seam.x = original` is the restore `tests/test_github.py` and
    `tests/test_run_id_fixture.py` perform; the overlay is the fan-out, so
    writing the original back restores every module."""
    original = bindings("run_command")
    fake = object()
    seam.run_command = fake
    assert bindings("run_command") == [fake, fake, fake]
    seam.run_command = original[0]
    assert bindings("run_command") == original


def test_a_name_no_module_binds_is_a_no_op():
    """Writing a name the proxy cannot resolve changes nothing, so undoing it
    changes nothing either."""
    seam.no_such_seam_name = object()
    assert not hasattr(seam, "no_such_seam_name")


def test_the_proxy_keeps_no_instance_state():
    """The mechanism behind the nested restore (Issue #1953): with no instance
    `__dict__` pytest cannot read an (absent) old-value entry and keeps the
    old value it resolved through `__getattr__`, so `undo()` assigns it back
    instead of deleting the attribute."""
    assert not hasattr(seam, "__dict__")


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
        seam.LOGGER = bootstrap_logger
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
