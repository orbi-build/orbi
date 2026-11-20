"""The ONE exit for a comment that blocks an Issue (Issues #1730, #2245).

Every blocked scene imports `post_blocked_comment` from here; the exit itself
lives in `orbi.stop_comment` since Issue #2287, because it now also sends the
stop's Telegram alert and this module sits at its size-ratchet ceiling. The
name stays importable from this module exactly as before — the same
re-export the `failure_evidence` split used (Issue #2202).
"""
from __future__ import annotations

from orbi.delivery_labels import EVENT_BLOCKED, READY_LABEL  # noqa: F401
from orbi.stop_comment import post_blocked_comment as post_blocked_comment
