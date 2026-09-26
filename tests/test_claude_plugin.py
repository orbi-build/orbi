"""Contract tests for the Claude plugin (Issue #1389).

The plugin at ``integrations/claude-plugin/`` is Markdown and JSON only, plus
the repository ``LICENSE`` and the one non-Markdown asset the claude.com
directory reads at its default path, ``.claude-plugin/icon.svg`` (Issue
#1399): no hooks, no MCP server, no scripts and no package installs. It leans
on the user's own ``gh`` CLI. These tests pin the shape the claude.com plugin
directory and the repository's contract require, and they carry five
counter-proofs (a fixture copy with ``hooks/``, a fixture copy without
``homepage``, a fixture copy missing the ``author.url`` ref, a fixture copy
with ``skills/x/logo.svg``, and a fixture copy reading the config from the
working tree) so a future change cannot silently turn the assertions into
no-ops.

The checks are pure file reads: no ``orbi`` import, no network, no ``gh``.
"""
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "integrations" / "claude-plugin"
MANIFEST_REL = Path(".claude-plugin") / "plugin.json"

# claude.com plugin name rule: lowercase alphanumeric and hyphens, 1-64
# chars, must start and end alphanumeric.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
HOMEPAGE = "https://orbi.build/?ref=plugin-listing"
AUTHOR_URL = "https://orbi.build/?ref=plugin-listing"
ALLOWED_REF_TOKENS = {"plugin-listing", "plugin-readme", "plugin-skill"}
FORBIDDEN_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
FORBIDDEN_ENTRIES = {"hooks", "bin", ".mcp.json"}
ALLOWED_SUFFIXES = {".md", ".json"}
# Issue #1399: the claude.com directory reads the plugin icon from this exact
# default path. It is the single permitted non-Markdown/JSON, non-LICENSE asset.
ICON_REL = Path(".claude-plugin") / "icon.svg"
ALLOWED_SVG_RELS = {ICON_REL}
MAX_FILE_BYTES = 256 * 1024
ICON_VIEWBOX = "0 0 512 512"
ICON_MIN_PX = 128
# An ``href``/``xlink:href`` whose value names a scheme or a protocol-relative
# URL is external; an in-document fragment (``#...``) is not (Issue #1399).
EXTERNAL_HREF_RE = re.compile(
    r"""(?:xlink:)?href\s*=\s*["'](?:[a-z][a-z0-9+.-]*:|//)""",
    re.IGNORECASE,
)
CREDENTIAL_MARKERS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
ORBI_BUILD_LINK_RE = re.compile(r"https?://\S*orbi\.build\S*")
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
REF_RE = re.compile(r"[?&]ref=([a-z0-9_-]+)")
PLUGIN_TOKEN_ROWS = ("plugin-listing", "plugin-readme", "plugin-skill")


def plugin_files(plugin_dir: Path = PLUGIN_DIR) -> list[Path]:
    return sorted(p for p in plugin_dir.rglob("*") if p.is_file())


def load_manifest(plugin_dir: Path = PLUGIN_DIR) -> dict:
    return json.loads((plugin_dir / MANIFEST_REL).read_text(encoding="utf-8"))


def read_front_matter(path: Path) -> dict:
    """Parse a Markdown file's YAML front matter, failing on a missing or
    malformed block."""
    text = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(text)
    assert match, f"{path}: missing YAML front matter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), f"{path}: front matter is not a mapping"
    return data


def check_manifest_homepage(plugin_dir: Path = PLUGIN_DIR) -> None:
    """The claude.com listing page takes the homepage link from the manifest,
    so it must be present and carry the attribution token (Issue #1389)."""
    homepage = load_manifest(plugin_dir).get("homepage")
    assert homepage == HOMEPAGE, (
        f"plugin homepage must be {HOMEPAGE!r}, got {homepage!r}"
    )


def check_manifest_author_url(plugin_dir: Path = PLUGIN_DIR) -> None:
    """The claude.com listing links the author to orbi.build, so the
    manifest ``author.url`` must carry the attribution token (Issue #1398)."""
    author = load_manifest(plugin_dir).get("author")
    assert isinstance(author, dict), "manifest author must be a mapping"
    url = author.get("url")
    assert url == AUTHOR_URL, (
        f"plugin author.url must be {AUTHOR_URL!r}, got {url!r}"
    )


def check_allowed_file_types(plugin_dir: Path = PLUGIN_DIR) -> None:
    """Markdown, JSON and the repository LICENSE only, plus the single
    directory icon ``.claude-plugin/icon.svg`` (Issue #1399). Any other
    ``.svg`` — e.g. under ``skills/`` — fails."""
    for path in plugin_files(plugin_dir):
        rel = path.relative_to(plugin_dir)
        if rel.suffix == ".svg":
            assert rel in ALLOWED_SVG_RELS, (
                f"unexpected .svg file in plugin: {rel}"
            )
            continue
        assert rel.suffix in ALLOWED_SUFFIXES or rel.name == "LICENSE", (
            f"unexpected file type in plugin: {rel}"
        )


def viewbox_size(viewbox: str) -> tuple[float, float]:
    parts = viewbox.split()
    assert len(parts) == 4, f"viewBox needs 4 numbers, got {viewbox!r}"
    return float(parts[2]), float(parts[3])


def check_icon(plugin_dir: Path = PLUGIN_DIR) -> None:
    """The directory icon must exist, parse as XML, be a square >= 128px
    SVG with no script and no external href (Issue #1399)."""
    icon_path = plugin_dir / ICON_REL
    assert icon_path.is_file(), f"plugin icon missing: {ICON_REL}"
    raw = icon_path.read_bytes()
    assert len(raw) <= MAX_FILE_BYTES, (
        f"{ICON_REL} is {len(raw)} bytes (max 256 KiB)"
    )
    root = ET.fromstring(raw)
    tag = root.tag.rsplit("}", 1)[-1]
    assert tag == "svg", f"{ICON_REL}: root must be <svg>, got <{tag}>"
    viewbox = root.get("viewBox")
    assert viewbox == ICON_VIEWBOX, (
        f"{ICON_REL}: viewBox must be {ICON_VIEWBOX!r}, got {viewbox!r}"
    )
    width, height = viewbox_size(viewbox)
    assert width == height, (
        f"{ICON_REL}: viewBox must be square, got {width}x{height}"
    )
    assert width >= ICON_MIN_PX, (
        f"{ICON_REL}: viewBox must be >= {ICON_MIN_PX}px, got {width}x{height}"
    )
    text = raw.decode("utf-8")
    assert "<script" not in text.lower(), (
        f"{ICON_REL}: must not contain <script>"
    )
    assert not EXTERNAL_HREF_RE.search(text), (
        f"{ICON_REL}: must not link an external href"
    )


def check_no_forbidden_entries(plugin_dir: Path = PLUGIN_DIR) -> None:
    """No hooks, no MCP file, no bin/ and no editor/archive droppings: the
    plugin carries no executable or configuration payload (Issue #1389)."""
    for path in sorted(plugin_dir.rglob("*")):
        rel = path.relative_to(plugin_dir)
        assert path.name not in FORBIDDEN_NAMES, f"forbidden file: {rel}"
        assert path.name not in FORBIDDEN_ENTRIES, f"forbidden entry: {rel}"


def words_outside_code(text: str) -> int:
    """Word count that ignores fenced code blocks."""
    count = 0
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            count += len(line.split())
    return count


# --- Manifest -----------------------------------------------------------------


def test_manifest_parses():
    manifest = load_manifest()
    assert isinstance(manifest, dict)


def test_manifest_name_matches_directory_rule():
    name = load_manifest().get("name")
    assert isinstance(name, str) and NAME_RE.match(name), (
        f"invalid plugin name: {name!r}"
    )


def test_manifest_required_fields_present():
    manifest = load_manifest()
    for field in ("version", "description", "license"):
        assert manifest.get(field), f"manifest missing {field!r}"
    author = manifest.get("author")
    assert isinstance(author, dict) and author.get("name"), (
        "manifest missing author.name"
    )


def test_manifest_homepage():
    check_manifest_homepage()


def test_manifest_author_url():
    check_manifest_author_url()


def test_manifest_version_is_0_1_3():
    assert load_manifest().get("version") == "0.1.3"


# --- README and license -------------------------------------------------------


def test_readme_has_at_least_150_words_outside_code():
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    words = words_outside_code(text)
    assert words >= 150, f"README has only {words} words outside code blocks"


def test_words_outside_code_ignores_fenced_blocks():
    text = "alpha beta\n```\ngamma delta epsilon\n```\nzeta\n"
    assert words_outside_code(text) == 3


def test_readme_has_data_section():
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^##\s+Data\b", text, re.MULTILINE), (
        "README is missing its '## Data' section"
    )


def test_readme_data_section_lists_gh_api():
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"^##\s+Data\b(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match, "README is missing its '## Data' section"
    assert "gh api" in match.group(1), (
        "README '## Data' must list the `gh api` command the plugin runs"
    )


def requirements_section(text: str) -> str:
    match = re.search(
        r"^##\s+Requirements\b(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "README is missing its '## Requirements' section"
    return match.group(1)


def test_readme_declares_both_tiers():
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert "## Write an Issue (no Orbi needed)" in text, (
        "README must head the no-Orbi tier explicitly"
    )
    assert "## Hand it to Orbi (repositories Orbi delivers)" in text, (
        "README must head the Orbi tier explicitly"
    )


def test_readme_drops_the_commands_only_work_with_orbi_claim():
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert "These two commands only add behavior" not in text, (
        "README must not claim /orbi:ship only works in a repository Orbi delivers"
    )


def test_readme_requirements_qualifies_gh_to_claude_code():
    section = requirements_section(
        (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    )
    gh_bullets = [line for line in section.splitlines() if "`gh`" in line]
    assert gh_bullets, "README '## Requirements' must mention `gh`"
    assert any("Claude Code" in line for line in gh_bullets), (
        "README '## Requirements' must qualify the `gh` line to the Claude Code path"
    )


def test_license_is_byte_identical_to_repository_license():
    assert (PLUGIN_DIR / "LICENSE").read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()


# --- Skill and command files --------------------------------------------------


def markdown_front_matter_files() -> list[Path]:
    files = sorted(PLUGIN_DIR.glob("skills/*/SKILL.md"))
    files += sorted(PLUGIN_DIR.glob("commands/*.md"))
    return files


def test_skill_and_commands_have_parsable_front_matter_with_description():
    files = markdown_front_matter_files()
    assert files, "plugin has no SKILL.md or commands/*.md files"
    for path in files:
        data = read_front_matter(path)
        assert isinstance(data.get("description"), str), (
            f"{path}: front matter description must be a single string"
        )


def test_skill_folder_name_matches_front_matter_name():
    for path in sorted(PLUGIN_DIR.glob("skills/*/SKILL.md")):
        data = read_front_matter(path)
        assert data.get("name") == path.parent.name, (
            f"{path}: skill name {data.get('name')!r} != folder {path.parent.name!r}"
        )


# --- Folder contents ----------------------------------------------------------


def test_no_symlinks():
    for path in sorted(PLUGIN_DIR.rglob("*")):
        assert not path.is_symlink(), f"symlink in plugin: {path}"


def test_no_forbidden_entries():
    check_no_forbidden_entries()


def test_no_file_over_256_kib():
    for path in plugin_files():
        size = path.stat().st_size
        assert size <= MAX_FILE_BYTES, f"{path} is {size} bytes (max 256 KiB)"


def test_only_markdown_json_license_and_icon_files():
    check_allowed_file_types()


# --- Directory icon (Issue #1399) ---------------------------------------------


def test_directory_icon_is_a_valid_square_svg():
    check_icon()


# --- Links and credentials ----------------------------------------------------


def test_every_orbi_build_link_carries_a_ref_token():
    for path in plugin_files():
        text = path.read_text(encoding="utf-8")
        for url in ORBI_BUILD_LINK_RE.findall(text):
            match = REF_RE.search(url)
            assert match, f"{path}: orbi.build link without ref token: {url}"
            assert match.group(1) in ALLOWED_REF_TOKENS, (
                f"{path}: unexpected ref token {match.group(1)!r} in {url}"
            )


def test_no_environment_credentials_referenced():
    for path in plugin_files():
        text = path.read_text(encoding="utf-8")
        for marker in CREDENTIAL_MARKERS:
            assert marker not in text, f"{path}: references credential {marker!r}"


def test_agents_md_token_table_has_plugin_tokens():
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for token in PLUGIN_TOKEN_ROWS:
        assert re.search(rf"^{re.escape(token)}\s+\S", text, re.MULTILINE), (
            f"AGENTS.md token table is missing {token!r}"
        )


# --- active_milestone from the default branch (Issue #1395) -------------------

RAW_MEDIA_TYPE = "application/vnd.github.raw"
SKILL_REL = Path("skills") / "write-ai-ready-issue" / "SKILL.md"
SHIP_REL = Path("commands") / "ship.md"
# A hand-off that tells Claude to read the config from a local copy instead of
# the default branch is exactly the #1395 defect.
LOCAL_CONFIG_READ_RE = re.compile(
    r"(?:read|reads|reading|open|opens|load|loads)[^.\n]*?"
    r"\.github/orbi\.toml[^.\n]*?"
    r"(?:working tree|local checkout|local|disk|checkout)",
    re.IGNORECASE,
)


def read_plugin_file(plugin_dir: Path, rel: Path) -> str:
    return (plugin_dir / rel).read_text(encoding="utf-8")


def find_local_config_reads(text: str) -> list[str]:
    """Lines that tell Claude to read ``.github/orbi.toml`` from a local copy
    rather than the default branch on GitHub (Issue #1395)."""
    return [
        line.strip()
        for line in text.splitlines()
        if LOCAL_CONFIG_READ_RE.search(line)
    ]


def check_config_reads_from_default_branch(plugin_dir: Path = PLUGIN_DIR) -> None:
    """The hand-off reads ``active_milestone`` through ``gh api repos/`` with
    the raw media type, and never from a local checkout (Issue #1395)."""
    for rel in (SKILL_REL, SHIP_REL):
        text = read_plugin_file(plugin_dir, rel)
        assert "gh api repos/" in text, (
            f"{rel}: must read .github/orbi.toml through `gh api repos/`"
        )
        assert RAW_MEDIA_TYPE in text, (
            f"{rel}: must request the raw media type {RAW_MEDIA_TYPE!r}"
        )
        local_reads = find_local_config_reads(text)
        assert not local_reads, (
            f"{rel}: reads the local .github/orbi.toml: {local_reads}"
        )


def test_skill_and_ship_read_config_from_default_branch():
    check_config_reads_from_default_branch()


def test_skill_checks_the_milestone_is_open():
    text = read_plugin_file(PLUGIN_DIR, SKILL_REL)
    assert "milestones?state=open" in text, (
        "SKILL.md must check the value against the repository's open milestones"
    )
    assert "per_page=100" in text, (
        "SKILL.md must page the milestone query so a later open milestone is seen"
    )
    assert "--paginate" in text, (
        "SKILL.md must paginate the milestone query, not only widen its page size"
    )
    assert re.search(r"among\s+the\s+open\s+Milestones", text), (
        "SKILL.md must say the value must be among the open milestones"
    )
    assert re.search(r"without `--milestone`", text), (
        "SKILL.md must say to omit --milestone when the value is not open"
    )


# --- Counter-proof ------------------------------------------------------------


def test_fixture_copy_with_hooks_fails_the_no_hooks_assertion(tmp_path):
    fixture = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, fixture)
    hooks = fixture / "hooks"
    hooks.mkdir()
    (hooks / "hooks.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError):
        check_no_forbidden_entries(fixture)


def test_fixture_copy_without_homepage_fails_the_homepage_assertion(tmp_path):
    fixture = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, fixture)
    manifest_path = fixture / MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["homepage"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError):
        check_manifest_homepage(fixture)


def test_fixture_copy_without_author_url_ref_fails_the_author_assertion(tmp_path):
    fixture = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, fixture)
    manifest_path = fixture / MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["author"]["url"] = "https://orbi.build/"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError):
        check_manifest_author_url(fixture)


def test_fixture_copy_with_svg_outside_the_icon_path_fails_the_type_assertion(
    tmp_path,
):
    fixture = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, fixture)
    skill_dir = fixture / "skills" / "x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "logo.svg").write_text("<svg/>", encoding="utf-8")
    with pytest.raises(AssertionError):
        check_allowed_file_types(fixture)


def test_fixture_skill_reading_the_working_tree_fails_the_default_branch_assertion(
    tmp_path,
):
    fixture = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, fixture)
    skill_path = fixture / SKILL_REL
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8")
        + "\nRead `.github/orbi.toml` from the working tree.\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        check_config_reads_from_default_branch(fixture)
