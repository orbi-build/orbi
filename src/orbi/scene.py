"""The run scene: a versioned machine-readable recovery block (Issue #786).

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
but corrupted. The transition keeps one version reading the legacy text
grammar (`- key: value` and `(key=value)`) next to the v1 block; the
next version removes the legacy path and notes it in the release notes.
"""
from __future__ import annotations

import dataclasses
import json
import re

from orbi.journal import validate_run_id

SCHEMA_VERSION = 1

# The single hidden block: a versioned marker framing a JSON payload
# that mirrors the `Scene` fields exactly (render writes every field).
# The capture is non-greedy to the first `-->` so a truncated or
# mangled payload still matches the marker and fails as corrupted
# instead of falling back to the text beside it.
SCENE_BLOCK_TEMPLATE = "<!-- orbi:scene:v1 {payload} -->"
_SCENE_BLOCK_RE = re.compile(
    r"<!--\s*orbi:scene:v1\s+(.*?)-->", re.DOTALL,
)

# The legacy text grammar lives one version longer, for scenes written
# before the block existed ("Accept both during recovery").
OPENED_PR_PREFIX = "Orbi opened PR: "
_FIELD_LINE_RE = re.compile(r"\s*-\s*([A-Za-z_][\w-]*)(?::\s*|=)(.*)\s*$")


@dataclasses.dataclass(frozen=True)
class Scene:
    """One recoverable run scene, exactly as the v1 block carries it.

    `external` keeps the legacy string semantics ("" or "true"): a
    takeover delivery (Issue #608) reviews the contributor's PR instead
    of a runner-owned one. `review_round` is the delivery's review
    round counter (0 when the PR opens); it turns the scene into the
    waiting primitive's state anchor (Issue #788). `schema` mirrors the
    marker version the block was rendered with.
    """

    run_id: str
    base_branch: str
    base_sha: str
    pr_url: str
    external: str = ""
    review_round: int = 0
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


def parse(body: object) -> Scene | None:
    """Parse the scene from one comment body.

    Returns `None` when the body carries no scene at all (没有现场: not
    an opened-PR comment, or only a scene marker this reader does not
    support). Returns the `Scene` when a valid v1 block is present —
    the block wins over any legacy text next to it. Raises
    `SceneError` when a scene is present but corrupted (损坏现场): an
    unparseable or schema-violating block, or a legacy scene comment
    with missing/invalid fields. A corrupted block is never worked
    around through the legacy text beside it.
    """
    if not isinstance(body, str):
        return None
    blocks = _SCENE_BLOCK_RE.findall(body)
    if len(blocks) > 1:
        raise SceneError(
            "multiple orbi:scene:v1 blocks in one comment body"
        )
    if blocks:
        return _parse_block(blocks[0])
    return _parse_legacy(body)


_SCENE_FIELDS = tuple(
    field.name for field in dataclasses.fields(Scene)
)


def _parse_block(payload: str) -> Scene:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SceneError(
            f"orbi:scene:v1 block is not valid JSON: {exc}"
        ) from exc
    unknown = sorted(set(data) - set(_SCENE_FIELDS))
    if unknown:
        raise SceneError(
            f"orbi:scene:v1 block has unknown fields: {unknown}"
        )
    missing = sorted(name for name in _SCENE_FIELDS if name not in data)
    if missing:
        raise SceneError(
            f"orbi:scene:v1 block is missing fields: {missing}"
        )
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
        schema=data["schema"],
    )


def _parse_legacy(body: str) -> Scene | None:
    """Parse the pre-block text grammar, or None when it is not a scene.

    The exact pre-#786 grammar: `Orbi opened PR: <url>` followed by
    `- key: value` lines and/or a `(key=value ...)` tail; both are
    accepted during recovery. Errors match the historical messages.
    """
    if OPENED_PR_PREFIX not in body:
        return None
    head = body.split(OPENED_PR_PREFIX, 1)[1]
    pr_head = head.partition(" (")[0].splitlines()
    pr_url = pr_head[0].strip() if pr_head else ""
    fields: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        match = _FIELD_LINE_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2)
    legacy = head.partition(" (")[2].rstrip(")")
    for part in legacy.split():
        key, _, value = part.partition("=")
        if key:
            fields[key] = value
    values = {
        "pr_url": pr_url.strip(),
        "base_branch": fields.get("base_branch", ""),
        "base_sha": fields.get("base_sha", ""),
        "run_id": fields.get("run_id", ""),
    }
    for name, value in values.items():
        if not value:
            raise SceneError(f"opened PR comment is missing {name}")
    try:
        run_id = validate_run_id(values["run_id"])
    except ValueError as exc:
        raise SceneError(str(exc)) from exc
    # The optional external-takeover flag is added after the required-
    # field check: its absence is normal, never an error.
    return Scene(
        run_id=run_id,
        base_branch=values["base_branch"],
        base_sha=values["base_sha"],
        pr_url=values["pr_url"],
        external=fields.get("external", ""),
    )
