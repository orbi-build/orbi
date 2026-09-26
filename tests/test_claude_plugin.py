"""Contract tests for the Claude plugin (Issue #1389).

The plugin at ``integrations/claude-plugin/`` is Markdown-and-JSON-only: no
hooks, no MCP server, no scripts and no package installs. It leans on the
user's own ``gh`` CLI. These tests pin the shape the claude.com plugin
directory and the repository's contract require, and they carry two
counter-proofs (a fixture copy with ``hooks/`` and a fixture copy without
``homepage``) so a future change cannot silently turn the assertions into
no-ops.

The checks are pure file reads: no ``orbi`` import, no network, no ``gh``.
"""
import json
import re
import shutil
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
ALLOWED_REF_TOKENS = {"plugin-listing", "plugin-readme", "plugin-skill"}
FORBIDDEN_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
FORBIDDEN_ENTRIES = {"hooks", "bin", ".mcp.json"}
ALLOWED_SUFFIXES = {".md", ".json"}
MAX_FILE_BYTES = 256 * 1024
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


def check_no_forbidden_entries(plugin_dir: Path = PLUGIN_DIR) -> None:
    """No hooks, no MCP file, no bin/ and no editor/archive droppings: the
    plugin is Markdown and JSON only (Issue #1389)."""
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


def test_only_markdown_json_and_license_files():
    for path in plugin_files():
        rel = path.relative_to(PLUGIN_DIR)
        assert rel.suffix in ALLOWED_SUFFIXES or rel.name == "LICENSE", (
            f"unexpected file type in plugin: {rel}"
        )


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
