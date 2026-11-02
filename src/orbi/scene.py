"""The run scene: a versioned machine-readable recovery block.

The Runner's resume path recovers the run scene (run id, base, PR URL,
external-takeover flag, review round) from ONE hidden, versioned block
in the trusted "Orbi opened PR" comment:

    <!-- orbi:scene:v1 {"base_branch": ..., "schema": 1, ...} -->

The human-readable text around the block is display only; the machine
reads only the block, so a wording change can never change control flow
again. The module is pure (the `delivery_labels` model): every function
is a deterministic function of its inputs, with no I/O. The trusted-
author filter and the comment read/write stay with the runner and the
GitHub adapter; this module sees only comment body strings.

Failure shapes are distinct: `parse` returns `None` when the body
carries no scene at all and raises `SceneError` when a scene is present
but corrupted. The pre-v0.5.2 text grammar (`- key: value` and
`(key=value)`) is gone (Issue #2068): a comment without the block
carries no scene, however its human-readable text reads.
"""
from __future__ import annotations

import dataclasses
import json

from orbi.comment_kind import parse_versioned_block, versioned_block_pattern
from orbi.journal import validate_run_id

SCHEMA_VERSION = 1

# The single hidden block: a versioned marker framing a JSON payload
# that mirrors the `Scene` fields exactly (render writes every field).
# The envelope (marker capture, single block, JSON object, field diff) is
# `comment_kind.parse_versioned_block`; this module keeps the fields and
# their checks.
_MARKER = "orbi:scene:v1"
SCENE_BLOCK_TEMPLATE = f"<!-- {_MARKER} {{payload}} -->"
_SCENE_BLOCK_RE = versioned_block_pattern(_MARKER)


@dataclasses.dataclass(frozen=True)
class Scene:
    """One recoverable run scene, exactly as the v1 block carries it.

    `external` keeps the legacy string semantics ("" or "true"): a
    takeover delivery reviews the contributor's PR instead
    of a runner-owned one. `review_round` is the delivery's review
    round counter (0 when the PR opens); it turns the scene into the
    waiting primitive's state anchor. `schema` mirrors the
    marker version the block was rendered with.
    """

    run_id: str
    base_branch: str
    base_sha: str
    pr_url: str
    external: str = ""
    review_round: int = 0
    base_advance_round: int = 0
    verdict_head_unknown_round: int = 0
    schema: int = SCHEMA_VERSION


class SceneError(ValueError):
    """A scene is present in the body but cannot be parsed (损坏)."""


class SceneMissingError(ValueError):
    """No trusted comment of the Issue carries a scene at all (没有)."""


def render(record: Scene) -> str:
    """Render the single hidden v1 block for one scene.

    Deterministic: the same scene always renders the same line (sorted
    JSON keys), so a re-render never rewrites the comment in place.
    """
    payload = json.dumps(dataclasses.asdict(record), sort_keys=True)
    return SCENE_BLOCK_TEMPLATE.format(payload=payload)


def carries_scene_block(body: object) -> bool:
    """True when the body carries a v1 scene block.

    The #825 dead-loop guard reads this as "the delivery line
    advanced" (an opened PR, a completed review round): such a comment
    ends a repeated-failure streak. The parser's own regex decides, so
    a corrupted block still counts as present.
    """
    return isinstance(body, str) and bool(_SCENE_BLOCK_RE.search(body))


def parse(body: object) -> Scene | None:
    """Parse the scene from one comment body.

    Returns `None` when the body carries no scene at all (没有现场: no
    v1 block, however the human-readable text reads, or only a scene
    marker this reader does not support). Returns the `Scene` when a
    valid v1 block is present. Raises `SceneError` when a block is
    present but corrupted (损坏现场): an unparseable or
    schema-violating block.
    """
    if not isinstance(body, str):
        return None
    data = parse_versioned_block(
        body, _MARKER, _SCENE_FIELDS, SceneError,
        required=_SCENE_REQUIRED_FIELDS,
    )
    return None if data is None else _parse_block(data)


_SCENE_FIELDS = tuple(
    field.name for field in dataclasses.fields(Scene)
)
# These counters were added to the v1 payload without changing the
# marker version; old v1 opened-PR comments must remain resumable.
_SCENE_REQUIRED_FIELDS = tuple(
    name for name in _SCENE_FIELDS
    if name not in {"base_advance_round", "verdict_head_unknown_round"}
)


def _parse_block(data: dict) -> Scene:
    for name in ("run_id", "base_branch", "base_sha", "pr_url"):
        if not isinstance(data[name], str) or not data[name]:
            raise SceneError(
                f"orbi:scene:v1 field {name} must be a non-empty string"
            )
    if not isinstance(data["external"], str):
        raise SceneError(
            "orbi:scene:v1 field external must be a string"
        )
    if type(data["review_round"]) is not int or data["review_round"] < 0:
        raise SceneError(
            "orbi:scene:v1 field review_round must be a "
            "non-negative integer"
        )
    for counter in ("base_advance_round", "verdict_head_unknown_round"):
        if counter in data and (
            type(data[counter]) is not int or data[counter] < 0
        ):
            raise SceneError(
                f"orbi:scene:v1 field {counter} must be a "
                "non-negative integer"
            )
    if type(data["schema"]) is not int or data["schema"] != SCHEMA_VERSION:
        raise SceneError(
            f"orbi:scene:v1 block schema {data['schema']!r} != "
            f"supported {SCHEMA_VERSION}"
        )
    try:
        run_id = validate_run_id(data["run_id"])
    except ValueError as exc:
        raise SceneError(str(exc)) from exc
    return Scene(
        run_id=run_id,
        base_branch=data["base_branch"],
        base_sha=data["base_sha"],
        pr_url=data["pr_url"],
        external=data["external"],
        review_round=data["review_round"],
        base_advance_round=data.get("base_advance_round", 0),
        verdict_head_unknown_round=data.get("verdict_head_unknown_round", 0),
        schema=data["schema"],
    )
