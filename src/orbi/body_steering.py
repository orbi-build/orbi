"""Trust decisions for Issue body edits during a running delivery."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from orbi.comment_trust import comment_is_trusted
from orbi.github import run_gh_read_command
from orbi.journal import event, issue_context


_ISSUE_BODY_EDITOR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      body
      editor { login }
    }
  }
}
"""


@dataclass(frozen=True)
class BodyEdit:
    body: str
    editor: str | None
    trusted: bool


def issue_body_editor(number: int, *, repo: str, timeout: int) -> tuple[str, str | None]:
    """Read the body and its editor together so the trust check applies to that text."""
    owner, _, name = repo.partition("/")
    raw = run_gh_read_command([
        "gh", "api", "graphql", "-f", f"query={_ISSUE_BODY_EDITOR_QUERY}",
        "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
    ], timeout=timeout)
    try:
        payload = json.loads(raw)
        issue = payload["data"]["repository"]["issue"]
        body, editor = issue["body"], issue["editor"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("malformed issue body editor response") from exc
    if payload.get("errors") or not isinstance(body, str) or (
        editor is not None and not isinstance(editor, dict)
    ):
        raise ValueError("malformed issue body editor response")
    login = editor.get("login") if editor is not None else None
    return body, login if isinstance(login, str) and login else None


def poll_body_edit(
    snapshot: dict, recorded_body: object, *, number: int, repo: str,
) -> BodyEdit | None:
    polled_body = snapshot.get("body")
    if not isinstance(polled_body, str) or polled_body == recorded_body:
        return None
    body, editor = issue_body_editor(number, repo=repo, timeout=30)
    if body == recorded_body:
        return None
    trusted = comment_is_trusted({"author": {"login": editor}}, repo=repo)
    return BodyEdit(body, editor, trusted)


def ignore_untrusted_body_edit(edit: BodyEdit, issue: dict, repo: str) -> str:
    event(
        "steering_body_untrusted", level=logging.WARNING,
        issue=issue_context(repo, int(issue["number"])), editor=edit.editor or "-",
    )
    return edit.body
