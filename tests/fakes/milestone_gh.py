"""Stateful ``gh`` for the in-ticket ``/milestone`` command (Issue #1290).

The command's three steps emit a short, deterministic ``gh`` argv sequence.
This fake answers that sequence and keeps the state the steps change — the
Milestone list, the release tickets of one Milestone and the repository
policy blob — so a test runs the SAME command twice and observes that the
second run creates nothing (the idempotency contract).

It lives in ``tests/fakes/`` because it decodes ``gh`` argv by prefix: those
prefixes are this fake's dispatch table, not a test expectation, and the
patch ratchet (Issue #789) keeps new command-shape assertions out of the
test corpus while still letting the corpus use a shared fake.

Unsupported argv fails loudly with ``AssertionError`` naming the command,
the same fail-fast contract as a mistyped ``gh`` invocation.
"""
from __future__ import annotations

import base64
import json

REPO = "owner/repo"
_MILESTONES_PATH = f"repos/{REPO}/milestones?state=all&per_page=100"
_POLICY_PATH = f"repos/{REPO}/contents/.github/orbi.toml"
_TEMPLATE_PATH = f"repos/{REPO}/contents/.github/release-ticket-template.md"


class FakeMilestoneGh:
    """One repository's Milestone, release-ticket and policy state."""
    def __init__(self, *, policy_text: str | None = None,
                 template: str | None = None,
                 current: str = "v0.5.40",
                 fail_policy_put: bool = False,
                 tree: tuple[str, ...] = ("pyproject.toml",)):
        self.milestones = [{"title": current, "state": "closed"}]
        self.release_issues: list[dict] = []
        # Decision notices (`orbi-milestone-advance` fingerprints, Issue
        # #856): a `gh issue list` search on the body returns these, a
        # search on the release label returns `release_issues`.
        self.notices: list[dict] = []
        # Every `gh issue edit --add-label` (the idle-path arm).
        self.armed: list[tuple[int, str]] = []
        self.policy_text = policy_text
        self.template = template
        self.tree = list(tree)
        self.comments: list[dict] = []
        self.commands: list[list[str]] = []
        # Each successful contents-API PUT of the policy, decoded: the
        # observable "the advance landed where the engine reads it back".
        self.policy_puts: list[str] = []
        self.fail_policy_put = fail_policy_put

    def run(self, command: list[str], **kwargs) -> str:
        self.commands.append(command)
        if command[:3] == ["gh", "api", _MILESTONES_PATH]:
            return json.dumps([list(self.milestones)])
        if command[:4] == ["gh", "api", "--method", "POST"]:
            self.milestones.append({
                "title": command[command.index("-f") + 1][len("title="):],
                "state": "open",
            })
            return "{}"
        if command[:4] == ["gh", "api", "--method", "PUT"]:
            content = next(item for item in command if item.startswith("content="))
            if self.fail_policy_put:
                raise RuntimeError("gh unavailable")
            self.policy_text = base64.b64decode(
                content[len("content="):]
            ).decode()
            self.policy_puts.append(self.policy_text)
            return "{}"
        if command[:3] == ["gh", "issue", "list"]:
            if "in:body" in _search_of(command):
                return json.dumps(list(self.notices))
            return json.dumps(list(self.release_issues))
        if command[:3] == ["gh", "issue", "comment"]:
            self.comments.append({
                "number": int(command[3]),
                "body": command[command.index("--body") + 1],
            })
            return "{}"
        if command[:3] == ["gh", "issue", "edit"]:
            self.armed.append((
                int(command[3]), command[command.index("--add-label") + 1],
            ))
            return "{}"
        if command[:3] == ["gh", "issue", "close"]:
            number = int(command[3])
            self.notices = [
                notice for notice in self.notices
                if notice["number"] != number
            ]
            self.release_issues = [
                issue for issue in self.release_issues
                if issue.get("number") != number
            ]
            self.comments.append({
                "number": number,
                "body": command[command.index("--comment") + 1],
            })
            return "{}"
        if command[:2] == ["gh", "api"] and "/git/trees/" in command[2]:
            return json.dumps({
                "tree": [{"path": path} for path in self.tree],
            })
        if command[:2] == ["gh", "api"] and "/contents/" in command[2]:
            if command[2].startswith(_TEMPLATE_PATH):
                if self.template is None:
                    raise RuntimeError("404 Not Found")
                return json.dumps({
                    "sha": "template-blob",
                    "content": base64.b64encode(
                        self.template.encode()
                    ).decode(),
                })
            if command[2] != _POLICY_PATH:
                raise AssertionError(f"unexpected endpoint: {command}")
            if self.policy_text is None:
                raise RuntimeError("404 Not Found")
            return json.dumps({
                "sha": "blobsha",
                "content": base64.b64encode(
                    self.policy_text.encode()
                ).decode(),
            })
        if command[:3] == ["gh", "issue", "create"]:
            body = command[command.index("--body") + 1]
            number = 500 + len(self.release_issues) + len(self.notices)
            if "orbi-milestone-advance" in body:
                self.notices.append({
                    "number": number,
                    "title": command[command.index("--title") + 1],
                    "body": body,
                })
            else:
                self.release_issues.append({"number": number, "body": body})
            return f"https://github.com/{REPO}/issues/{number}"
        raise AssertionError(f"unexpected command: {command}")

    def first_index(self, *prefix: str) -> int:
        """The position of the first recorded argv starting with
        `prefix` (−1 when none was recorded): the fake's own way of
        stating the order two steps ran in."""
        for index, command in enumerate(self.commands):
            if command[:len(prefix)] == list(prefix):
                return index
        return -1


def _search_of(command: list[str]) -> str:
    """The `--search` value of one `gh issue list` argv ("" when absent)."""
    return command[command.index("--search") + 1] if "--search" in command else ""
