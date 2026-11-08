"""The `orbi suggest` Pi seam (Issue #2143).

`orbi suggest` starts its sessions through ``seam.stream_pi``; a test
answers them with a canned stub. The patch lives here because
``tests/fakes/`` is the adapter contract and sits outside the patch
ratchet (Issue #789) — the `orbi draft` seam is installed the same way
(``tests/fakes/draft.py``).
"""
from seam import seam


def stub_stream_pi(monkeypatch, answer) -> None:
    """Answer every `orbi suggest` Pi session through `answer`."""
    monkeypatch.setattr(seam, "stream_pi", answer)
