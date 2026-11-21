#!/usr/bin/env python3
"""The platform label definitions, read from data (Issue #2347).

`labels.toml` — shipped in this package — is the only place a platform
label name is written down. Each `[[label]]` entry carries a `role`, the
key the engine asks for, plus the name, color and description `orbi
setup` aligns. The `[reserved]` table carries a name the engine
reserves or routes on but does not own (`bug`): setup never creates or
updates it.

Nothing in the engine names a label. The rest of the engine asks this
module for the role -> name table: the module reads the file once at
start (`install(load_default())`), and every later question (`active()`)
is a lookup in the same table.

The one overridable role is the queue. Precedence, per repository:
`.github/orbi.toml` `dispatch_label` > host `orbi.toml`
`dispatch_label` > the `queue` entry here (the config merge in
`repo_config.resolve_policy` already applies the first two to one
`dispatch_label`). Every other role is engine data and changes only
with an engine release.

Validation is fail-fast: a missing file, malformed TOML, an unknown role,
a duplicated role, a duplicated label name, a missing queue role, an
invalid color or an empty description raises :class:`LabelsError` naming
the problem — at start, never as a silently missing delivery state.
"""
from __future__ import annotations

import dataclasses
import importlib.resources
import re
import tomllib
from pathlib import Path

# The role vocabulary: one key per label, exactly one `queue`. The
# lifecycle roles are the delivery states; the rest order, route or gate a
# ticket without being a state of its own. Every platform role appears
# exactly once in `labels.toml`; the reserved roles are read-only engine
# names (setup never creates or updates those labels).
PLATFORM_ROLES = (
    "queue",
    "in_progress",
    "pr_opened",
    "fix_needed",
    "merged",
    "blocked",
    "awaiting_merge",
    "priority",
    "epic",
    "release",
    "content_only",
    "ops_only",
    "human_review",
    "needs_detail",
)
LIFECYCLE_ROLES = PLATFORM_ROLES[:7]
NON_LIFECYCLE_ROLES = PLATFORM_ROLES[7:] + ("bug",)
RESERVED_ROLES = ("bug",)
ROLES = PLATFORM_ROLES + RESERVED_ROLES
QUEUE_ROLE = "queue"

LABELS_FILE = "labels.toml"
COLOR_PATTERN = re.compile(r"^[0-9a-fA-F]{6}$")

_ACTIVE: Labels | None = None


class LabelsError(ValueError):
    """The label definitions are missing or invalid (fail at start)."""


@dataclasses.dataclass(frozen=True)
class LabelDef:
    """One label definition: the role key plus what setup aligns."""

    role: str
    name: str
    color: str
    description: str


@dataclasses.dataclass(frozen=True)
class Labels:
    """The role -> name table the engine reads.

    `entries` are the platform labels in file order (the setup target);
    `reserved` holds the reserved role names (no color/description).
    """

    entries: tuple[LabelDef, ...]
    reserved: tuple[tuple[str, str], ...]

    def role(self, role: str) -> LabelDef:
        for entry in self.entries:
            if entry.role == role:
                return entry
        for reserved_role, name in self.reserved:
            if reserved_role == role:
                return LabelDef(role, name, "", "")
        raise LabelsError(
            f"{LABELS_FILE} has no {role!r} role; the roles are "
            f"{list(PLATFORM_ROLES)} and reserved {list(RESERVED_ROLES)}"
        )

    def name(self, role: str) -> str:
        """The configured label name for one role."""
        return self.role(role).name

    def lifecycle_states(self) -> frozenset[str]:
        """The delivery-state label names (the queue entry included)."""
        return frozenset(self.name(role) for role in LIFECYCLE_ROLES)

    def non_lifecycle_labels(self) -> frozenset[str]:
        """Every non-lifecycle marker name (priority, reserved, markers)."""
        return frozenset(self.name(role) for role in NON_LIFECYCLE_ROLES)

    def with_queue(self, name: str | None) -> Labels:
        """A copy whose queue role is `name` (`None` keeps this one)."""
        if not name or name == self.name(QUEUE_ROLE):
            return self
        entries = tuple(
            dataclasses.replace(entry, name=name)
            if entry.role == QUEUE_ROLE
            else entry
            for entry in self.entries
        )
        return dataclasses.replace(self, entries=entries)

    def label_defs(self) -> list[dict]:
        """The platform definitions in the `orbi setup` alignment shape."""
        return [
            {
                "name": entry.name,
                "color": entry.color,
                "description": entry.description,
            }
            for entry in self.entries
        ]


def default_path() -> Path:
    """The `labels.toml` shipped with the engine package."""
    return Path(importlib.resources.files("orbi").joinpath(LABELS_FILE))


def load(path: Path) -> Labels:
    """Parse and validate one label definition file (fail fast).

    A missing file, malformed TOML, a non-`[[label]]` shape, an unknown,
    duplicated or missing role, a duplicated name, an invalid color and an
    empty description are all :class:`LabelsError` with the problem named.
    """
    path = Path(path)
    if not path.is_file():
        raise LabelsError(
            f"{LABELS_FILE} missing: {path} (the engine's single source of "
            "the platform label names; reinstall the engine package)"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LabelsError(f"malformed {LABELS_FILE}: {path}: {exc}") from exc
    raw_entries = data.get("label")
    if not isinstance(raw_entries, list):
        raise LabelsError(
            f"malformed {LABELS_FILE}: {path}: expected a [[label]] array "
            "of role/name/color/description entries"
        )
    entries: list[LabelDef] = []
    seen_roles: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: each label entry must be "
                "a table with role/name/color/description"
            )
        role = raw.get("role")
        name = raw.get("name")
        color = raw.get("color")
        description = raw.get("description")
        if role not in PLATFORM_ROLES:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: unknown label role "
                f"{role!r}; the platform roles are {list(PLATFORM_ROLES)}"
            )
        if role in seen_roles:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: duplicate role {role!r}"
            )
        seen_roles.add(role)
        if not isinstance(name, str) or not name:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: the {role!r} role has no "
                "non-empty name"
            )
        if name in seen_names:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: duplicate label name "
                f"{name!r}"
            )
        seen_names.add(name)
        if not isinstance(color, str) or not COLOR_PATTERN.fullmatch(color):
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: label {name} has an "
                f"invalid color (6-character hex required): {color!r}"
            )
        if not isinstance(description, str) or not description:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: label {name} has no "
                "non-empty description"
            )
        entries.append(LabelDef(role, name, color, description))
    missing = [role for role in PLATFORM_ROLES if role not in seen_roles]
    if missing:
        raise LabelsError(
            f"malformed {LABELS_FILE}: {path}: missing label roles "
            f"{missing}; every platform role must appear exactly once "
            "(the queue role is required)"
        )
    raw_reserved = data.get("reserved")
    if not isinstance(raw_reserved, dict):
        raise LabelsError(
            f"malformed {LABELS_FILE}: {path}: expected a [reserved] table "
            f"naming {list(RESERVED_ROLES)}"
        )
    reserved: list[tuple[str, str]] = []
    for role in RESERVED_ROLES:
        name = raw_reserved.get(role)
        if not isinstance(name, str) or not name:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: the reserved role "
                f"{role!r} needs a non-empty name"
            )
        if name in seen_names:
            raise LabelsError(
                f"malformed {LABELS_FILE}: {path}: reserved name {name!r} "
                "duplicates a platform label name"
            )
        reserved.append((role, name))
    return Labels(tuple(entries), tuple(reserved))


def load_default() -> Labels:
    """Load the `labels.toml` shipped with this engine."""
    return load(default_path())


def install(table: Labels | None) -> None:
    """Bind the role -> name table this process reads (`None` resets)."""
    global _ACTIVE
    _ACTIVE = table


def active() -> Labels:
    """The role -> name table, loaded from the shipped file on first use.

    The Runner binds it at start (`runner.validate_config`), so a broken
    file fails the tick with its reason instead of a missing label later.
    """
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_default()
    return _ACTIVE
