"""The run scene: a versioned machine-readable recovery block (Issue #786).

`orbi.scene` is a pure module (the `delivery_labels` model): no I/O, no
`gh`, deterministic render and parse. The Runner writes the single
hidden `<!-- orbi:scene:v1 {json} -->` block into its trusted
"Orbi opened PR" comment and the next tick's resume reads the scene
back from that block — the human-readable text is display only. During
the transition one version of `parse` also reads the legacy text
grammar (`- key: value` and `(key=value)`).

`parse` distinguishes the two failure shapes the resume path must tell
apart (Issue #786): `None` — the body carries no scene at all;
`SceneError` — a scene is present but corrupted.
"""
import dataclasses
import json
import re

import pytest

from orbi import scene


RUN_ID = "a1b2c3d4"
PR_URL = "https://github.com/owner/repo/pull/9"


def scene_for(**overrides) -> scene.Scene:
    values = dict(
        run_id=RUN_ID,
        base_branch="main",
        base_sha="abc123def456",
        pr_url=PR_URL,
    )
    values.update(overrides)
    return scene.Scene(**values)


def block_for(**overrides) -> str:
    return scene.render(scene_for(**overrides))


def block_payload(text: str) -> dict:
    """The JSON payload of the first v1 block in `text` (test helper)."""
    match = re.search(r"<!--\s*orbi:scene:v1\s+(\{.*\})\s*-->", text)
    assert match, f"no v1 scene block in: {text!r}"
    return json.loads(match.group(1))


# ------------------------------------------------------------------ Scene


def test_scene_is_frozen():
    record = scene_for()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.run_id = "deadbeef"


def test_scene_defaults_cover_the_transition_fields():
    # `external` matches the legacy dict semantics ("" / "true");
    # `review_round` is the waiting-primitive field (Issue #788) and is
    # 0 at the moment the PR opens; `schema` mirrors the marker version.
    record = scene_for()
    assert record.external == ""
    assert record.review_round == 0
    assert record.schema == scene.SCHEMA_VERSION == 1


# ----------------------------------------------------------------- render


def test_render_produces_the_single_hidden_v1_block():
    body = block_for()
    assert body == (
        '<!-- orbi:scene:v1 {"base_branch": "main", '
        '"base_sha": "abc123def456", "external": "", '
        f'"pr_url": "{PR_URL}", "review_round": 0, '
        f'"run_id": "{RUN_ID}", "schema": 1}} -->'
    )


def test_render_is_deterministic():
    assert block_for() == block_for()


def test_render_carries_every_field_the_reader_needs():
    body = block_for(external="true", review_round=2)
    assert block_payload(body) == {
        "run_id": RUN_ID,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": PR_URL,
        "external": "true",
        "review_round": 2,
        "schema": 1,
    }


# ------------------------------------------------------------------ parse
# ------------------------------------------------- v1 block (the protocol)


def test_parse_round_trips_a_rendered_scene():
    assert scene.parse(block_for()) == scene_for()


def test_parse_reads_the_block_from_a_full_production_comment():
    body = (
        f"<!-- orbi:run={RUN_ID} -->\n"
        f"{block_for()}\n"
        f"Orbi opened PR: {PR_URL}\n"
        "- base_branch: main\n"
        "- base_sha: abc123def456\n"
        f"- run_id={RUN_ID}\n"
        "\n"
        "<!-- runner=19472631 -->"
    )
    assert scene.parse(body) == scene_for()


def test_parse_prefers_the_block_over_contradicting_legacy_text():
    # The block is the machine protocol; the human text below it is
    # display only — a tampered or stale text line never wins.
    body = (
        f"{block_for()}\n"
        f"Orbi opened PR: {PR_URL}\n"
        "- base_branch: evil-branch\n"
        "- base_sha: deadbeefdead\n"
        "- run_id=ffffffff\n"
    )
    assert scene.parse(body) == scene_for()


def test_parse_external_and_review_round_round_trip():
    body = block_for(external="true", review_round=3)
    assert scene.parse(body) == scene_for(external="true", review_round=3)


# ------------------------------------------- corrupted v1 block (SceneError)


def test_parse_fails_fast_on_a_block_with_invalid_json():
    with pytest.raises(scene.SceneError, match="not valid JSON"):
        scene.parse("<!-- orbi:scene:v1 {not json} -->")


@pytest.mark.parametrize("payload", ["42", "null", "true", '"text"'])
def test_parse_fails_fast_on_a_non_object_json_payload(payload: str):
    # A decodable-but-non-object payload is present but corrupted: it
    # must raise `SceneError` (the corrupted branch), never the
    # `TypeError` a scalar would cause in the field-set diff.
    with pytest.raises(scene.SceneError, match="JSON object"):
        scene.parse(f"<!-- orbi:scene:v1 {payload} -->")


def test_parse_fails_fast_on_a_block_missing_a_required_field():
    payload = block_payload(block_for())
    del payload["base_sha"]
    with pytest.raises(scene.SceneError, match="missing fields"):
        scene.parse(f"<!-- orbi:scene:v1 {json.dumps(payload)} -->")


def test_parse_fails_fast_on_an_empty_block():
    with pytest.raises(scene.SceneError, match="missing fields"):
        scene.parse("<!-- orbi:scene:v1 {} -->")


def test_parse_fails_fast_on_a_block_with_an_unknown_field():
    payload = block_payload(block_for())
    payload["branch"] = "/etc/passwd"
    with pytest.raises(scene.SceneError, match="unknown fields"):
        scene.parse(f"<!-- orbi:scene:v1 {json.dumps(payload)} -->")


def test_parse_fails_fast_on_a_block_with_an_empty_required_field():
    with pytest.raises(scene.SceneError, match="non-empty string"):
        scene.parse(block_for(pr_url=""))


def test_parse_fails_fast_on_a_block_with_a_non_string_required_field():
    with pytest.raises(scene.SceneError, match="non-empty string"):
        scene.parse(block_for(base_sha=123))


def test_parse_fails_fast_on_a_block_with_an_invalid_run_id():
    with pytest.raises(scene.SceneError, match="invalid run id"):
        scene.parse(block_for(run_id="run1"))


def test_parse_fails_fast_on_a_block_with_a_wrong_schema_version():
    with pytest.raises(scene.SceneError, match="schema"):
        scene.parse(block_for(schema=2))


def test_parse_fails_fast_on_a_block_with_a_non_integer_schema():
    with pytest.raises(scene.SceneError, match="schema"):
        scene.parse(block_for(schema="1"))


def test_parse_fails_fast_on_a_block_with_a_non_string_external():
    with pytest.raises(scene.SceneError, match="external"):
        scene.parse(block_for(external=1))


def test_parse_fails_fast_on_a_block_with_a_negative_review_round():
    with pytest.raises(scene.SceneError, match="review_round"):
        scene.parse(block_for(review_round=-1))


def test_parse_fails_fast_on_a_block_with_a_non_integer_review_round():
    with pytest.raises(scene.SceneError, match="review_round"):
        scene.parse(block_for(review_round="1"))


def test_parse_fails_fast_on_multiple_blocks_in_one_body():
    with pytest.raises(scene.SceneError, match="multiple"):
        scene.parse(f"{block_for()}\n{block_for()}")


def test_parse_fails_fast_on_a_corrupted_block_even_when_text_is_valid():
    # The block is authoritative: a present-but-broken block is a bug
    # or tampering and must never be silently worked around through the
    # legacy text that sits next to it.
    body = (
        "<!-- orbi:scene:v1 {broken -->\n"
        f"Orbi opened PR: {PR_URL} "
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4)"
    )
    with pytest.raises(scene.SceneError):
        scene.parse(body)


# ------------------------------------------- corrupted legacy text (SceneError)


def legacy_comment(fields: str) -> str:
    return f"Orbi opened PR: {PR_URL} {fields}"


def test_legacy_multiline_syntax_parses():
    body = (
        f"Orbi opened PR: {PR_URL}\n"
        "- base_branch: main\n"
        "- base_sha: abc123def456\n"
        f"- run_id={RUN_ID}\n"
    )
    assert scene.parse(body) == scene_for()


def test_legacy_paren_syntax_parses():
    body = legacy_comment(
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4)"
    )
    assert scene.parse(body) == scene_for()


def test_legacy_external_flag_parses():
    body = legacy_comment(
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 "
        "external=true)"
    )
    assert scene.parse(body) == scene_for(external="true")


def test_legacy_ignores_unrelated_lines_and_retired_fields():
    body = (
        f"Orbi opened PR: {PR_URL}\n"
        "- base_branch: main\n"
        "- base_sha: abc123def456\n"
        f"- run_id={RUN_ID}\n"
        "not a field\n"
        "- branch: orbi/owner-repo-issue-9\n"
        "- worktree: /srv/repo/.worktrees/x\n"
    )
    assert scene.parse(body) == scene_for()


@pytest.mark.parametrize("field,needle", [
    ("base_branch", "base_branch="),
    ("base_sha", " base_sha="),
    ("run_id", " run_id="),
    ("pr_url", PR_URL),
])
def test_legacy_fails_fast_when_a_field_is_missing(field: str, needle: str):
    body = legacy_comment(
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4)"
    ).replace(needle, "", 1)
    with pytest.raises(scene.SceneError, match=f"missing {field}"):
        scene.parse(body)


def test_legacy_fails_fast_on_an_invalid_run_id():
    body = legacy_comment(
        "(base_branch=main base_sha=abc123def456 run_id=run1)"
    )
    with pytest.raises(scene.SceneError, match="invalid run id"):
        scene.parse(body)


def test_legacy_ignores_a_field_part_without_key():
    body = legacy_comment(
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 =stray)"
    )
    assert scene.parse(body) == scene_for()


def test_legacy_fails_fast_when_the_head_has_no_lines():
    with pytest.raises(scene.SceneError, match="missing pr_url"):
        scene.parse("Orbi opened PR: ")


# --------------------------------------------------------- no scene (None)


@pytest.mark.parametrize("body", [
    "",
    None,
    123,
    f"<!-- orbi:run={RUN_ID} -->",
    f"Orbi started Pi: run_id={RUN_ID} base_branch=main",
    f"Orbi failed: boom (base_branch=main run_id={RUN_ID})",
    "**Orbi progress**\n- phase: review",
    "<!-- orbi:scene:v2 {} -->",
])
def test_parse_returns_none_when_the_body_carries_no_scene(body):
    assert scene.parse(body) is None
