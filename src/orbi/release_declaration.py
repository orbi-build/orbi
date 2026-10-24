"""Release-issue declaration parsing (Issue #1643).

The optional `## Release` override section of a release Issue body and its
strict, fail-fast parser, extracted verbatim from the release state machine
(`orbi.release`) so the syntax and the error copy can be reviewed and
changed without the ~1500-line release orchestration around them.

It is a leaf: it owns only the declaration contract and imports nothing
from `orbi.release` or `orbi.runner`. The supported `version_file` values
keep their single home in `orbi.release_git` — the same set selects the
metadata file `prepare_release_version` writes.
"""
from __future__ import annotations

import re

from orbi.release_git import RELEASE_VERSION_FILE_OPTIONS


# The optional machine-readable override section on a release Issue (Issue
# #905). When present, `- version:`, `- base_branch:` and `- scope:` (or
# `- scope_from_milestone:`) are parsed strictly. Without the section,
# GitHub's Milestone and repository configuration provide the contract. The
# declaration carries NO
# local test contract: test acceptance is the GitHub
# Actions CI result on the release commit (the #268 CI-wait gate).
RELEASE_SECTION = "## Release"


# Issue #831: the copy-paste guidance embedded in the declaration parse
# errors. The example is valid parser input (locked by test) and the
# `version_file` value range quoted in the errors is generated from
# RELEASE_VERSION_FILE_OPTIONS, so the error copy cannot drift from the
# parser's supported set.
RELEASE_DECLARATION_EXAMPLE = (
    f"{RELEASE_SECTION}\n"
    "\n"
    "- version: v1.2.0\n"
    "- base_branch: main\n"
    f"- version_file: {RELEASE_VERSION_FILE_OPTIONS[0]}\n"
    "- scope:\n"
    "  - #53\n"
    "  - #54"
)
_RELEASE_FIELD_EXAMPLES = {
    "version": "`- version: v1.2.0`",
    "base_branch": "`- base_branch: main`",
    "scope": "`- scope:` followed by `  - #53` item lines",
    "scope_from_milestone": "`- scope_from_milestone: v1.2.0`",
    "version_file": "`- version_file: pyproject.toml`",
}


def parse_release_declaration(body: str) -> dict:
    """Parse the optional `## Release` overrides of a release Issue body.

    Without the section this returns an empty override mapping; the release
    resolver obtains the contract from GitHub's Milestone and repository
    configuration. A present section remains the machine-readable contract
    (checkboxes are never parsed):

    ```markdown
    ## Release

    - version: v0.3.0
    - base_branch: main
    - scope:
      - #123
      - #124
    ```

    or, instead of the hand-listed `scope`, the scope derived from the
    Milestone whose title is the release version:

    ```markdown
    - scope_from_milestone: v0.3.0
    ```

    `version` is the exact tag name (no spaces) and `base_branch` the
    branch the release commit is frozen from. The declaration carries
    NO test contract: test acceptance is the GitHub
    Actions CI result on the release commit (the #268 CI-wait gate).
    `scope` lists the Issue/PR numbers verified one by one.
    Optional `version_file` selects a supported ecosystem metadata file
    (the default is `pyproject.toml`) or `none` to skip version metadata
    changes; its existence in the frozen release tree is verified at
    claim time by `verify_release_version_file`.
    Exactly one of `scope` / `scope_from_milestone` must be present:
    both (conflict) or neither fails fast. `scope_from_milestone` is
    the Milestone TITLE (no spaces); its scope is derived later by
    `derive_release_scope_from_milestone`. Every other deviation fails
    fast with the concrete field: missing section, missing or
    duplicated field, unknown key, empty value, empty scope or a
    malformed scope item. No guessing. Every failure message also
    carries the expected form (a copy-pasteable example for a missing
    section) so the user can repair the Issue body without reading the
    parser (Issue #831).
    """
    if not isinstance(body, str):
        raise ValueError("release declaration body must be a string")
    lines = body.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.strip() == RELEASE_SECTION
        )
    except StopIteration:
        # Issue #905: the GitHub Milestone is the default release contract.
        # An empty mapping is resolved after the frozen tree and repository
        # configuration are available; a present section remains strict for
        # compatibility with existing release tickets.
        return {}
    section: list[str] = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith("## "):
            break
        section.append(line)
    fields: dict[str, str] = {}
    scope: list[int] = []
    scope_open = False
    for line in section:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            content = stripped[2:].strip()
            if scope_open and re.fullmatch(r"#\d+", content):
                number = int(content[1:])
                if number < 1:
                    raise ValueError(
                        f"release declaration scope item {content!r} "
                        "must be a positive Issue or PR number "
                        "(expected form: `  - #53`)"
                    )
                scope.append(number)
                continue
            if scope_open and ":" not in content:
                raise ValueError(
                    f"release declaration scope item {content!r} is "
                    "malformed (expected `  - #N` under the `- scope:` "
                    "field, e.g. `  - #53`)"
                )
            # A `- key: value` line closes the scope list (and a
            # duplicated or unknown key is caught below).
            scope_open = False
            key, sep, value = content.partition(":")
            if not sep:
                raise ValueError(
                    f"release declaration field {key.strip()!r} is "
                    "malformed (expected form: `- key: value`, e.g. "
                    "`- version: v1.2.0`)"
                )
            key = key.strip()
            value = value.strip()
            if key in fields:
                raise ValueError(
                    f"release declaration field {key!r} is duplicated "
                    "(keep exactly one; expected form: "
                    f"{_RELEASE_FIELD_EXAMPLES[key]})"
                )
            if key == "scope":
                if value:
                    raise ValueError(
                        "release declaration `scope` must be a list of "
                        "`  - #N` items, not an inline value (expected "
                        f"form: {_RELEASE_FIELD_EXAMPLES['scope']})"
                    )
                scope_open = True
                fields["scope"] = ""
            elif key in ("version", "base_branch",
                         "scope_from_milestone", "version_file"):
                fields[key] = value
            else:
                raise ValueError(
                    f"release declaration has the unknown field {key!r} "
                    "(expected version, base_branch, scope, "
                    "scope_from_milestone or version_file; expected "
                    "form: `- key: value`, e.g. `- version: v1.2.0`)"
                )
        elif scope_open:
            raise ValueError(
                f"release declaration scope item {stripped!r} is "
                "malformed (expected `  - #N` under the `- scope:` "
                "field, e.g. `  - #53`)"
            )
        else:
            raise ValueError(
                f"release declaration line {stripped!r} is not a "
                "`- key: value` field or a scope item (expected form: "
                "`- key: value`, e.g. `- version: v1.2.0`)"
            )
    for key in ("version", "base_branch"):
        if key not in fields:
            raise ValueError(
                f"release declaration is missing the `{key}` field "
                "(expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
        if not fields[key]:
            raise ValueError(
                f"release declaration field `{key}` is empty "
                "(expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
    if "scope" in fields and "scope_from_milestone" in fields:
        raise ValueError(
            "release declaration must use exactly one of `scope` or "
            "`scope_from_milestone`, not both (keep one; expected "
            f"form: {_RELEASE_FIELD_EXAMPLES['scope']} OR "
            f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
        )
    if "scope" not in fields and "scope_from_milestone" not in fields:
        raise ValueError(
            "release declaration is missing the `scope` field or the "
            "`scope_from_milestone` field (exactly one of the two; "
            f"expected form: {_RELEASE_FIELD_EXAMPLES['scope']} OR "
            f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
        )
    if "scope" in fields and not scope:
        raise ValueError(
            "release declaration `scope` must list at least one "
            "`  - #N` Issue or PR number (expected form: "
            f"{_RELEASE_FIELD_EXAMPLES['scope']})"
        )
    for key in ("version", "base_branch"):
        if any(ch.isspace() for ch in fields[key]):
            raise ValueError(
                f"release declaration field `{key}` must not contain "
                "spaces (one token; expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
    if "scope_from_milestone" in fields:
        if not fields["scope_from_milestone"]:
            raise ValueError(
                "release declaration field `scope_from_milestone` is "
                "empty (expected form: "
                f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
            )
        if any(ch.isspace() for ch in fields["scope_from_milestone"]):
            raise ValueError(
                "release declaration field `scope_from_milestone` must "
                "not contain spaces (the Milestone TITLE is one token; "
                "expected form: "
                f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
            )
    version_file = fields.get("version_file", "pyproject.toml")
    if version_file not in RELEASE_VERSION_FILE_OPTIONS:
        raise ValueError(
            "release declaration field `version_file` is not supported "
            "(supported values: "
            f"{', '.join(RELEASE_VERSION_FILE_OPTIONS)}; expected "
            f"form: {_RELEASE_FIELD_EXAMPLES['version_file']})"
        )
    return {
        "version": fields["version"],
        "base_branch": fields["base_branch"],
        "scope": scope,
        "scope_from_milestone": fields.get("scope_from_milestone"),
        "version_file": version_file,
    }
