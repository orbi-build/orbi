"""The platform label definitions are data (Issue #2347).

`labels.toml` (package data) is the only place a platform label name is
written down. These tests pin the contract:

- the loader's fail-fast validation (missing file, malformed TOML, an
  unknown role, a duplicated role, a missing queue role);
- table-driven over EVERY role: rename the role's label in a test
  `labels.toml`, install the table, and the engine paths that read or
  write that role use the new name and never the old one;
- the queue precedence: repository `.github/orbi.toml` >
  host `orbi.toml` > `labels.toml`;
- no file under `src/` (outside `labels.toml`) carries a platform label
  name as a string literal.
"""
import ast
import pathlib

import pytest

from orbi import claim, delivery_labels, labels as label_data, milestone
from orbi import runner_health

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "orbi"

# role -> the delivery_labels constant that answers for that role.
ROLE_CONSTANTS = {
    "queue": "READY_LABEL",
    "in_progress": "IN_PROGRESS_LABEL",
    "pr_opened": "PR_OPENED_LABEL",
    "fix_needed": "FIX_NEEDED_LABEL",
    "merged": "MERGED_LABEL",
    "blocked": "BLOCKED_LABEL",
    "awaiting_merge": "AWAITING_MERGE_LABEL",
    "needs_detail": "NEEDS_DETAIL_LABEL",
    "epic": "EPIC_LABEL",
    "release": "RELEASE_LABEL",
    "priority": "P0_LABEL",
    "content_only": "CONTENT_ONLY_LABEL",
    "ops_only": "OPS_LABEL",
    "human_review": "HUMAN_REVIEW_LABEL",
    "bug": "BUG_LABEL",
}


@pytest.fixture(autouse=True)
def restore_the_shipped_table():
    yield
    label_data.install(None)


def shipped_definitions() -> tuple[list[dict], dict[str, str]]:
    import tomllib

    data = tomllib.loads(
        label_data.default_path().read_text(encoding="utf-8"),
    )
    return data["label"], data["reserved"]


def write_table(tmp_path, rename: dict[str, str]) -> pathlib.Path:
    """A test labels.toml with the given role -> new name overrides."""
    entries, reserved = shipped_definitions()
    lines: list[str] = []
    for entry in entries:
        lines.append("[[label]]")
        for key in ("role", "name", "color", "description"):
            value = (
                rename[entry["role"]]
                if key == "name" and entry["role"] in rename
                else entry[key]
            )
            lines.append(f'{key} = "{value}"')
        lines.append("")
    lines.append("[reserved]")
    for role, name in reserved.items():
        lines.append(f'{role} = "{rename.get(role, name)}"')
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def install_table(tmp_path, rename: dict[str, str]) -> label_data.Labels:
    table = label_data.load(write_table(tmp_path, rename))
    label_data.install(table)
    return table


# --- the shipped file -------------------------------------------------------


def test_the_shipped_file_defines_every_role_exactly_once():
    table = label_data.load_default()
    assert [entry.role for entry in table.entries] == list(
        label_data.PLATFORM_ROLES
    )
    assert dict(table.reserved) == {"bug": "bug"}
    # The defaults are today's names: no rename ships with this change.
    assert [entry.name for entry in table.entries] == [
        "ai-ready", "ai-in-progress", "ai-pr-opened", "ai-fix-needed",
        "ai-merged", "ai-blocked", "ai-awaiting-merge", "p0", "ai-epic",
        "ai-release", "ai-content-only", "ai-ops-only", "ai-human-review",
        "ai-needs-detail",
    ]


def test_the_package_ships_the_definition_file():
    """`labels.toml` is package data: the wheel carries it."""
    import tomllib

    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert "labels.toml" in pyproject["tool"]["setuptools"]["package-data"][
        "orbi"
    ]
    assert label_data.default_path() == SRC / "labels.toml"


# --- fail-fast validation ---------------------------------------------------


def test_a_missing_file_names_the_file(tmp_path):
    missing = tmp_path / "labels.toml"
    with pytest.raises(label_data.LabelsError, match="labels.toml missing"):
        label_data.load(missing)


def test_a_duplicated_role_names_the_role(tmp_path):
    text = label_data.default_path().read_text(encoding="utf-8")
    # Duplicate the queue entry (same role, distinct name).
    duplicate = text.replace(
        '[[label]]\nrole = "in_progress"',
        '[[label]]\nrole = "queue"\nname = "second-queue"\n'
        'color = "fbca04"\ndescription = "x"\n\n[[label]]\n'
        'role = "in_progress"',
        1,
    )
    path = tmp_path / "labels.toml"
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="duplicate role 'queue'"):
        label_data.load(path)


def test_a_missing_queue_role_names_the_problem(tmp_path):
    entries, reserved = shipped_definitions()
    lines = []
    for entry in entries:
        if entry["role"] == "queue":
            continue
        lines.append("[[label]]")
        for key in ("role", "name", "color", "description"):
            lines.append(f'{key} = "{entry[key]}"')
        lines.append("")
    lines.append("[reserved]")
    lines.extend(f'{role} = "{name}"' for role, name in reserved.items())
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="queue"):
        label_data.load(path)


def test_malformed_toml_names_the_problem(tmp_path):
    path = tmp_path / "labels.toml"
    path.write_text('label = ["\n', encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="malformed labels.toml"):
        label_data.load(path)


def test_the_start_binds_the_table_from_the_shipped_file(tmp_path):
    """`runner.validate_config` reads the engine's `labels.toml` at start
    (Issue #2347): a broken file fails there with the problem named,
    instead of a silently missing delivery state later."""
    from orbi import runner

    from orbi.config import RunnerConfig

    config = RunnerConfig(
        source_repos=["acme/app"], repo_dir=REPO_ROOT,
        deploy_home=REPO_ROOT,
        prompt=REPO_ROOT / "prompts" / "prompt.md",
        prompt_review=REPO_ROOT / "prompts" / "prompt_review.md",
        skills=(), context_files=(), repositories=(),
    )
    runner.validate_config(config)
    assert delivery_labels.READY_LABEL == label_data.load_default().name(
        "queue"
    )
    # The failure the start inherits: the same loader, on a missing file.
    with pytest.raises(label_data.LabelsError, match="labels.toml missing"):
        label_data.load(tmp_path / "labels.toml")


# --- table-driven over every role ------------------------------------------


@pytest.mark.parametrize("role", label_data.ROLES)
def test_every_role_reads_and_writes_the_configured_name(tmp_path, role):
    entries, reserved = shipped_definitions()
    names = {entry["role"]: entry["name"] for entry in entries}
    names.update(reserved)
    new_name = f"role-{role}"
    table = install_table(tmp_path, {role: new_name})
    old_name = names[role]

    # 1. The delivery-label constants answer with the table's name.
    for constant_role, constant in ROLE_CONSTANTS.items():
        value = getattr(delivery_labels, constant)
        if constant_role == role:
            assert value == new_name
        else:
            assert value == names[constant_role]
    if role in ("epic", "release", "priority", "content_only", "ops_only",
                "human_review", "needs_detail", "bug"):
        assert new_name in delivery_labels.NON_LIFECYCLE_LABELS
    else:
        assert new_name in delivery_labels.LIFECYCLE_STATES

    # 2. Every label transition reads the table: the old name is never
    # written, and the role's event writes the new one.
    queue = table.name("queue")
    events = (
        delivery_labels.EVENT_CLAIM,
        delivery_labels.EVENT_PR_OPENED,
        delivery_labels.EVENT_FIX_NEEDED,
        delivery_labels.EVENT_MERGED,
        delivery_labels.EVENT_RELEASE_WAITING,
        delivery_labels.EVENT_HUMAN_REVIEW_WAITING,
        delivery_labels.EVENT_REQUEUE,
        delivery_labels.EVENT_BLOCKED,
        delivery_labels.EVENT_AWAITING_MERGE,
    )
    for event in events:
        add, remove = delivery_labels.label_patch(
            event, [old_name], ready_label=queue,
        )
        assert old_name not in add
        assert old_name not in remove
    added = {
        delivery_labels.EVENT_CLAIM: "in_progress",
        delivery_labels.EVENT_PR_OPENED: "pr_opened",
        delivery_labels.EVENT_FIX_NEEDED: "fix_needed",
        delivery_labels.EVENT_MERGED: "merged",
        delivery_labels.EVENT_RELEASE_WAITING: "queue",
        delivery_labels.EVENT_REQUEUE: "queue",
        delivery_labels.EVENT_BLOCKED: "blocked",
        delivery_labels.EVENT_AWAITING_MERGE: "awaiting_merge",
    }
    for event, added_role in added.items():
        if added_role == role:
            assert new_name in delivery_labels.label_patch(
                event, [], ready_label=queue,
            )[0]

    # 3. The state decisions compare with the table's names.
    assert delivery_labels.is_pickup_eligible([new_name]) is (role == "queue")
    assert delivery_labels.is_resumable([new_name]) is (
        role in ("pr_opened", "fix_needed", "awaiting_merge")
    )
    assert delivery_labels.needs_human_intervention([new_name]) is (
        role == "blocked"
    )
    assert delivery_labels.has_delivery_state([new_name]) is (
        role in label_data.LIFECYCLE_ROLES and role != "queue"
    )

    # 4. The scans and message paths build their query from the table.
    scans = claim.ready_searches(
        None, dispatch_label=table.name("queue"),
    )
    tokens = set(" ".join(scans).split())
    assert f"-label:{old_name}" not in tokens
    assert f"label:{old_name}" not in tokens
    assert f"label:{table.name('queue')}" in tokens
    assert f"label:{table.name('priority')}" in tokens
    assert f"label:{table.name('bug')}" in tokens
    for lifecycle_role in ("in_progress", "pr_opened", "fix_needed",
                           "merged", "blocked"):
        assert f"-label:{table.name(lifecycle_role)}" in tokens
    takeover = claim.external_takeover_search(table.name("queue"))
    assert old_name not in takeover
    assert table.name("queue") in takeover
    stages = milestone.release_stage_labels()
    if role in label_data.LIFECYCLE_ROLES[1:]:
        assert new_name in stages

    # 5. The health-Issue writer labels with the table's names.
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return "[]"

    runner_health.create_health_issue(
        "acme/app", "crash_loop", "boom", run_command=fake_run,
        dispatch_label=table.name("queue"),
    )
    create = next(
        line for line in commands
        if line[0:3] == ["gh", "issue", "create"]
    )
    labels_added = [
        create[index + 1]
        for index, value in enumerate(create[:-1]) if value == "--label"
    ]
    assert table.name("bug") in labels_added
    assert table.name("queue") in labels_added
    assert old_name not in labels_added


def test_reserved_and_unknown_roles():
    """The reserved role is read from the [reserved] table; an unknown
    role is a fail-fast error naming the vocabulary."""
    table = label_data.load_default()
    assert table.role("bug").name == "bug"
    with pytest.raises(label_data.LabelsError, match="no 'nope' role"):
        table.role("nope")


def test_with_queue_overlays_only_the_queue_role(tmp_path):
    table = install_table(tmp_path, {})
    overlaid = table.with_queue("ship")
    assert overlaid.name("queue") == "ship"
    assert overlaid.name("blocked") == table.name("blocked")
    # No override (or the same name) keeps the same table.
    assert table.with_queue(None) is table
    assert table.with_queue(table.name("queue")) is table


def test_a_file_without_the_reserved_table_fails(tmp_path):
    entries, _reserved = shipped_definitions()
    lines = []
    for entry in entries:
        lines.append("[[label]]")
        for key in ("role", "name", "color", "description"):
            lines.append(f'{key} = "{entry[key]}"')
        lines.append("")
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="reserved"):
        label_data.load(path)


def test_an_empty_reserved_name_fails(tmp_path):
    entries, reserved = shipped_definitions()
    lines = []
    for entry in entries:
        lines.append("[[label]]")
        for key in ("role", "name", "color", "description"):
            lines.append(f'{key} = "{entry[key]}"')
        lines.append("")
    lines.append("[reserved]")
    lines.extend(f'{role} = ""' for role in reserved)
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="reserved role"):
        label_data.load(path)


def test_a_reserved_name_duplicating_a_platform_name_fails(tmp_path):
    entries, reserved = shipped_definitions()
    lines = []
    for entry in entries:
        lines.append("[[label]]")
        for key in ("role", "name", "color", "description"):
            lines.append(f'{key} = "{entry[key]}"')
        lines.append("")
    lines.append("[reserved]")
    lines.extend(f'{role} = "{entries[0]["name"]}"' for role in reserved)
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(label_data.LabelsError, match="duplicates"):
        label_data.load(path)


# --- queue precedence -------------------------------------------------------


def test_the_queue_default_is_the_file(tmp_path):
    table = install_table(tmp_path, {})
    assert delivery_labels.queue_label(None) == table.name("queue")
    assert delivery_labels.queue_label("") == table.name("queue")


def test_the_host_override_wins_over_the_file(tmp_path):
    install_table(tmp_path, {})
    assert delivery_labels.queue_label("host-ship") == "host-ship"


def test_the_repository_override_wins_over_the_host():
    from orbi import repo_config
    from orbi.config import RunnerConfig

    policy = repo_config.RepoPolicy(dispatch_label="ship", sha="abc")
    config = RunnerConfig(
        source_repos=["acme/app"], repo_dir=REPO_ROOT,
        deploy_home=REPO_ROOT,
        dispatch_label="host-ship",
    )
    resolved = repo_config.resolve_policy(config, policy)
    assert resolved.dispatch_label == "ship"
    assert delivery_labels.queue_label(resolved.dispatch_label) == "ship"
    # No repository key: the host value stays the effective override.
    empty = repo_config.RepoPolicy(sha="abc")
    resolved = repo_config.resolve_policy(config, empty)
    assert delivery_labels.queue_label(resolved.dispatch_label) == "host-ship"


# --- no literal label names under src/ -------------------------------------


def _string_literals(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


# The strings that coincide with a label name and are NOT label names.
VOCABULARY = {
    ("delivery_labels.py", "bug"),  # the reserved role key
    ("labels.py", "bug"),  # the reserved role key (roles + parser)
    ("github.py", "p0"),  # the journal `priority=p0` field value
    ("language_terms.py", "p0"),  # the display term key
    ("runner_health.py", "bug"),  # the health-check `kind=bug` value
}


def label_literal_offenders(src_dir: pathlib.Path,
                             names: set[str]) -> list[str]:
    """Every string literal under `src_dir` that IS a platform label name.

    The allowlist is the vocabulary that coincides with a label name and
    is NOT one: the `labels.toml` role keys (`bug`) and the journal /
    display vocabulary (`priority=p0`, the health-check `kind=bug`, the
    `p0` display term). No file may be added here for a label write.
    """
    offenders = []
    for path in sorted(src_dir.glob("*.py")):
        for literal in _string_literals(path):
            if literal in names and (path.name, literal) not in VOCABULARY:
                offenders.append(f"{path.name}: {literal!r}")
    return offenders


def test_no_engine_module_writes_a_platform_label_name():
    """No module under `src/` carries a platform label name (Issue #2347).

    The scan matches a string literal that IS a label name (the acceptance
    shape: `--label "ai-ready"` must not exist in code). Prose that
    mentions a name in a sentence ("the Issue stays ai-blocked") is user
    copy, not a label write, and is not a match.
    """
    table = label_data.load_default()
    names = {entry.name for entry in table.entries}
    names.update(name for _role, name in table.reserved)
    assert label_literal_offenders(SRC, names) == []


def test_the_literal_guard_catches_a_hard_coded_name(tmp_path):
    """The guard above must fail on a planted literal, not just pass."""
    (tmp_path / "engine.py").write_text(
        'QUEUE = "ai-ready"\n', encoding="utf-8",
    )
    assert label_literal_offenders(tmp_path, {"ai-ready"}) == [
        "engine.py: 'ai-ready'",
    ]


