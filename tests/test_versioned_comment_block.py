"""One versioned envelope for the three v1 comment records (Issue #1855).

`orbi:failure:v1`, `orbi:scene:v1` and `orbi:base-ci-red:v1` share the same
envelope rules: at most one block per body, a JSON object payload, no unknown
field and no missing required field. Each domain keeps its own exception class,
field types and validation, but a corrupt envelope must be rejected with the
identical message text. These tests feed the same four corrupt shapes to all
three records, so a change to one envelope rule can never diverge silently.
"""
import json

import pytest

from orbi import base_ci_red, comment_kind, failure, scene

# One record per v1 marker: its parse entry point, its own exception type, a
# valid payload and one required field (removed for the missing-field case).
RECORDS = [
    pytest.param(
        "orbi:failure:v1",
        failure.parse,
        failure.FailureError,
        {
            "schema": 1,
            "outcome": "blocked",
            "reason_code": "unclassified",
            "action_code": "fix_ticket",
            "retry_safe": False,
        },
        "reason_code",
        id="failure",
    ),
    pytest.param(
        "orbi:scene:v1",
        scene.parse,
        scene.SceneError,
        {
            "run_id": "a1b2c3d4",
            "base_branch": "main",
            "base_sha": "adeeaa0457c62fb5a105da82701f6d834e3ac676",
            "pr_url": "https://github.com/o/r/pull/1",
            "external": "",
            "review_round": 0,
            "base_advance_round": 0,
            "verdict_head_unknown_round": 0,
            "schema": 1,
        },
        "base_sha",
        id="scene",
    ),
    pytest.param(
        "orbi:base-ci-red:v1",
        base_ci_red.parse,
        base_ci_red.BaseCIRedError,
        {
            "schema": 1,
            "state": "waiting",
            "base_branch": "main",
            "base_sha": "adeeaa0457c62fb5a105da82701f6d834e3ac676",
            "pr": 7,
            "failing_checks": ["tests"],
        },
        "state",
        id="base-ci-red",
    ),
]


def _block(marker: str, payload: object) -> str:
    return f"<!-- {marker} {json.dumps(payload)} -->"


@pytest.mark.parametrize(
    "marker, parse, error, payload, required_field", RECORDS,
)
def test_every_record_rejects_the_same_corrupt_envelopes(
    marker, parse, error, payload, required_field,
):
    assert parse(_block(marker, payload)) is not None

    with pytest.raises(error) as excinfo:
        parse(_block(marker, ["blocked"]))
    assert str(excinfo.value) == f"{marker} block payload must be a JSON object"

    valid = _block(marker, payload)
    with pytest.raises(error) as excinfo:
        parse(f"{valid}\n{valid}")
    assert str(excinfo.value) == (
        f"multiple {marker} blocks in one comment body"
    )

    with pytest.raises(error) as excinfo:
        parse(_block(marker, {**payload, "extra": 1}))
    assert str(excinfo.value) == (
        f"{marker} block has unknown fields: ['extra']"
    )

    missing = {name: value for name, value in payload.items()
               if name != required_field}
    with pytest.raises(error) as excinfo:
        parse(_block(marker, missing))
    assert str(excinfo.value) == (
        f"{marker} block is missing fields: ['{required_field}']"
    )


def test_the_envelope_helper_owns_the_shared_rules():
    # The leaf the three records share: absent block -> None (never an
    # exception), and a present-but-corrupt block raises the caller's own
    # error type through the `error` factory.
    assert comment_kind.parse_versioned_block(
        "plain prose", "orbi:failure:v1", ("schema",), failure.FailureError,
    ) is None
    assert comment_kind.parse_versioned_block(
        None, "orbi:failure:v1", ("schema",), failure.FailureError,
    ) is None
    with pytest.raises(failure.FailureError, match="multiple"):
        comment_kind.parse_versioned_block(
            '<!-- orbi:failure:v1 {"schema": 1} -->\n'
            '<!-- orbi:failure:v1 {"schema": 1} -->',
            "orbi:failure:v1", ("schema",), failure.FailureError,
        )
    assert comment_kind.parse_versioned_block(
        '<!-- orbi:failure:v1 {"schema": 1} -->',
        "orbi:failure:v1", ("schema",), failure.FailureError,
    ) == {"schema": 1}
