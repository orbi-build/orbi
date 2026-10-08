"""The release-docs generator and its workflow (Issue #1482).

The docs-site release pages used to be written by the engine's release
state machine. They now come from `tools/release_docs.py`, run by
`.github/workflows/release-docs.yml` on `release: published`. These tests
pin the two contracts:

- the script renders the EN/ZH pages, inserts the `docs.json`
  navigation entries, moves the `(latest)` marker and is idempotent;
- the workflow file is structurally valid (the equivalent of an
  `actionlint` pass that is not installed in the sandbox): the published
  trigger, the manual `tag` input, `contents: write`, a tagged `main`
  checkout, the generator run, and the bounded rebase-and-retry push.
"""
import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "release_docs.py"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "release-docs.yml"

TAG = "v1.2.3"
TAG_OBJECT = "a" * 40
RELEASE_COMMIT = "b" * 40
RELEASE_URL = f"https://github.com/orbi-build/orbi/releases/tag/{TAG}"
PUBLISHED_AT = "2026-09-28T12:00:00Z"


def load_script():
    spec = importlib.util.spec_from_file_location("release_docs_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _page_fields(page: str) -> tuple[dict[str, str], list[str]]:
    """Parse a generated page's YAML frontmatter (key -> unquoted value)
    and return it with the body lines (Issue #1571)."""
    lines = page.splitlines()
    assert lines and lines[0] == "---", "the page must open with frontmatter"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields, lines[end + 1:]


def _changelog_body(first_bullet: str, *more_bullets: str) -> str:
    bullets = "\n".join(f"- {bullet}" for bullet in (first_bullet, *more_bullets))
    return f"# {TAG}\n\n## Changelog\n\n{bullets}\n"


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


def test_release_docs_page_emits_frontmatter_title_and_derived_description():
    """Issue #1571: the generated page carries the title in YAML
    frontmatter (Mintlify renders it as the single H1) and a meta
    description built from the first changelog bullet; no body line
    opens a second `# …` H1."""
    script = load_script()
    body = _changelog_body(
        "First change ([Issue #1](https://example.test/1); "
        "[PR #2](https://example.test/2))",
        "Second change ([Issue #3](https://example.test/3))",
    )
    for language, title, tail in (
        ("en", f"{TAG} release (latest)", " and 1 more change."),
        ("zh", f"{TAG} 发布（最新）", "，另有 1 项改动。"),
    ):
        page = script.release_docs_page(
            version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
            published_at=PUBLISHED_AT, release_url=RELEASE_URL,
            issue_number=42, body=body, language=language,
        )
        fields, body_lines = _page_fields(page)
        assert fields["title"] == title
        description = fields["description"]
        assert description, "the description must not be empty"
        assert len(description) <= 155, description
        assert "First change" in description
        assert "Second change" not in description
        assert description.endswith(tail)
        assert not any(line.startswith("# ") for line in body_lines)


def test_release_docs_page_description_pluralizes_the_other_changes():
    script = load_script()
    body = _changelog_body("Only change", "Another", "And a third")
    en_page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=42,
        body=body, language="en",
    )
    zh_page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=42,
        body=body, language="zh",
    )
    en_description = _page_fields(en_page)[0]["description"]
    zh_description = _page_fields(zh_page)[0]["description"]
    assert en_description == (
        f"Orbi {TAG} release notes: Only change and 2 more changes."
    )
    assert zh_description == (
        f"Orbi {TAG} 发布说明：Only change，另有 2 项改动。"
    )


def test_release_docs_page_description_truncates_at_a_word_boundary():
    script = load_script()
    body = _changelog_body(
        " ".join(["alpha"] * 60) + " ([Issue #1](https://example.test/1))",
    )
    for language in ("en", "zh"):
        page = script.release_docs_page(
            version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
            published_at=PUBLISHED_AT, release_url=RELEASE_URL,
            issue_number=42, body=body, language=language,
        )
        description = _page_fields(page)[0]["description"]
        assert len(description) <= 155
        assert "alpha" in description
        assert description.split()[-1] == "alpha"


def test_truncate_at_word_boundary_keeps_a_space_less_text_whole():
    script = load_script()
    assert script.truncate_at_word_boundary("short", 50) == "short"
    assert script.truncate_at_word_boundary("a" * 200, 50) == "a" * 50
    assert script.truncate_at_word_boundary("one two three", 8) == "one two"


def test_changelog_bullets_ignore_code_fences_and_later_sections():
    script = load_script()
    notes = (
        f"# {TAG}\n\n## Changelog\n\n"
        "```text\n- not a bullet\n```\n\n"
        "- real bullet\n\n"
        "## Contributors\n\n- tag: v1.2.3\n"
    )
    assert script.changelog_bullets(notes) == ["real bullet"]


def test_release_docs_page_description_falls_back_without_changelog_bullets():
    script = load_script()
    page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=42,
        body=f"# {TAG}\n\nNo deliveries are linked to this milestone.\n",
        language="en",
    )
    zh_page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=42,
        body=f"# {TAG}\n\nNo deliveries are linked to this milestone.\n",
        language="zh",
    )
    assert _page_fields(page)[0]["description"] == (
        f"Orbi {TAG} release notes: tag and verification for the GitHub Release."
    )
    assert _page_fields(zh_page)[0]["description"] == (
        f"Orbi {TAG} 发布说明：GitHub Release 的 tag 与验证记录。"
    )


def test_release_docs_page_renders_en_and_zh_titles():
    script = load_script()
    kwargs = dict(
        version=TAG,
        tag_object=TAG_OBJECT,
        release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT,
        release_url=RELEASE_URL,
        issue_number=42,
        body=(
            f"# {TAG}\n\n"
            "## Changelog\n\n"
            "- A change ([Issue #1](https://example.test/1))\n\n"
            "## Scope (verified item by item)\n\n"
            "- verified\n"
            "run_id=deadbeef\n\n"
            "## Pre-release gates\n\n"
            "- gate\n"
        ),
    )
    en_page = script.release_docs_page(**kwargs, language="en")
    zh_page = script.release_docs_page(**kwargs, language="zh")
    assert _page_fields(en_page)[0]["title"] == f"{TAG} release (latest)"
    assert f"(release task: Issue #42)" in en_page
    assert _page_fields(zh_page)[0]["title"] == f"{TAG} 发布（最新）"
    # The engine audit blocks (Scope / Pre-release gates / run_id) are
    # dropped from the reader-facing page.
    assert "Scope (verified item by item)" not in en_page
    assert "run_id=deadbeef" not in en_page
    assert "- A change ([Issue #1](https://example.test/1))" in en_page


def test_release_docs_page_renders_without_published_at():
    script = load_script()
    kwargs = dict(
        version=TAG,
        tag_object=TAG_OBJECT,
        release_commit=RELEASE_COMMIT,
        published_at=None,
        release_url=RELEASE_URL,
        issue_number=7,
        body=f"# {TAG}\n\n- no publish time\n",
    )
    for language in ("en", "zh"):
        page = script.release_docs_page(**kwargs, language=language)
        assert RELEASE_URL in page
        assert "- no publish time" in page


def test_release_docs_page_omits_the_release_task_clause_without_an_issue():
    script = load_script()
    en_page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=None,
        body=f"# {TAG}\n\n- change\n", language="en",
    )
    zh_page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=None,
        body=f"# {TAG}\n\n- change\n", language="zh",
    )
    assert "release task" not in en_page
    assert "release task" not in zh_page
    assert f"[{TAG}]({RELEASE_URL})." in en_page
    assert f"[{TAG}]({RELEASE_URL})。" in zh_page


def test_release_docs_page_drops_html_comments_and_the_leading_heading():
    script = load_script()
    page = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=1,
        body=f"# {TAG}\n\n<!-- orbi:run=abc -->\n\n- change\n",
        language="en",
    )
    assert _page_fields(page)[0]["title"] == f"{TAG} release (latest)"
    assert "orbi:run" not in page


def test_release_docs_page_rejects_an_unknown_language():
    script = load_script()
    with pytest.raises(script.ReleaseDocsError, match="not supported"):
        script.release_docs_page(
            version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
            published_at=PUBLISHED_AT, release_url=RELEASE_URL,
            issue_number=1, body="- change\n", language="fr",
        )


# ---------------------------------------------------------------------------
# docs.json navigation and the (latest) marker.
# ---------------------------------------------------------------------------


def _release_groups(
    en_pages: list, zh_pages: list,
) -> dict:
    return {"navigation": {"languages": [
        {"language": "en", "groups": [{"group": "Releases", "pages": en_pages}]},
        {"language": "zh", "groups": [{"group": "发布", "pages": zh_pages}]},
    ]}}


def test_current_latest_release_slug_reads_the_first_release_page():
    script = load_script()
    config = json.dumps({"navigation": {"languages": [
        {"language": "zh", "groups": [
            {"group": "发布", "pages": ["zh/release-v1.0.0"]},
        ]},
        {"language": "en", "groups": [
            {"group": "Releases", "pages": ["release-v1.0.0", "release-v0.9.0"]},
        ]},
    ]}})
    assert script.current_latest_release_slug(config) == "release-v1.0.0"


def test_current_latest_release_slug_fails_fast_on_a_broken_config():
    script = load_script()
    empty_group = json.dumps(_release_groups([], []))
    with pytest.raises(script.ReleaseDocsError, match="has no"):
        script.current_latest_release_slug(empty_group)
    no_group = json.dumps({"navigation": {"languages": [
        {"language": "en", "groups": [{"group": "Other", "pages": ["index"]}]},
    ]}})
    with pytest.raises(script.ReleaseDocsError, match="no English Releases group"):
        script.current_latest_release_slug(no_group)


def test_update_release_navigation_inserts_and_collapses_older_pages():
    script = load_script()
    config = json.dumps(_release_groups(
        ["release-v0.3.0", "release-v0.2.0", "release-v0.1.0",
         {"group": "Earlier releases", "expanded": False,
          "pages": ["release-v0.0.9"]}],
        ["zh/release-v0.3.0", "zh/release-v0.2.0", "zh/release-v0.1.0",
         {"group": "历史版本", "expanded": False,
          "pages": ["zh/release-v0.0.9"]}],
    ))
    updated, changed = script.update_release_navigation(config, "release-v0.4.0")
    assert changed is True
    en_pages = json.loads(updated)["navigation"]["languages"][0]["groups"][0]["pages"]
    zh_pages = json.loads(updated)["navigation"]["languages"][1]["groups"][0]["pages"]
    assert en_pages == [
        "release-v0.4.0", "release-v0.3.0", "release-v0.2.0",
        {"group": "Earlier releases", "expanded": False,
         "pages": ["release-v0.1.0", "release-v0.0.9"]},
    ]
    assert zh_pages == [
        "zh/release-v0.4.0", "zh/release-v0.3.0", "zh/release-v0.2.0",
        {"group": "历史版本", "expanded": False,
         "pages": ["zh/release-v0.1.0", "zh/release-v0.0.9"]},
    ]


def test_update_release_navigation_ignores_unrelated_groups():
    script = load_script()
    config = json.dumps({"navigation": {"languages": [
        {"language": "en", "groups": [
            {"group": "Other", "pages": ["index"]},
            {"group": "Releases", "pages": ["release-v1.0.0"]},
        ]},
        {"language": "zh", "groups": [
            {"group": "发布", "pages": ["zh/release-v1.0.0"]},
        ]},
    ]}})
    updated, changed = script.update_release_navigation(config, "release-v0.9.0")
    assert changed is True
    en_pages = json.loads(updated)["navigation"]["languages"][0]["groups"][1]["pages"]
    assert en_pages[0] == "release-v0.9.0"


def test_update_release_navigation_is_idempotent_for_a_listed_slug():
    script = load_script()
    config = json.dumps(_release_groups(
        ["release-v1.0.0", "release-v0.9.0"],
        ["zh/release-v1.0.0", "zh/release-v0.9.0"],
    ))
    updated, changed = script.update_release_navigation(config, "release-v0.9.0")
    assert updated == config
    assert changed is False


def test_update_release_navigation_fails_fast_without_a_collapsed_subgroup():
    script = load_script()
    config = json.dumps(_release_groups(
        ["release-v0.3.0", "release-v0.2.0", "release-v0.1.0", "release-v0.0.9"],
        ["zh/release-v0.3.0", "zh/release-v0.2.0", "zh/release-v0.1.0",
         "zh/release-v0.0.9"],
    ))
    with pytest.raises(script.ReleaseDocsError, match="no collapsed subgroup"):
        script.update_release_navigation(config, "release-v0.4.0")


def test_update_release_navigation_fails_fast_on_a_single_release_group():
    script = load_script()
    config = json.dumps({"navigation": {"languages": [
        {"language": "en", "groups": [
            {"group": "Releases", "pages": ["release-v1.0.0"]},
        ]},
    ]}})
    with pytest.raises(script.ReleaseDocsError, match="exactly two release groups"):
        script.update_release_navigation(config, "release-v0.9.0")


def _release_page(title: str, extra: str = "") -> str:
    return f'---\ntitle: "{title}"\ndescription: "d"\n---\n\n{extra}\n'


def _write_marker_pages(tmp_path: Path) -> None:
    (tmp_path / "docs" / "zh").mkdir(parents=True)
    (tmp_path / "docs" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release (latest)", "old"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布（最新）", "旧"), encoding="utf-8")
    (tmp_path / "docs" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 release (latest)", "new"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 发布（最新）", "新"), encoding="utf-8")


def test_move_latest_marker_moves_the_marker_off_the_old_page(tmp_path):
    script = load_script()
    _write_marker_pages(tmp_path)
    changed = script.move_latest_marker(
        tmp_path, "release-v0.9.0", "release-v1.0.0", resume=False,
    )
    assert changed == ["docs/release-v0.9.0.mdx", "docs/zh/release-v0.9.0.mdx"]
    old_en = (tmp_path / "docs" / "release-v0.9.0.mdx").read_text()
    old_zh = (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").read_text()
    assert _page_fields(old_en)[0]["title"] == "v0.9.0 release"
    assert _page_fields(old_zh)[0]["title"] == "v0.9.0 发布"
    new_en = (tmp_path / "docs" / "release-v1.0.0.mdx").read_text()
    assert _page_fields(new_en)[0]["title"] == "v1.0.0 release (latest)"


def test_move_latest_marker_fails_fast_when_the_old_page_is_missing(tmp_path):
    script = load_script()
    with pytest.raises(script.ReleaseDocsError, match="is missing"):
        script.move_latest_marker(
            tmp_path, "release-v0.9.0", "release-v1.0.0", resume=False,
        )


def test_move_latest_marker_fails_fast_without_the_marker_on_a_fresh_move(tmp_path):
    script = load_script()
    (tmp_path / "docs" / "zh").mkdir(parents=True)
    (tmp_path / "docs" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布"), encoding="utf-8")
    (tmp_path / "docs" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 release (latest)"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 发布（最新）"), encoding="utf-8")
    with pytest.raises(script.ReleaseDocsError, match="does not carry"):
        script.move_latest_marker(
            tmp_path, "release-v0.9.0", "release-v1.0.0", resume=False,
        )


def test_move_latest_marker_accepts_a_resumed_move(tmp_path):
    script = load_script()
    (tmp_path / "docs" / "zh").mkdir(parents=True)
    (tmp_path / "docs" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布"), encoding="utf-8")
    (tmp_path / "docs" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 release (latest)"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 发布（最新）"), encoding="utf-8")
    assert script.move_latest_marker(
        tmp_path, "release-v0.9.0", "release-v1.0.0", resume=True,
    ) == []


def test_move_latest_marker_rejects_an_unmoved_marker_on_resume(tmp_path):
    script = load_script()
    (tmp_path / "docs" / "zh").mkdir(parents=True)
    (tmp_path / "docs" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布"), encoding="utf-8")
    (tmp_path / "docs" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 release"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v1.0.0.mdx").write_text(
        _release_page("v1.0.0 发布"), encoding="utf-8")
    with pytest.raises(script.ReleaseDocsError, match="does not carry"):
        script.move_latest_marker(
            tmp_path, "release-v0.9.0", "release-v1.0.0", resume=True,
        )


def test_move_latest_marker_rejects_a_resume_without_the_new_page(tmp_path):
    script = load_script()
    (tmp_path / "docs" / "zh").mkdir(parents=True)
    (tmp_path / "docs" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release"), encoding="utf-8")
    (tmp_path / "docs" / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布"), encoding="utf-8")
    with pytest.raises(script.ReleaseDocsError, match="does not carry"):
        script.move_latest_marker(
            tmp_path, "release-v0.9.0", "release-v1.0.0", resume=True,
        )


# ---------------------------------------------------------------------------
# GitHub / git reads.
# ---------------------------------------------------------------------------


def test_gh_release_view_reads_the_published_release(monkeypatch):
    script = load_script()
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Result(stdout=json.dumps(
            {"body": "b", "publishedAt": PUBLISHED_AT, "url": RELEASE_URL},
        ))

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    assert script.gh_release_view(TAG) == {
        "body": "b", "publishedAt": PUBLISHED_AT, "url": RELEASE_URL,
    }
    assert calls[0][:4] == ["gh", "release", "view", TAG]
    assert "--json" in calls[0]


def test_gh_release_view_fails_fast(monkeypatch):
    script = load_script()
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(returncode=1, stderr="not found"),
    )
    with pytest.raises(script.ReleaseDocsError, match="gh release view"):
        script.gh_release_view(TAG)


def test_find_release_issue_returns_the_exact_title(monkeypatch):
    script = load_script()
    issues = json.dumps([
        {"number": 99, "title": f"Release {TAG} notes"},
        {"number": 42, "title": f"Release {TAG}"},
    ])
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(stdout=issues),
    )
    assert script.find_release_issue(TAG) == 42


def test_find_release_issue_returns_none_without_a_match(monkeypatch):
    script = load_script()
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(stdout=json.dumps([
            {"number": 99, "title": f"Release {TAG} notes"},
        ])),
    )
    assert script.find_release_issue(TAG) is None


def test_find_release_issue_fails_fast(monkeypatch):
    script = load_script()
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(returncode=1, stderr="api down"),
    )
    with pytest.raises(script.ReleaseDocsError, match="gh issue list"):
        script.find_release_issue(TAG)


def test_git_output_returns_stdout(monkeypatch, tmp_path):
    script = load_script()
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(stdout="deadbeef\n"),
    )
    assert script.git_output(tmp_path, "rev-parse", "HEAD") == "deadbeef"


def test_git_output_fails_fast(monkeypatch, tmp_path):
    script = load_script()
    monkeypatch.setattr(
        script.subprocess, "run",
        lambda *args, **kwargs: _Result(returncode=128, stderr="bad ref"),
    )
    with pytest.raises(script.ReleaseDocsError, match="git rev-parse"):
        script.git_output(tmp_path, "rev-parse", "refs/tags/absent")


# ---------------------------------------------------------------------------
# generate() end to end.
# ---------------------------------------------------------------------------


def _release_repo(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "zh").mkdir(parents=True)
    (docs / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 release (latest)", "old"), encoding="utf-8")
    (docs / "zh" / "release-v0.9.0.mdx").write_text(
        _release_page("v0.9.0 发布（最新）", "旧"), encoding="utf-8")
    (docs / "docs.json").write_text(json.dumps(_release_groups(
        ["release-v0.9.0"], ["zh/release-v0.9.0"],
    )), encoding="utf-8")
    return tmp_path


def _fake_git(repo_root, *args):
    target = args[-1]
    return TAG_OBJECT if not target.endswith("^{commit}") else RELEASE_COMMIT


def _stub_github(script, monkeypatch):
    monkeypatch.setattr(script, "gh_release_view", lambda tag: {
        "body": f"# {TAG}\n\n## Changelog\n\n- work\n",
        "publishedAt": PUBLISHED_AT,
        "url": RELEASE_URL,
    })
    monkeypatch.setattr(script, "find_release_issue", lambda tag: 42)
    monkeypatch.setattr(script, "git_output", _fake_git)


def test_generate_writes_pages_marker_and_navigation(tmp_path, monkeypatch):
    script = load_script()
    repo = _release_repo(tmp_path)
    _stub_github(script, monkeypatch)

    assert script.generate(TAG, repo) == f"release notes for {TAG} generated"
    en_page = (repo / "docs" / f"release-{TAG}.mdx").read_text(encoding="utf-8")
    zh_page = (
        repo / "docs" / "zh" / f"release-{TAG}.mdx"
    ).read_text(encoding="utf-8")
    assert _page_fields(en_page)[0]["title"] == f"{TAG} release (latest)"
    assert _page_fields(en_page)[0]["description"]
    assert f"(release task: Issue #42)" in en_page
    assert _page_fields(zh_page)[0]["title"] == f"{TAG} 发布（最新）"
    old_en = (repo / "docs" / "release-v0.9.0.mdx").read_text(encoding="utf-8")
    old_zh = (
        repo / "docs" / "zh" / "release-v0.9.0.mdx"
    ).read_text(encoding="utf-8")
    assert _page_fields(old_en)[0]["title"] == "v0.9.0 release"
    assert _page_fields(old_zh)[0]["title"] == "v0.9.0 发布"
    config = json.loads((repo / "docs" / "docs.json").read_text(encoding="utf-8"))
    en_pages = config["navigation"]["languages"][0]["groups"][0]["pages"]
    zh_pages = config["navigation"]["languages"][1]["groups"][0]["pages"]
    assert en_pages[0] == f"release-{TAG}"
    assert zh_pages[0] == f"zh/release-{TAG}"


def test_generate_is_idempotent_when_both_pages_exist(tmp_path, monkeypatch):
    script = load_script()
    repo = _release_repo(tmp_path)
    en_path = repo / "docs" / f"release-{TAG}.mdx"
    zh_path = repo / "docs" / "zh" / f"release-{TAG}.mdx"
    en_path.write_text("# existing en\n", encoding="utf-8")
    zh_path.write_text("# existing zh\n", encoding="utf-8")
    monkeypatch.setattr(
        script, "gh_release_view",
        lambda tag: (_ for _ in ()).throw(AssertionError("must not be read")),
    )

    assert script.generate(TAG, repo) == (
        f"pages for {TAG} already exist — nothing to do"
    )
    assert en_path.read_text(encoding="utf-8") == "# existing en\n"
    assert zh_path.read_text(encoding="utf-8") == "# existing zh\n"


def test_generate_keeps_an_identical_existing_page(tmp_path, monkeypatch):
    script = load_script()
    repo = _release_repo(tmp_path)
    _stub_github(script, monkeypatch)
    body = f"# {TAG}\n\n## Changelog\n\n- work\n"
    en_content = script.release_docs_page(
        version=TAG, tag_object=TAG_OBJECT, release_commit=RELEASE_COMMIT,
        published_at=PUBLISHED_AT, release_url=RELEASE_URL, issue_number=42,
        body=body, language="en", latest=True,
    )
    (repo / "docs" / f"release-{TAG}.mdx").write_text(
        en_content, encoding="utf-8",
    )

    assert script.generate(TAG, repo) == f"release notes for {TAG} generated"
    assert (repo / "docs" / f"release-{TAG}.mdx").read_text(
        encoding="utf-8",
    ) == en_content
    assert (repo / "docs" / "zh" / f"release-{TAG}.mdx").is_file()


def test_generate_skips_the_marker_move_when_the_tag_is_the_nav_head(
    tmp_path, monkeypatch,
):
    """A malformed intermediate state: the navigation already lists the new
    tag at its head but its page is missing. The move must be skipped (the
    nav head IS the new tag) and the listed navigation left untouched."""
    script = load_script()
    repo = _release_repo(tmp_path)
    (repo / "docs" / "docs.json").write_text(json.dumps(_release_groups(
        [f"release-{TAG}", "release-v0.9.0"],
        [f"zh/release-{TAG}", "zh/release-v0.9.0"],
    )), encoding="utf-8")
    _stub_github(script, monkeypatch)

    assert script.generate(TAG, repo) == f"release notes for {TAG} generated"
    old_en = (repo / "docs" / "release-v0.9.0.mdx").read_text(encoding="utf-8")
    assert _page_fields(old_en)[0]["title"] == "v0.9.0 release (latest)"


def test_generate_fails_fast_without_a_mintlify_docs_site(tmp_path):
    script = load_script()
    with pytest.raises(script.ReleaseDocsError, match="no Mintlify docs site"):
        script.generate(TAG, tmp_path)


def test_generate_fails_fast_on_an_empty_release_body(tmp_path, monkeypatch):
    script = load_script()
    repo = _release_repo(tmp_path)
    monkeypatch.setattr(script, "gh_release_view", lambda tag: {
        "body": "  ", "publishedAt": PUBLISHED_AT, "url": RELEASE_URL,
    })
    with pytest.raises(script.ReleaseDocsError, match="body is empty"):
        script.generate(TAG, repo)


def test_generate_refuses_to_overwrite_a_different_page(tmp_path, monkeypatch):
    script = load_script()
    repo = _release_repo(tmp_path)
    (repo / "docs" / f"release-{TAG}.mdx").write_text(
        "# a different page\n", encoding="utf-8",
    )
    _stub_github(script, monkeypatch)
    with pytest.raises(script.ReleaseDocsError, match="never overwritten"):
        script.generate(TAG, repo)


# ---------------------------------------------------------------------------
# main().
# ---------------------------------------------------------------------------


def test_main_prints_the_result(monkeypatch, capsys, tmp_path):
    script = load_script()
    monkeypatch.setattr(script, "generate", lambda tag, repo: f"ok {tag}")
    assert script.main([TAG, "--repo", str(tmp_path)]) == 0
    assert f"release_docs: ok {TAG}" in capsys.readouterr().out


def test_main_reports_a_failure(capsys):
    script = load_script()
    assert script.main([TAG, "--repo", "/nonexistent/orbi-checkout"]) == 1
    assert "release_docs: " in capsys.readouterr().err


def test_script_entrypoint_exits_zero_when_pages_already_exist(
    tmp_path, monkeypatch, capsys,
):
    """The `python3 tools/release_docs.py <tag>` entrypoint the workflow
    runs: run it as `__main__` against a checkout whose pages already exist
    (idempotent) and assert the process status is 0."""
    repo = _release_repo(tmp_path)
    (repo / "docs" / f"release-{TAG}.mdx").write_text(
        "# en\n", encoding="utf-8")
    (repo / "docs" / "zh" / f"release-{TAG}.mdx").write_text(
        "# zh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), TAG, "--repo", str(repo)])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert excinfo.value.code == 0
    assert f"release_docs: pages for {TAG} already exist" in (
        capsys.readouterr().out
    )


# ---------------------------------------------------------------------------
# Workflow structure (the actionlint-equivalent check).
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    assert WORKFLOW_FILE.is_file(), f"missing workflow: {WORKFLOW_FILE}"
    workflow = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "workflow file is not a YAML mapping"
    return workflow


def _on_section(workflow: dict) -> dict:
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict), "workflow has no `on:` trigger section"
    return section


def _steps() -> list[dict]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict) and "release-docs" in jobs, (
        f"missing release-docs job: {sorted(jobs or {})}"
    )
    steps = jobs["release-docs"].get("steps")
    assert isinstance(steps, list) and steps, "release-docs job has no steps"
    return steps


def _run_commands() -> str:
    return "\n".join(
        str(step.get("run", "")) for step in _steps() if step.get("run")
    )


def test_release_docs_workflow_triggers_on_published_release_and_dispatch():
    section = _on_section(_workflow())
    assert section.get("release", {}).get("types") == ["published"]
    dispatch = section.get("workflow_dispatch")
    assert isinstance(dispatch, dict), "workflow_dispatch trigger missing"
    tag = (dispatch.get("inputs") or {}).get("tag") or {}
    assert tag.get("required") is True, (
        "the manual backfill/re-run input names the tag"
    )


def test_release_docs_workflow_has_write_permission_and_tags_checkout():
    workflow = _workflow()
    assert (workflow.get("permissions") or {}).get("contents") == "write"
    checkouts = [
        step for step in _steps()
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1, checkouts
    options = checkouts[0].get("with", {})
    assert options.get("ref") == "main"
    assert options.get("fetch-tags") is True, "the tag object must be present"


def test_release_docs_workflow_runs_the_script_on_the_release_tag():
    commands = _run_commands()
    assert "python3 tools/release_docs.py" in commands
    assert "github.event.release.tag_name || inputs.tag" in _workflow_text()


def test_release_docs_workflow_commits_and_pushes_to_main_with_a_bounded_retry():
    commands = _run_commands()
    assert 'git commit -m "docs: release notes for $TAG"' in commands
    assert "git push origin HEAD:main" in commands
    assert "for attempt in 1 2 3;" in commands, "at most 3 push attempts"
    assert "git pull --rebase origin main" in commands
    assert "git diff --cached --quiet" in commands, (
        "an idempotent run commits nothing"
    )


def _workflow_text() -> str:
    return WORKFLOW_FILE.read_text(encoding="utf-8")
