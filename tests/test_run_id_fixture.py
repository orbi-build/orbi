"""The `bind_run_id` fixture restores the binding it found (Issue #1882).

`seam.set_run_id` fans a test's patch out to `orbi.journal.set_run_id`, and
pytest tears the autouse `bind_run_id` fixture down BEFORE undoing that
patch. The fixture therefore must not re-read the setter at teardown: with a
non-None previous binding it would call the test's fake and leave the test's
own `a1b2c3d4` bound for the next test.

Each test drives the fixture's generator the way pytest does (setup, yield,
teardown) and keeps the setter patch active exactly during teardown, which is
the real autouse ordering. The patch goes through `seam` itself — the
fan-out target a test's `monkeypatch` setattr on `seam` for `set_run_id`
uses — so the regression adds no patch-ratchet internal site.
"""
from contextlib import contextmanager

import pytest

import orbi.journal as journal
from conftest import bind_run_id as bind_run_id_fixture
from seam import seam

RUN_ID = "a1b2c3d4"
PREVIOUS_ID = "deadbeef"


def _start_fixture():
    """Start one fresh `bind_run_id` fixture, returning its bind callable."""
    generator = bind_run_id_fixture.__wrapped__()
    return generator, next(generator)


def _teardown(generator) -> None:
    """Run the fixture's teardown; pytest resumes the generator once."""
    with pytest.raises(StopIteration):
        next(generator)


@contextmanager
def _patched_setter():
    """Apply `seam.set_run_id`'s fan-out patch, restoring it afterwards."""
    original = seam.set_run_id
    seam.set_run_id = lambda run_id: None
    try:
        yield
    finally:
        seam.set_run_id = original


def _bind_previous(previous: str | None) -> None:
    if previous is None:
        journal.clear_run_id()
    else:
        journal.set_run_id(previous)


@pytest.mark.parametrize("previous", [None, PREVIOUS_ID])
def test_fixture_restores_the_previous_binding(previous):
    _bind_previous(previous)
    generator, bind = _start_fixture()
    try:
        bind(RUN_ID)
        assert journal.current_run_id() == RUN_ID
    finally:
        _teardown(generator)
    assert journal.current_run_id() == previous


@pytest.mark.parametrize("previous", [None, PREVIOUS_ID])
def test_fixture_restores_the_previous_binding_while_the_setter_is_patched(previous):
    _bind_previous(previous)
    generator, bind = _start_fixture()
    try:
        bind(RUN_ID)
        assert journal.current_run_id() == RUN_ID
    finally:
        # The test's monkeypatch is still applied here: pytest finalizes
        # `bind_run_id` before it undoes `monkeypatch`.
        with _patched_setter():
            _teardown(generator)
    assert journal.current_run_id() == previous


def test_fixture_restores_the_previous_binding_when_the_test_raises():
    journal.set_run_id(PREVIOUS_ID)
    generator, bind = _start_fixture()
    with pytest.raises(RuntimeError, match="the test failed"):
        try:
            bind(RUN_ID)
            raise RuntimeError("the test failed")
        finally:
            with _patched_setter():
                _teardown(generator)
    assert journal.current_run_id() == PREVIOUS_ID
