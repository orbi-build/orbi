"""Release documentation contract (Issue #128).

The docs `Releases`/`发布` group must mirror the REAL release state of
the repository — verified against origin with `git ls-remote --tags
origin` and `gh release list` (this run): tags `v0.1.0`, `v0.1.1`,
`v0.1.2` exist; GitHub Releases `v0.1.1`, `v0.1.2` exist; **no
`v0.1.3` tag or release exists**. Therefore:

- every listed release page corresponds to a real tag (no dead links,
  nothing real missing, nothing invented listed);
- the navigation lists the releases in DESCENDING version order (Issue
  #154): the latest release (v0.1.2) is the first entry in both the
  English `Releases` and the Chinese `发布` group, so the current
  release is found at the top of the list;
- `v0.1.3` must not appear anywhere in the docs (fact-based handling:
  no page, no navigation entry, no text claim);
- the release pages pin the REAL tag objects and commits (verified
  with actual `git` commands — a guessed SHA fails, the same
  anti-guessing contract as tests/test_release_v01.py);
- the v0.1.1 pages state that v0.1.1 is the CORRECTED release tag over
  v0.1.0 (the v0.1.0 record page stays the durable reconciliation
  artifact) and keep the link to it;
- the v0.1.2 pages mark the LATEST release of the current main.

The EN/ZH page-set parity and the nav↔file exact match are enforced by
tests/test_docs_i18n.py and tests/test_docs_site.py.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DOCS_CONFIG = DOCS_DIR / "docs.json"

# The real release objects (verified against origin with
# `git ls-remote --tags origin` at run time — Issue #128).
RELEASES = {
    "v0.1.0": {
        "tag_object": "912631d390b0b1f7039afac52125ecf7e1d92589",
        "commit": "335fa274e4b451a025bc2426b2a9fe7297ce8bb2",
    },
    "v0.1.1": {
        "tag_object": "b68d4f7d46582760ee04bac4308b785b4cc12c5d",
        "commit": "f82969e974b17803759493bad7e339bdcd6f463f",
    },
    "v0.1.2": {
        "tag_object": "209af556a9e0d49c068ee361fcacb7a1a49153cf",
        "commit": "abe48f1e64aa6fa2e76633023df5b52141b35082",
    },
}

# The only versions with a real tag on origin — and therefore the only
# versions the docs may list. No v0.1.3 exists (no tag, no GitHub
# Release), so it must not be documented.
REAL_RELEASE_SLUGS = [
    f"release-{version}"
    for version in ("v0.1.0", "v0.1.1", "v0.1.2")
]

# Issue #154: the navigation order is DESCENDING by version — the
# latest release first (v0.1.2, v0.1.1, v0.1.0).
RELEASE_SLUGS_LATEST_FIRST = list(reversed(REAL_RELEASE_SLUGS))


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


def release_group_pages(language_code: str) -> list[str]:
    """The page list of the release group of one navigation language
    (en: `Releases`, zh: `发布`)."""
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
    raise AssertionError(
        f"{language_code} navigation has no Releases/发布 group"
    )


def test_git_helper_fails_fast_on_nonzero_exit():
    """The helper must fail fast (no silent swallow) when git exits
    non-zero — the same contract as the other git smoke helpers."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git("rev-parse", "no-such-ref")


def test_release_group_lookup_fails_fast_when_the_release_group_is_missing(
    tmp_path, monkeypatch
):
    """A navigation language without a Releases/发布 group must fail
    fast with a named reason (no silent empty list)."""
    module = sys.modules[__name__]
    config = tmp_path / "docs.json"
    config.write_text(
        json.dumps({
            "navigation": {
                "languages": [
                    {
                        "language": "en",
                        "groups": [{"group": "Other", "pages": ["index"]}],
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DOCS_CONFIG", config)
    with pytest.raises(AssertionError, match=r"no Releases/发布 group"):
        module.release_group_pages("en")


def test_release_navigation_lists_exactly_the_real_releases_latest_first():
    """Acceptance (Issue #154): the English and Chinese release
    navigation match the real v0.1.x tags in DESCENDING version order —
    the latest release (v0.1.2) is the first entry, nothing real is
    missing, nothing invented is listed."""
    en_pages = release_group_pages("en")
    zh_pages = release_group_pages("zh")
    assert en_pages == RELEASE_SLUGS_LATEST_FIRST, (
        f"en Releases group must list exactly "
        f"{RELEASE_SLUGS_LATEST_FIRST} (latest first, descending "
        f"version order), got: {en_pages!r}"
    )
    assert zh_pages == [f"zh/{slug}" for slug in RELEASE_SLUGS_LATEST_FIRST], (
        f"zh 发布 group must list exactly "
        f"{[f'zh/{slug}' for slug in RELEASE_SLUGS_LATEST_FIRST]} (latest "
        f"first, descending version order), got: {zh_pages!r}"
    )


def test_every_listed_release_page_exists_in_both_languages():
    """No dead link: every listed release page has a real file (en at
    the docs root, zh under docs/zh/)."""
    for slug in REAL_RELEASE_SLUGS:
        en_path = DOCS_DIR / f"{slug}.mdx"
        zh_path = DOCS_DIR / "zh" / f"{slug}.mdx"
        assert en_path.is_file(), f"missing English release page: {en_path}"
        assert zh_path.is_file(), f"missing Chinese release page: {zh_path}"


def test_no_fabricated_release_version_anywhere_in_the_docs():
    """Fact-based handling of v0.1.3 (acceptance): origin has no
    `v0.1.3` tag and no `v0.1.3` GitHub Release, so the docs must not
    list, link or claim it anywhere — no page file, no navigation
    entry, no text mention."""
    for path in docs_files():
        content = path.read_text(encoding="utf-8")
        assert "v0.1.3" not in content, (
            f"{path.name} mentions v0.1.3, which has no tag or GitHub "
            f"Release on origin (invented version)"
        )
    config_text = DOCS_CONFIG.read_text(encoding="utf-8")
    assert "v0.1.3" not in config_text, (
        "docs.json navigation lists v0.1.3, which does not exist on origin"
    )
    stray = list(DOCS_DIR.glob("release-v0.1.3.mdx")) + list(
        (DOCS_DIR / "zh").glob("release-v0.1.3.mdx")
    )
    assert not stray, f"a v0.1.3 release page exists: {stray}"


def test_release_v011_pages_pin_the_real_tag_and_commit():
    """The v0.1.1 pages must pin the REAL tag object and commit: the
    SHAs are named in the page AND resolve to real objects of this
    repository (a guessed SHA fails)."""
    for rel in ("release-v0.1.1.mdx", "zh/release-v0.1.1.mdx"):
        text = (DOCS_DIR / rel).read_text(encoding="utf-8")
        tag_object = RELEASES["v0.1.1"]["tag_object"]
        commit = RELEASES["v0.1.1"]["commit"]
        assert tag_object in text, (
            f"{rel} must pin the real v0.1.1 tag object"
        )
        assert commit in text, f"{rel} must pin the real v0.1.1 commit"
    assert git("cat-file", "-t", RELEASES["v0.1.1"]["tag_object"]) == "tag", (
        "the pinned v0.1.1 tag object is not a tag object here"
    )
    assert git(
        "rev-parse", f"{RELEASES['v0.1.1']['commit']}^{{commit}}"
    ) == RELEASES["v0.1.1"]["commit"], (
        "the pinned v0.1.1 SHA does not resolve to a commit"
    )


def test_release_v011_pages_state_that_they_correct_v010():
    """v0.1.1 is the CORRECTED release tag over v0.1.0 (the real
    GitHub Release body): the pages must say so and keep the link to
    the durable v0.1.0 reconciliation record page."""
    en = (DOCS_DIR / "release-v0.1.1.mdx").read_text(encoding="utf-8")
    zh = (DOCS_DIR / "zh" / "release-v0.1.1.mdx").read_text(encoding="utf-8")
    assert "v0.1.0" in en and "correct" in en.lower(), (
        "en v0.1.1 page must state that it corrects v0.1.0"
    )
    assert "/release-v0.1.0" in en, (
        "en v0.1.1 page must link the v0.1.0 record page"
    )
    assert "v0.1.0" in zh and "修正" in zh, (
        "zh v0.1.1 page must state that it corrects (修正) v0.1.0"
    )
    assert "/zh/release-v0.1.0" in zh, (
        "zh v0.1.1 page must link the Chinese v0.1.0 record page"
    )


def test_release_v012_pages_pin_the_real_tag_and_commit_and_mark_latest():
    """v0.1.2 is the LATEST release (the real GitHub Release
    `Muyan Pilot v0.1.2`, published 2026-08-27): the pages must pin
    the real tag object and commit and mark it as the latest release."""
    for rel in ("release-v0.1.2.mdx", "zh/release-v0.1.2.mdx"):
        text = (DOCS_DIR / rel).read_text(encoding="utf-8")
        tag_object = RELEASES["v0.1.2"]["tag_object"]
        commit = RELEASES["v0.1.2"]["commit"]
        assert tag_object in text, (
            f"{rel} must pin the real v0.1.2 tag object"
        )
        assert commit in text, f"{rel} must pin the real v0.1.2 commit"
    assert git("cat-file", "-t", RELEASES["v0.1.2"]["tag_object"]) == "tag", (
        "the pinned v0.1.2 tag object is not a tag object here"
    )
    assert git(
        "rev-parse", f"{RELEASES['v0.1.2']['commit']}^{{commit}}"
    ) == RELEASES["v0.1.2"]["commit"], (
        "the pinned v0.1.2 SHA does not resolve to a commit"
    )
    en = (DOCS_DIR / "release-v0.1.2.mdx").read_text(encoding="utf-8")
    zh = (DOCS_DIR / "zh" / "release-v0.1.2.mdx").read_text(encoding="utf-8")
    assert "latest" in en.lower(), (
        "en v0.1.2 page must mark it the latest release"
    )
    assert "最新" in zh, (
        "zh v0.1.2 page must mark it the latest release (最新)"
    )
