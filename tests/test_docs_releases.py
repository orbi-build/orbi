"""Cross-source release documentation contract.

Release pages are produced in the filesystem, while their English and
Chinese navigation entries are maintained independently in ``docs.json``.
These tests compare those sources instead of freezing a release snapshot.
"""
import json
import re
import sys
from pathlib import Path

import pytest

from conftest import git

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DOCS_CONFIG = DOCS_DIR / "docs.json"
RELEASE_SLUG_PATTERN = re.compile(r"^(release-v(\d+)\.(\d+)\.(\d+))$")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def docs_files() -> list[Path]:
    return sorted(
        path for path in DOCS_DIR.rglob("*")
        if path.is_file() and path.suffix in (".md", ".mdx")
    )


def release_page_slugs(language_code: str) -> set[str]:
    directory = DOCS_DIR / "zh" if language_code == "zh" else DOCS_DIR
    return {
        match.group(1)
        for path in directory.glob("release-v*.mdx")
        if (match := RELEASE_SLUG_PATTERN.fullmatch(path.stem))
    }


def release_version(slug: str) -> tuple[int, int, int]:
    match = RELEASE_SLUG_PATTERN.fullmatch(slug)
    assert match is not None, f"invalid release slug: {slug!r}"
    return tuple(int(match.group(index)) for index in (2, 3, 4))


def release_group_pages(language_code: str) -> list[str]:
    """Return the flattened release group pages for one navigation
    language.

    Issue #1096: the release group nests one collapsed subgroup
    (`Earlier releases`/`历史版本`); its pages are flattened so the
    parity and order checks cover the collapsed entries too.
    """
    assert DOCS_CONFIG.is_file(), f"missing Mintlify config: {DOCS_CONFIG}"
    config = json.loads(DOCS_CONFIG.read_text(encoding="utf-8"))
    navigation = config.get("navigation")
    assert isinstance(navigation, dict) and navigation, (
        "docs.json has no navigation section"
    )
    languages = navigation.get("languages")
    assert isinstance(languages, list) and languages, (
        "docs.json navigation must use the languages array (Mintlify i18n)"
    )
    lang = next(
        (
            entry for entry in languages
            if isinstance(entry, dict)
            and entry.get("language") == language_code
        ),
        None,
    )
    assert lang is not None, f"docs.json has no {language_code!r} language"
    groups = lang.get("groups")
    assert isinstance(groups, list) and groups, (
        f"navigation language {language_code!r} has no groups"
    )
    for group in groups:
        if isinstance(group, dict) and group.get("group") in ("Releases", "发布"):
            pages = group.get("pages")
            assert isinstance(pages, list) and pages, (
                f"release group of {language_code!r} has no pages"
            )
            flat: list[str] = []
            for entry in pages:
                if isinstance(entry, dict):
                    nested = entry.get("pages")
                    assert isinstance(nested, list) and nested, (
                        f"release subgroup of {language_code!r} has no pages"
                    )
                    flat.extend(nested)
                else:
                    flat.append(entry)
            return flat
    raise AssertionError(f"{language_code} navigation has no release group")


def test_release_group_lookup_fails_fast_when_the_release_group_is_missing(
    tmp_path, monkeypatch
):
    module = sys.modules[__name__]
    config = tmp_path / "docs.json"
    config.write_text(
        json.dumps({
            "navigation": {
                "languages": [{
                    "language": "en",
                    "groups": [{"group": "Other", "pages": ["index"]}],
                }]
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DOCS_CONFIG", config)
    with pytest.raises(AssertionError, match=r"no release group"):
        module.release_group_pages("en")


def test_release_navigation_matches_files_and_is_latest_first():
    en_files = release_page_slugs("en")
    zh_files = release_page_slugs("zh")
    en_pages = release_group_pages("en")
    zh_pages = release_group_pages("zh")

    assert en_files == zh_files, "English and Chinese release files must match"
    assert set(en_pages) == en_files, (
        "English release navigation must exactly match release page files"
    )
    assert {page.removeprefix("zh/") for page in zh_pages} == zh_files, (
        "Chinese release navigation must exactly match release page files"
    )
    assert en_pages == sorted(en_files, key=release_version, reverse=True), (
        "English releases must be navigated in descending semantic version order"
    )
    assert zh_pages == [f"zh/{slug}" for slug in en_pages], (
        "Chinese release navigation must mirror English navigation"
    )


def test_release_navigation_detects_a_page_missing_from_navigation(tmp_path, monkeypatch):
    module = sys.modules[__name__]
    docs = tmp_path / "docs"
    (docs / "zh").mkdir(parents=True)
    (docs / "release-v1.0.0.mdx").write_text("# release\n", encoding="utf-8")
    (docs / "zh" / "release-v1.0.0.mdx").write_text("# 发布\n", encoding="utf-8")
    config = docs / "docs.json"
    config.write_text(json.dumps({
        "navigation": {"languages": [
            {"language": "en", "groups": [{"group": "Releases", "pages": ["index"]}]},
            {"language": "zh", "groups": [{"group": "发布", "pages": ["zh/index"]}]},
        ]}
    }), encoding="utf-8")
    monkeypatch.setattr(module, "DOCS_DIR", docs)
    monkeypatch.setattr(module, "DOCS_CONFIG", config)
    with pytest.raises(AssertionError, match="exactly match"):
        module.test_release_navigation_matches_files_and_is_latest_first()


def test_release_pages_exist_in_both_languages():
    for slug in release_page_slugs("en"):
        assert (DOCS_DIR / f"{slug}.mdx").is_file()
        assert (DOCS_DIR / "zh" / f"{slug}.mdx").is_file()


def test_release_pages_pin_resolvable_tag_objects_and_commits():
    for slug in release_page_slugs("en"):
        version = slug.removeprefix("release-")
        tag_object = git(REPO_ROOT, "rev-parse", f"refs/tags/{version}")
        commit = git(REPO_ROOT, "rev-parse", f"refs/tags/{version}^{{commit}}")
        assert git(REPO_ROOT, "cat-file", "-t", tag_object) == "tag", (
            f"{version} must be an annotated tag"
        )
        assert git(REPO_ROOT, "rev-parse", f"{commit}^{{commit}}") == commit
        for path in (DOCS_DIR / f"{slug}.mdx", DOCS_DIR / "zh" / f"{slug}.mdx"):
            text = path.read_text(encoding="utf-8")
            hashes = SHA_PATTERN.findall(text)
            assert tag_object in hashes, f"{path} must record {version}'s tag object"
            assert commit in hashes, f"{path} must record {version}'s release commit"


def test_only_the_highest_version_pages_carry_latest_markers():
    slugs = release_page_slugs("en")
    latest = max(slugs, key=release_version)
    for slug in slugs:
        en_title = (DOCS_DIR / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        zh_title = (DOCS_DIR / "zh" / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        is_latest = slug == latest
        assert ("(latest)" in en_title) is is_latest
        assert ("（最新）" in zh_title) is is_latest


# Issue #910: the two pre-generator releases moved their records into
# the GitHub Release bodies and their docs pages are gone. The tags
# stay (a tag is never moved or deleted), so these two are the pinned
# exception to tag/page completeness — and one-way: no page for them
# may come back (the orphan check below still applies to them).
PRE_GENERATOR_TAGS = frozenset({"v0.1.0", "v0.1.1"})


def release_tags(repo_root: Path) -> set[str]:
    """Every released tag visible in the checkout. Requires the tag refs
    (CI provides them with `fetch-tags: true`)."""
    tags = {
        tag for tag in git(repo_root, "tag", "--list", "v*").splitlines() if tag.strip()
    }
    assert tags, (
        "no tags found in the checkout — the release pages cannot be "
        "checked for completeness (CI fetches tags with fetch-tags: true)"
    )
    return tags


def tags_at_head(repo_root: Path) -> set[str]:
    """The released tags whose commit is HEAD (Issue #1363).

    The release page is committed one commit AFTER its tag, so on the
    tagged commit itself HEAD is that tag's commit and the page has not
    landed yet — only those tags may lack their page. Once HEAD advances
    to the docs-sync commit the exemption is gone, so a page that never
    lands still fails the completeness check."""
    head = git(repo_root, "rev-parse", "HEAD")
    return {
        tag for tag in release_tags(repo_root)
        if git(repo_root, "rev-parse", f"refs/tags/{tag}^{{commit}}") == head
    }


def test_every_released_tag_has_its_release_page():
    """Tag/page completeness (Issue #910): every released tag must have
    a corresponding docs page and vice versa — the file/nav parity tests
    above cannot see a release whose docs sync never ran, which is how
    a version-sequence gap like the skipped v0.5.1 becomes visible in
    the navigation without any test failing. Requires the tag refs in
    the checkout (CI provides them with `fetch-tags: true`).

    Issue #1363: the page commit follows the tag, so the tag at HEAD is
    exempt while its page commit has not landed — a normal release tag
    commit is green, while any older tag without a page still fails."""
    tags = release_tags(REPO_ROOT)
    expected_slugs = {
        f"release-{tag}" for tag in tags - PRE_GENERATOR_TAGS
    }
    pending_slugs = {
        f"release-{tag}" for tag in tags_at_head(REPO_ROOT) - PRE_GENERATOR_TAGS
    }
    for language_code in ("en", "zh"):
        slugs = release_page_slugs(language_code)
        missing_pages = (expected_slugs - pending_slugs) - slugs
        assert not missing_pages, (
            f"released tags without a {language_code} docs page: "
            f"{sorted(missing_pages)}"
        )
        orphan_pages = slugs - expected_slugs
        assert not orphan_pages, (
            f"{language_code} release pages without a released tag "
            f"(the pre-generator records live on the GitHub Releases, "
            f"not in docs/): {sorted(orphan_pages)}"
        )


def _release_repo(tmp_path: Path) -> Path:
    """A throwaway checkout for the tag/page completeness cases (Issue
    #1363): real git refs, a real HEAD, an empty ``docs/``."""
    repo = tmp_path / "repo"
    (repo / "docs" / "zh").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "release-test@example.com")
    git(repo, "config", "user.name", "Release Test")
    return repo


def _commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_tag_at_head_without_its_page_yet_passes(tmp_path, monkeypatch):
    """Issue #1363: the release page commit lands one commit after the
    tag, so on the tagged commit HEAD is the tag commit and its page is
    not there yet. The completeness check must pass in that state."""
    module = sys.modules[__name__]
    repo = _release_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _commit_all(repo, "chore: prepare release v1.0.0")
    git(repo, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "DOCS_DIR", repo / "docs")

    module.test_every_released_tag_has_its_release_page()


def test_older_tag_without_its_page_fails(tmp_path, monkeypatch):
    """Issue #1363: only the tag at HEAD is exempt — an older released
    tag whose page never landed must still fail the check."""
    module = sys.modules[__name__]
    repo = _release_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _commit_all(repo, "chore: prepare release v0.9.0")
    git(repo, "tag", "-a", "v0.9.0", "-m", "release v0.9.0")
    (repo / "README.md").write_text("# repo v1\n", encoding="utf-8")
    _commit_all(repo, "chore: prepare release v1.0.0")
    git(repo, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "DOCS_DIR", repo / "docs")

    with pytest.raises(
        AssertionError,
        match=r"released tags without a en docs page: \['release-v0\.9\.0'\]",
    ):
        module.test_every_released_tag_has_its_release_page()


def test_release_pages_carry_no_release_machine_audit_blocks():
    """The docs page is for readers, not the release machine's audit
    trail (Issue #910): no release page carries the `## Scope (verified
    item by item)` or `## Pre-release gates` sections or a `run_id=`
    line — those stay on the GitHub Release. Pins both the generator
    change and the one-time trim of the existing pages."""
    for directory in (DOCS_DIR, DOCS_DIR / "zh"):
        for path in sorted(directory.glob("release-v*.mdx")):
            text = path.read_text(encoding="utf-8")
            assert "Scope (verified item by item)" not in text, path
            assert "Pre-release gates" not in text, path
            assert not re.search(r"^run_id=", text, re.MULTILINE), path


def test_release_pages_have_matching_version_titles():
    for slug in release_page_slugs("en"):
        version = slug.removeprefix("release-")
        en_title = (DOCS_DIR / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        zh_title = (DOCS_DIR / "zh" / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        assert version in en_title
        assert version in zh_title


def test_docs_files_are_not_release_pages_with_unparseable_names():
    for path in docs_files():
        if path.parent == DOCS_DIR and path.name.startswith("release-"):
            assert RELEASE_SLUG_PATTERN.fullmatch(path.stem), path
        if path.parent == DOCS_DIR / "zh" and path.name.startswith("release-"):
            assert RELEASE_SLUG_PATTERN.fullmatch(path.stem), path
