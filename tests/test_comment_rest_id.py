"""The one REST-id rule every comment PATCH goes through (Issue #2004).

`gh issue view --json comments` reports the GraphQL node id (an
`"IC_kw..."` string) next to the comment's permalink, and the permalink's
`#issuecomment-<digits>` suffix IS the REST id the in-place edit's
`PATCH /repos/{repo}/issues/comments/{id}` takes. The base-CI red recovery,
the check-permission recovery and the failure reporter all read it here.
"""
from pathlib import Path

import pytest

from orbi import comment_rest_id


@pytest.mark.parametrize("comment, expected", [
    # The real `gh issue view --json comments` shape: a GraphQL node id and
    # the permalink that carries the REST id.
    ({"id": "IC_kwDOUC1jsc8AAAABfdXK_Q",
      "url": "https://github.com/o/r/issues/7#issuecomment-6406130429"},
     6406130429),
    # A numeric id field is the REST id already (a `gh api` read).
    ({"id": 41, "url": "https://github.com/o/r/issues/7#issuecomment-41"}, 41),
    # A bool is not an id (it is an `int` subclass in Python).
    ({"id": True, "url": "https://github.com/o/r/issues/7#issuecomment-42"}, 42),
    # A URL that carries no permalink id resolves to nothing, never a guess.
    ({"url": "https://github.com/o/r/issues/7"}, None),
    ({"url": "https://github.com/o/r/issues/7#comment-9"}, None),
    ({}, None),
])
def test_rest_comment_id_reads_the_id_field_then_the_permalink(
    comment, expected,
):
    assert comment_rest_id.rest_comment_id(comment) == expected


def test_the_comment_permalink_regex_has_one_owner():
    """Issue #2004 acceptance: `grep -rn 'issuecomment-' src/orbi/` finds
    exactly one regex — this module's — so no caller keeps a copy. The
    failure reporter's log message still names the expected suffix, which is
    prose about the rule, not a second parse of it."""
    source_dir = Path(comment_rest_id.__file__).resolve().parent
    owners = sorted(
        path.name for path in source_dir.glob("*.py")
        if r"#issuecomment-(\d+)" in path.read_text(encoding="utf-8")
    )
    assert owners == ["comment_rest_id.py"]
