"""Cross-source release documentation contract.

Release pages are produced in the filesystem, while their English and
Chinese navigation entries are maintained independently in ``docs.json``.
These tests compare those sources instead of freezing a release snapshot.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DOCS_CONFIG = DOCS_DIR / "docs.json"
RELEASE_SLUG_PATTERN = re.compile(r"^(release-v(\d+)\.(\d+)\.(\d+))$")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


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
    """Return the release group pages for one navigation language."""
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
            return list(pages)
    raise AssertionError(f"{language_code} navigation has no release group")


def test_git_helper_fails_fast_on_nonzero_exit():
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git("rev-parse", "no-such-ref")


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
        for path in (DOCS_DIR / f"{slug}.mdx", DOCS_DIR / "zh" / f"{slug}.mdx"):
            text = path.read_text(encoding="utf-8")
            hashes = SHA_PATTERN.findall(text)
            assert hashes, f"{path} must record release Git objects"
            object_types = {
                value: git("cat-file", "-t", value) for value in hashes
            }
            tag_objects = [
                value for value, object_type in object_types.items()
                if object_type == "tag"
            ]
            commits = [
                value for value, object_type in object_types.items()
                if object_type == "commit"
            ]
            assert tag_objects, f"{path} must record an annotated tag object"
            assert commits, f"{path} must record the release commit"
            assert git("rev-parse", f"{commits[0]}^{{commit}}") == commits[0]


def test_only_the_highest_version_pages_carry_latest_markers():
    slugs = release_page_slugs("en")
    latest = max(slugs, key=release_version)
    for slug in slugs:
        en_title = (DOCS_DIR / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        zh_title = (DOCS_DIR / "zh" / f"{slug}.mdx").read_text(encoding="utf-8").splitlines()[0]
        is_latest = slug == latest
        assert ("(latest)" in en_title) is is_latest
        assert ("（最新）" in zh_title) is is_latest


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
