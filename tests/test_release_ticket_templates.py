"""Release-ticket template contract (Issue #749).

`parse_release_declaration` (src/orbi/release.py) is a strict, fail-fast
machine-readable contract, but before #749 it lived only in the parser
docstring and in past release tickets — every new release ticket was
copied from a historical one, and orbi-build/orbi-cloud#246 stalled on
a missing `version_file`. The two template files below are the contract's
canonical home, and these tests feed the real template FILES through
the real parser (never a re-typed copy) so the templates and the parser
cannot drift.

The body template lives at `.github/release-ticket-template.md`, not at
the Issue-suggested `docs/` path — on purpose: `docs/` is the Mintlify
site tree where every `.md` must be a navigation page
(`test_docs_site.py::test_docs_navigation_matches_the_actual_pages_exactly`)
and `release-*` names at the docs root are reserved for `release-vX.Y.Z`
pages
(`test_docs_releases.py::test_docs_files_are_not_release_pages_with_unparseable_names`).
Both collisions were verified with a real failing run before this file
existed. `gh issue create --body-file
.github/release-ticket-template.md` consumes the body template directly
(the `--body-file` flag verified against `gh issue create --help`).

Issue #734 (filed 2026-09-11, before #749 existed) suggested the UI
path `.github/ISSUE_TEMPLATE/release-task.md`; #749 — the same author's
later, deliberate decision, merged first — named it `release.md`, so
the name stays (a second file would put two release templates into the
GitHub issue-creation UI). What #734 adds on top of the merged #749
delivery is locked by the last two tests below: the frontmatter-stripped
UI body with the `vX.Y.Z` placeholders replaced by a REAL version must
parse to exactly the right declaration (the user journey a ticket
actually takes), and the live `## Release` section must never carry the
legacy `test_command` (the memo section's #569 do-not-write warning is
documentation of the contract, not a declaration field).
"""
import re
from pathlib import Path

from orbi.delivery_labels import READY_LABEL, RELEASE_LABEL
from orbi.release import parse_release_declaration
from orbi.runner import RELEASE_SECTION

ROOT = Path(__file__).resolve().parent.parent
UI_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "release.md"
BODY_TEMPLATE = ROOT / ".github" / "release-ticket-template.md"

# The parser's known keys (src/orbi/release.py parse_release_declaration;
# `test_command` is the legacy accepted-and-ignored key, Issue #569).
KNOWN_KEYS = frozenset({
    "version",
    "base_branch",
    "scope",
    "scope_from_milestone",
    "version_file",
    "test_command",
})


def test_both_release_ticket_templates_exist():
    assert UI_TEMPLATE.is_file(), f"missing UI template: {UI_TEMPLATE}"
    assert BODY_TEMPLATE.is_file(), f"missing body template: {BODY_TEMPLATE}"


def test_ui_template_front_matter_prelabels_the_release_ticket():
    front, separator, _rest = (
        UI_TEMPLATE.read_text(encoding="utf-8").partition("\n---\n")
    )
    assert separator, "the UI template must open with YAML front matter"
    assert re.fullmatch(
        rf"---\nname: .+\nabout: .+\ntitle: .+\n"
        rf"labels: {RELEASE_LABEL}, {READY_LABEL}",
        front,
    ), front


def test_ui_template_body_matches_the_body_template_verbatim():
    _front, separator, rest = (
        UI_TEMPLATE.read_text(encoding="utf-8").partition("\n---\n")
    )
    assert separator, "the UI template must open with YAML front matter"
    assert rest == "\n" + BODY_TEMPLATE.read_text(encoding="utf-8")


def test_templates_parse_the_three_declared_scope_forms():
    for path in (UI_TEMPLATE, BODY_TEMPLATE):
        text = path.read_text(encoding="utf-8")
        # Form 1 — the section a real ticket carries: scope_from_milestone.
        declaration = parse_release_declaration(text)
        assert declaration["version"] == "vX.Y.Z"
        assert declaration["base_branch"] == "main"
        assert declaration["scope_from_milestone"] == "vX.Y.Z"
        assert declaration["version_file"] == "pyproject.toml"
        # Forms 2 and 3 — the alternates the template demonstrates in
        # ```markdown fences (hand-listed scope; version_file for
        # non-Python projects, the orbi-cloud #246 stall), extracted from the file
        # itself and fed through the same strict parser.
        alternates = re.findall(
            r"```markdown\n(## Release\n.*?)```", text, re.DOTALL,
        )
        assert len(alternates) == 2, (
            f"{path} must demonstrate exactly two alternate declarations"
        )
        hand_listed = parse_release_declaration(alternates[0])
        assert hand_listed["scope"] == [123, 124]
        assert hand_listed["scope_from_milestone"] is None
        with_version_file = parse_release_declaration(alternates[1])
        assert with_version_file["scope_from_milestone"] == "vX.Y.Z"
        assert with_version_file["version_file"] == "package.json"


def test_every_declaration_field_shown_in_the_templates_is_known():
    for path in (UI_TEMPLATE, BODY_TEMPLATE):
        fields = set(re.findall(
            r"^- ([a-z_]+):", path.read_text(encoding="utf-8"),
            re.MULTILINE,
        ))
        assert fields, f"{path} demonstrates no declaration fields"
        unknown = fields - KNOWN_KEYS
        assert not unknown, (
            f"{path} shows fields the parser does not know: {sorted(unknown)}"
        )


def test_ui_template_body_with_a_real_substituted_version_parses():
    # Issue #734's user journey: GitHub strips the front matter when the
    # ticket is created, the maintainer replaces the `vX.Y.Z` placeholders
    # with the real version, and `parse_release_declaration` must accept
    # that body as-is — the exact failure that burned #717.
    _front, separator, rest = (
        UI_TEMPLATE.read_text(encoding="utf-8").partition("\n---\n")
    )
    assert separator, "the UI template must open with YAML front matter"
    body = rest.lstrip("\n").replace("vX.Y.Z", "v0.4.9")
    declaration = parse_release_declaration(body)
    assert declaration["version"] == "v0.4.9"
    assert declaration["base_branch"] == "main"
    assert declaration["scope_from_milestone"] == "v0.4.9"
    assert declaration["version_file"] == "pyproject.toml"
    assert declaration["test_command"] is None


def test_live_declaration_section_carries_no_legacy_test_command():
    # The skeleton a ticket KEEPS — the first exact `## Release` section,
    # the same slice the parser reads, before the memo section — must
    # never suggest the legacy accepted-and-ignored `test_command`
    # (Issue #734); the memo's #569 do-not-write warning is the only
    # place the name may appear. `partition` finds the live section even
    # though the memo's ```markdown fences repeat the heading.
    for path in (UI_TEMPLATE, BODY_TEMPLATE):
        text = path.read_text(encoding="utf-8")
        _before, marker, rest = text.partition(f"\n{RELEASE_SECTION}\n")
        assert marker, f"{path} has no live `{RELEASE_SECTION}` section"
        live = rest.split("\n## ", 1)[0]
        assert "test_command" not in live, (
            f"{path} suggests the legacy test_command in its live "
            f"`{RELEASE_SECTION}` section"
        )
