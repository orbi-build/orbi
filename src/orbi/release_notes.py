"""Release-note (Changelog) derivation for a tagged release (Issue #1492).

The release notes describe what the release TAG contains, never the
Milestone Issue list. `release_range_prs` maps the tagged commit range
`prev_tag..release_commit` to the pull requests merged into the release
branch; `release_changelog_scope` maps those PRs to their closing Issues;
`build_release_changelog` renders the deterministic notes. The Milestone
keeps its role for gating only (see `orbi.release`).

The git/GitHub data access goes through the `orbi.journal` seam, so the
module is unit-testable with a fake `run_command`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from orbi.journal import event, run_command


RELEASE_CHANGELOG_CATEGORIES = (
    "Features", "Reliability and recovery", "Deployment and operations",
    "Observability", "Documentation", "Bug fixes",
)


def release_changelog_category(item: dict) -> str:
    """Classify one live scoped Issue into a stable reader-facing group."""
    labels = item.get("labels")
    label_names = {
        label.get("name", "").lower() for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    } if isinstance(labels, list) else set()
    title = item.get("title")
    text = title.lower() if isinstance(title, str) else ""
    if "documentation" in label_names or any(
        term in text for term in ("documentation", "docs", "readme", "文档")
    ):
        return "Documentation"
    if any(term in text for term in (
        "deploy", "deployment", "systemd", "install", " cli", "ssh",
        "service", "timer", "packaging", "setup",
    )):
        return "Deployment and operations"
    if any(term in text for term in (
        "recovery", "recover", "resume", "timeout", "concurren", "reliab",
        "stale", "dead", "hang", "lock",
    )):
        return "Reliability and recovery"
    if any(term in text for term in (
        "observability", "prometheus", "grafana", "dashboard", "metrics",
        "exporter", "journal", "progress",
    )):
        return "Observability"
    if "bug" in label_names:
        return "Bug fixes"
    return "Features"


def previous_release_tag(repo_dir: Path, release_commit: str) -> str | None:
    """Return the newest `v*` tag before the release commit, or None.

    `git describe --tags --abbrev=0 --first-parent --match 'v*'
    <release_commit>^` names the previous version tag on the release
    branch's FIRST-PARENT line: a `v*` tag that only lives on a side
    branch merged in later is never mistaken for the previous release,
    and the `^` keeps the tag created at `release_commit` itself out of
    its own range. No tag reachable that way is the first release: the
    range has no lower bound. A real git failure other than "no tag" is
    not swallowed: it propagates, because a range that cannot be
    established must not silently become the full history.
    """
    try:
        raw = run_command(
            ["git", "describe", "--tags", "--abbrev=0", "--first-parent",
             "--match", "v*", f"{release_commit}^"],
            cwd=repo_dir, failure_log_level=logging.DEBUG,
        )
    except subprocess.CalledProcessError:
        # `git describe` exits non-zero when no matching tag is reachable
        # (and when <commit>^ does not exist on the first release). Both
        # mean exactly one thing here: no previous release tag.
        return None
    return raw.strip() or None


def release_range_prs(repo_dir: Path, repo: str, release_commit: str,
                      base_branch: str | None = None) -> set[int]:
    """Return the PR numbers contained in the tagged commit range.

    The source of truth for the release notes is what the tag actually
    contains, never the Milestone Issue list (Issue #1492). The range is
    `<previous v* tag>..<release_commit>` (or the full first-parent
    history when this is the first release). Each commit is resolved to
    its associated pull requests through `GET
    repos/{repo}/commits/{sha}/pulls`; a PR counts only when it is
    actually merged (`merged_at` set) into the release branch
    (`base.ref` equal to `base_branch`, defaulting to the checkout's
    current branch). A commit with no PR — a direct push, `chore:
    prepare release`, `chore: point active_milestone` — is skipped, so
    housekeeping never reaches the notes.

    A real git/`gh` failure propagates: a range that cannot be
    established is a failed release, never a guessed one.
    """
    previous = previous_release_tag(repo_dir, release_commit)
    revision_range = (
        f"{previous}..{release_commit}" if previous is not None
        else release_commit
    )
    raw = run_command(
        ["git", "rev-list", "--first-parent", revision_range],
        cwd=repo_dir,
    )
    if base_branch is None:
        base_branch = run_command(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir,
        ).strip()
    pull_requests: set[int] = set()
    for sha in raw.split():
        payload = json.loads(run_command(
            ["gh", "api", f"repos/{repo}/commits/{sha}/pulls"],
            log_command=["gh", "api", f"repos/{repo}/commits/{sha}/pulls"],
        ))
        if not isinstance(payload, list):
            raise ValueError(
                f"release range commit {sha} has malformed pull-request "
                "evidence"
            )
        for pull_request in payload:
            if not isinstance(pull_request, dict):
                continue
            base = pull_request.get("base")
            if not pull_request.get("merged_at"):
                continue
            if not isinstance(base, dict) or base.get("ref") != base_branch:
                continue
            number = pull_request.get("number")
            if not isinstance(number, int):
                raise ValueError(
                    f"release range commit {sha} has malformed pull-request "
                    "number evidence"
                )
            pull_requests.add(number)
    return pull_requests


RELEASE_PR_CLOSING_ISSUES_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      __typename
      ... on PullRequest {
        number
        closingIssuesReferences(first: 100) { nodes { number } }
      }
    }
  }
}"""


def release_changelog_scope(repo: str, range_prs: set[int], *,
                            release_issue: int | None = None) -> list[int]:
    """Resolve the range PRs to the Changelog's item numbers.

    Each range PR's closing Issues become Changelog items (the Issues
    carry the labels/title the categories are rendered from); a PR with
    no closing Issue is listed by its own PR number. The release Issue
    itself is never a Changelog item (Issue #1492): it drives the
    release, it is not released work. A PR whose only reference is that
    excluded release Issue falls back to its PR number so no shipped
    work is lost. Malformed evidence fails fast.
    """
    owner, _, name = repo.partition("/")
    scope: set[int] = set()
    for pr_number in sorted(range_prs):
        raw = run_command([
            "gh", "api", "graphql",
            "-f", f"query={RELEASE_PR_CLOSING_ISSUES_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}",
            "-F", f"number={pr_number}",
        ], log_command=["gh", "api", "graphql", f"pull={pr_number}"])
        node = ((json.loads(raw).get("data") or {}).get(
            "repository", {}) or {}).get("issueOrPullRequest") or {}
        references = (node.get("closingIssuesReferences") or {}).get("nodes")
        if not references:
            scope.add(pr_number)
            continue
        contributed = False
        for reference in references:
            number = (reference.get("number")
                      if isinstance(reference, dict) else None)
            if not isinstance(number, int):
                raise ValueError(
                    f"release changelog PR #{pr_number} has malformed "
                    "closing-Issue evidence"
                )
            if number == release_issue:
                continue
            scope.add(number)
            contributed = True
        if not contributed:
            scope.add(pr_number)
    return sorted(scope)


RELEASE_ISSUE_EVIDENCE_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue: issueOrPullRequest(number: $number) {
      __typename
      ... on Issue {
        number title body url stateReason
        labels(first: 100) { nodes { name } }
        closedByPullRequestsReferences(first: 100) {
          nodes { number url author { login avatarUrl } }
        }
      }
      ... on PullRequest { number title body url labels(first: 100) { nodes { name } } }
    }
  }
}"""


def build_release_changelog(repo: str, scope: list[int],
                           range_prs: set[int]) -> str:
    """Render deterministic readable notes from live scoped Issue evidence.

    Each scope number is resolved with one `gh api graphql` round trip
    (`issueOrPullRequest`, the same access path `gh issue view` uses):
    the Issue fields, labels and closing-PR
    references — now including each PR's author login and avatar so the
    Contributors section costs zero extra API calls.  A title is the
    concise change description; when it is absent, the first non-empty
    body line is usable summary evidence.  A NOT_PLANNED-closed Issue is
    not released work — it is excluded from the Changelog
    (the Scope evidence annotates the exclusion).  A closing PR's link
    is written only when the closing PR is contained in the release's
    tagged commit range (`range_prs`, Issue #1492): a PR that merged
    after the tag, or whose merge is not part of the tag, never appears
    in the release notes, and its author is not a contributor.  The
    `## Contributors` section lists every in-range closing-PR author
    exactly once (deduped by login, sorted by login)
    as a linked avatar; a null or malformed author is display evidence,
    not a release judge — it is skipped with a log line and never fails
    the release, and with no contributors at all no section is written.
    Missing or malformed evidence is an unsafe release input and fails
    before a tag or Release is created. An empty scope (Issue #1384: a
    milestone with no deliveries) is not an error — the notes carry the
    one-line empty-scope sentence instead of a Changelog list.
    """
    if not scope:
        return "No deliveries are linked to this milestone."
    owner, _, name = repo.partition("/")
    grouped: dict[str, list[tuple[int, str]]] = {
        category: [] for category in RELEASE_CHANGELOG_CATEGORIES
    }
    contributors: dict[str, str] = {}
    for number in scope:
        raw = run_command([
            "gh", "api", "graphql",
            "-f", f"query={RELEASE_ISSUE_EVIDENCE_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}",
            "-F", f"number={number}",
        ], log_command=["gh", "api", "graphql", f"issue={number}"])
        issue = (json.loads(raw).get("data") or {}).get(
            "repository", {},
        ).get("issue") or {}
        # Reshape the GraphQL payload into the field keys the strict
        # evidence validation below has always asserted; the closedBy
        # nodes (number/url/author) pass through raw.  The PullRequest
        # branch carries no closedBy field (schema: Issue-only) and no
        # stateReason — the same shape `gh issue view` produced for a
        # PR number.
        references = issue.get("closedByPullRequestsReferences") or {}
        item = {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "body": issue.get("body"),
            "url": issue.get("url"),
            "labels": [
                {"name": label.get("name")}
                for label in (issue.get("labels") or {}).get("nodes") or []
                if isinstance(label, dict)
            ],
            "closedByPullRequestsReferences": (
                references.get("nodes") or []
                if isinstance(references, dict) else None
            ),
        }
        if issue.get("__typename") == "Issue":
            item["stateReason"] = issue.get("stateReason")
        if item.get("stateReason") == "NOT_PLANNED":
            event(
                "release_changelog_issue_excluded", number=number,
                reason="NOT_PLANNED",
            )
            continue
        issue_url = item.get("url")
        issue_path = f"https://github.com/{repo}/issues/{number}"
        pull_path = f"https://github.com/{repo}/pull/{number}"
        if item.get("number") != number or issue_url not in (issue_path, pull_path):
            raise ValueError(
                f"release changelog Issue #{number} has malformed Issue evidence"
            )
        title = item.get("title")
        body = item.get("body")
        summary = title.strip() if isinstance(title, str) else ""
        if not summary and isinstance(body, str):
            summary = next((line.strip() for line in body.splitlines()
                            if line.strip()), "")
        if not summary or re.fullmatch(r"Issue #\d+ closed", summary, re.I):
            raise ValueError(
                f"release changelog Issue #{number} has no usable title/summary evidence"
            )
        source_kind = "PR" if issue_url == pull_path else "Issue"
        links = [f"[{source_kind} #{number}]({issue_url})"]
        pull_requests = item.get("closedByPullRequestsReferences")
        if not isinstance(pull_requests, list):
            raise ValueError(
                f"release changelog Issue #{number} has malformed PR evidence"
            )
        for pull_request in sorted(pull_requests, key=lambda pr: pr.get("number", 0)
                                   if isinstance(pr, dict) else 0):
            pr_number = pull_request.get("number") if isinstance(pull_request, dict) else None
            pr_url = pull_request.get("url") if isinstance(pull_request, dict) else None
            if (not isinstance(pr_number, int) or pr_number < 1 or
                    pr_url != f"https://github.com/{repo}/pull/{pr_number}"):
                raise ValueError(
                    f"release changelog Issue #{number} has malformed PR evidence"
                )
            # A PR outside the tagged commit range is not released
            # content — its link never enters the release notes
            # (Issue #1492).
            if pr_number not in range_prs:
                event(
                    "release_changelog_pr_link_dropped", issue=number,
                    pr=pr_number, state="out_of_range",
                )
                continue
            links.append(f"[PR #{pr_number}]({pr_url})")
            author = (pull_request.get("author")
                      if isinstance(pull_request, dict) else None)
            login = author.get("login") if isinstance(author, dict) else None
            avatar = (author.get("avatarUrl")
                      if isinstance(author, dict) else None)
            if isinstance(login, str) and login and \
                    isinstance(avatar, str) and avatar:
                contributors.setdefault(login, avatar)
            else:
                # A ghosted/malformed author is display evidence, never a
                # release judge: skip the contributor, keep the release.
                event(
                    "release_changelog_contributor_skipped", issue=number,
                    pr=pr_number,
                )
        grouped[release_changelog_category(item)].append(
            (number, f"- {summary} ({'; '.join(links)})")
        )
    sections = ["## Changelog"]
    for category in RELEASE_CHANGELOG_CATEGORIES:
        entries = grouped[category]
        if entries:
            sections.extend(["", f"### {category}", "",
                             *(entry for _, entry in sorted(entries))])
    if contributors:
        avatar_rows = []
        for login in sorted(contributors):
            avatar = contributors[login]
            sized = avatar + ("&s=48" if "?" in avatar else "?s=48")
            avatar_rows.append(
                f'<a href="https://github.com/{login}">'
                f'<img src="{sized}" width="32" height="32" alt="{login}" /></a>'
            )
        # One source line for all avatars (Issue #835): GitHub renders
        # each raw-HTML line as its own markdown block, so separated
        # lines stacked the avatars vertically instead of inline.
        sections.extend(["", "## Contributors", "", " ".join(avatar_rows)])
    return "\n".join(sections)
