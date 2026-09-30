#!/usr/bin/env python3
"""Generate the docs-site release pages for one published tag (Issue #1482).

This repository's own Mintlify release pages (`docs/release-<tag>.mdx`,
`docs/zh/release-<tag>.mdx`, the `Releases`/`发布` navigation entries and the
`(latest)` marker) used to be written by the engine's release state machine;
Issue #1483 removed that engine step. The pages
are this repository's own docs-site business, so they live here in a
standalone script that the `.github/workflows/release-docs.yml` workflow runs
on `release: published` and on manual dispatch.

There is NO `orbi` import: the tool is the repository's own docs tooling and
must keep working while the engine code changes. The rendering was copied from
the engine's former release-docs page builder with the same output format; the
one addition is that the
"release task" clause is omitted when no matching `ai-release` Issue exists.

Input: one tag. The script reads the published GitHub Release
(`gh release view <tag> --json body,publishedAt,url`), the annotated tag object
and its commit (git), and the release task Issue (the `ai-release` Issue titled
exactly `Release <tag>`).

Idempotent and never destructive: when both pages for the tag already exist it
changes nothing and exits 0. An existing page with different content is never
overwritten.

Usage:  python3 tools/release_docs.py <tag> [--repo <checkout root>]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

RELEASE_DOCS_LATEST_MARKER_EN = " (latest)"
RELEASE_DOCS_LATEST_MARKER_ZH = "（最新）"

# The release-machine audit blocks the GitHub Release body carries for
# the release state machine's own evidence trail (#204). They stay on
# the GitHub Release; the docs site is for readers, so the docs page
# drops them (#910) — the changelog and the rest of the body stay.
RELEASE_BODY_DROP_SECTION_HEADINGS = (
    "## Scope (verified item by item)",
    "## Pre-release gates",
)


class ReleaseDocsError(RuntimeError):
    """A release-docs precondition failed — fail fast, never guess."""


def strip_release_audit_sections(notes: str) -> str:
    """Drop the release-machine audit blocks from a release body: the
    `## Scope (verified item by item)` and `## Pre-release gates`
    sections and the standalone `run_id=` lines. Everything else — the
    changelog, the meta bullets, the `## Tests` section — stays."""
    dropping = False
    kept: list[str] = []
    for line in notes.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            dropping = stripped in RELEASE_BODY_DROP_SECTION_HEADINGS
            if dropping:
                continue
        if dropping or stripped.startswith("run_id="):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def release_docs_page(*, version: str, tag_object: str,
                      release_commit: str, published_at: str | None,
                      release_url: str, issue_number: int | None,
                      body: str, language: str,
                      latest: bool = True) -> str:
    """Build one docs-site Release notes page for a published release.

    The rendering was copied from the engine's former release-docs page
    builder; `issue_number=None` omits the "release task" clause
    (a release whose task Issue is not found).
    """
    notes = body.strip()
    lines = notes.splitlines()
    if lines and lines[0].strip() == f"# {version}":
        lines = lines[1:]
    lines = [
        line for line in lines
        if not line.strip().startswith("<!--")
    ]
    notes = strip_release_audit_sections("\n".join(lines))
    task_en = (
        f" (release task: Issue #{issue_number})"
        if issue_number is not None else ""
    )
    task_zh = (
        f"（release task：Issue #{issue_number}）"
        if issue_number is not None else ""
    )
    if language == "en":
        title = f"# {version} release" + (" (latest)" if latest else "")
        intro = (
            f"`{version}` release notes for the GitHub Release "
            f"[{version}]({release_url}){task_en}."
            if published_at is None else
            f"`{version}` was published {published_at} as the GitHub "
            f"Release [{version}]({release_url}){task_en}."
        )
        heading = "## Tag state (verified against origin)"
        table = (
            "| Ref | Object | Points at |\n"
            "|---|---|---|\n"
            f"| `{version}` | annotated tag `{tag_object}` "
            f"| commit `{release_commit}` |"
        )
    elif language == "zh":
        title = f"# {version} 发布" + ("（最新）" if latest else "")
        intro = (
            f"`{version}` 的 GitHub Release 发布说明："
            f"[{version}]({release_url}){task_zh}。"
            if published_at is None else
            f"`{version}` 于 {published_at} 发布为 GitHub Release "
            f"[{version}]({release_url}){task_zh}。"
        )
        heading = "## Tag 状态（对 origin 验证）"
        table = (
            "| Ref | 对象 | 指向 |\n"
            "|---|---|---|\n"
            f"| `{version}` | 注解 tag `{tag_object}` "
            f"| 提交 `{release_commit}` |"
        )
    else:
        raise ReleaseDocsError(
            f"release docs page language {language!r} is not supported "
            "(use 'en' or 'zh')"
        )
    return "\n".join([
        title, "",
        intro, "",
        heading, "",
        table, "",
        "## Release notes", "",
        notes, "",
    ])


def current_latest_release_slug(config_text: str) -> str:
    """The first page of the English `Releases` group — the current
    latest release (the groups are latest-first)."""
    config = json.loads(config_text)
    for lang in config["navigation"]["languages"]:
        if lang.get("language") != "en":
            continue
        for group in lang["groups"]:
            if group.get("group") == "Releases":
                pages = group["pages"]
                if not pages:
                    raise ReleaseDocsError(
                        "release docs sync: the Releases group has no "
                        "pages — cannot determine the current latest "
                        "release"
                    )
                return str(pages[0])
    raise ReleaseDocsError(
        "release docs sync: docs.json has no English Releases group — "
        "cannot determine the current latest release"
    )


def update_release_navigation(config_text: str, slug: str) -> tuple[str, bool]:
    """Insert a release and keep only the three newest pages visible.

    The `Releases` (en) and `发布` (zh) groups list releases latest-first.
    Older visible pages move to the head of the existing collapsed subgroup,
    preserving their order. A slug already listed in either group's visible
    pages or subgroup leaves the config untouched (idempotent). Exactly one
    group updated means a broken config — fail fast, never guess.
    """
    config = json.loads(config_text)
    updated = 0
    for lang in config["navigation"]["languages"]:
        for group in lang["groups"]:
            if group.get("group") not in ("Releases", "发布"):
                continue
            pages = group["pages"]
            entry = f"zh/{slug}" if group["group"] == "发布" else slug
            subgroup = next(
                (page for page in pages
                 if isinstance(page, dict) and not page.get("expanded", True)),
                None,
            )
            listed = entry in pages or (
                subgroup is not None and entry in subgroup.get("pages", [])
            )
            if listed:
                continue
            pages.insert(0, entry)
            if len([page for page in pages if isinstance(page, str)]) > 3:
                if subgroup is None:
                    raise ReleaseDocsError(
                        "release docs sync: release navigation has more "
                        "than three visible pages but no collapsed subgroup"
                    )
                visible = [page for page in pages if isinstance(page, str)]
                subgroup["pages"] = visible[3:] + subgroup.get("pages", [])
                pages[:] = visible[:3] + [subgroup]
            updated += 1
    if updated == 0:
        return config_text, False
    if updated != 2:
        raise ReleaseDocsError(
            f"release docs sync: expected exactly two release groups "
            f"(Releases + 发布) but updated {updated} — the docs.json "
            "navigation is not the expected Mintlify i18n layout"
        )
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n", True


def move_latest_marker(repo_root: Path, old_slug: str,
                       new_slug: str, *, resume: bool) -> list[str]:
    """Move the `(latest)` title marker off the previous latest page.

    Only the newest release page may carry the marker:
    ` (latest)` (en) / `（最新）` (zh) is stripped from the previous
    latest page's H1. When the old page already lacks the marker the
    move is only accepted on a resume (`resume=True`: the new page
    already carries the marker — a partial step of an earlier attempt
    already moved it); otherwise the invariant is broken and the step
    fails fast — a broken state is never silently repaired. Returns the
    changed relative paths.
    """
    changed: list[str] = []
    for directory, marker in (("docs", RELEASE_DOCS_LATEST_MARKER_EN),
                              ("docs/zh", RELEASE_DOCS_LATEST_MARKER_ZH)):
        path = repo_root / directory / f"{old_slug}.mdx"
        if not path.is_file():
            raise ReleaseDocsError(
                f"release docs sync: previous latest page {path} is "
                "missing — cannot move the (latest) marker"
            )
        text = path.read_text(encoding="utf-8")
        first_line, _, rest = text.partition("\n")
        if marker in first_line:
            path.write_text(
                first_line.replace(marker, "", 1) + "\n" + rest,
                encoding="utf-8",
            )
            changed.append(f"{directory}/{old_slug}.mdx")
            continue
        if resume:
            new_path = repo_root / directory / f"{new_slug}.mdx"
            new_first = (
                new_path.read_text(encoding="utf-8").splitlines()[0]
                if new_path.is_file() else ""
            )
            if marker in new_first:
                continue
        raise ReleaseDocsError(
            f"release docs sync: {path} does not carry the (latest) "
            "marker in its title and the move did not happen yet — "
            "the latest-marker invariant is broken, refusing to guess"
        )
    return changed


def gh_release_view(tag: str) -> dict:
    """The published GitHub Release (body, publishedAt, url)."""
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "body,publishedAt,url"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ReleaseDocsError(
            f"gh release view {tag} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def find_release_issue(tag: str) -> int | None:
    """The release task Issue number, or None when there is no
    `ai-release` Issue titled exactly `Release <tag>`."""
    result = subprocess.run(
        ["gh", "issue", "list", "--label", "ai-release", "--state", "all",
         "--search", f"Release {tag} in:title",
         "--json", "number,title", "--limit", "100"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ReleaseDocsError(
            f"gh issue list for {tag} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    expected = f"Release {tag}"
    for issue in json.loads(result.stdout):
        if issue.get("title") == expected:
            return int(issue["number"])
    return None


def git_output(repo_root: Path, *args: str) -> str:
    """One fail-fast git read in the checkout."""
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ReleaseDocsError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def generate(tag: str, repo_root: Path) -> str:
    """Generate (or verify) the EN/ZH release pages for one published tag.

    Reads the published Release, the annotated tag object/commit and the
    release task Issue, then writes the pages, moves the `(latest)` marker and
    inserts the navigation entries. Idempotent: when both pages already exist
    nothing is read, written or overwritten and the call returns.
    """
    docs_config = repo_root / "docs" / "docs.json"
    if not docs_config.is_file():
        raise ReleaseDocsError(
            f"no Mintlify docs site at {docs_config} — nothing to generate"
        )
    en_path = repo_root / "docs" / f"release-{tag}.mdx"
    zh_path = repo_root / "docs" / "zh" / f"release-{tag}.mdx"
    if en_path.is_file() and zh_path.is_file():
        return f"pages for {tag} already exist — nothing to do"
    release = gh_release_view(tag)
    body = release.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ReleaseDocsError(
            f"release {tag}: the GitHub Release body is empty — the "
            "docs page would be fabricated, refusing"
        )
    issue_number = find_release_issue(tag)
    tag_object = git_output(repo_root, "rev-parse", f"refs/tags/{tag}")
    release_commit = git_output(
        repo_root, "rev-parse", f"refs/tags/{tag}^{{commit}}",
    )
    new_slug = f"release-{tag}"
    en_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=release["publishedAt"], release_url=release["url"],
        issue_number=issue_number, body=body, language="en", latest=True,
    )
    zh_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=release["publishedAt"], release_url=release["url"],
        issue_number=issue_number, body=body, language="zh", latest=True,
    )
    new_page_preexisting = (
        en_path.is_file()
        and en_path.read_text(encoding="utf-8") == en_content
    )
    for path, content in ((en_path, en_content), (zh_path, zh_content)):
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise ReleaseDocsError(
                    f"release {tag}: {path} already exists with different "
                    "content — an existing release page is never overwritten"
                )
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config_text = docs_config.read_text(encoding="utf-8")
    old_slug = current_latest_release_slug(config_text)
    if old_slug != new_slug:
        move_latest_marker(
            repo_root, old_slug, new_slug, resume=new_page_preexisting,
        )
    new_config_text, nav_changed = update_release_navigation(
        config_text, new_slug,
    )
    if nav_changed:
        docs_config.write_text(new_config_text, encoding="utf-8")
    return f"release notes for {tag} generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tag", help="the published v* release tag, e.g. v0.5.57",
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(),
        help="the checkout root (default: the current directory)",
    )
    args = parser.parse_args(argv)
    try:
        message = generate(args.tag, args.repo)
    except ReleaseDocsError as exc:
        print(f"release_docs: {exc}", file=sys.stderr)
        return 1
    print(f"release_docs: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
