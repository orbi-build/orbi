"""Read one PR's status check rollup without the workflowRun field.

Issue #1534: `gh pr view --json statusCheckRollup` makes gh's generated
GraphQL fragment also select `checkSuite.workflowRun`, which needs the
GitHub App installation's Actions:read permission and fails with
`Resource not accessible by integration` on a private repository. This
sibling module is the home of the one GraphQL read that selects only the
fields the status gates classify (`CheckRun{name status conclusion}` and
`StatusContext{context state}`), so `github.py` stays within its size
ceiling (Issue #1229).

It binds the `run_command` seam directly, like `release_notes.py` does
for its own GraphQL reads (there is no retry classification for a
parameter-carrying `gh api` call, so `run_gh_read_command` would not
retry this either).
"""
from __future__ import annotations

import json
from pathlib import Path

from orbi.journal import run_command

PR_STATUS_CHECK_ROLLUP_QUERY = """query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      mergeable
      headRefOid
      statusCheckRollup {
        contexts(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            __typename
            ... on CheckRun { name status conclusion }
            ... on StatusContext { context state }
          }
        }
      }
    }
  }
}"""


def pr_status_rollup(number: int, *, repo: str,
                     cwd: Path | None = None,
                     timeout: int | None = None) -> dict:
    """Read a PR state, mergeability, head and status check rollup.

    ONE `gh api graphql` query selecting exactly the fields the status
    gates classify. `contexts` is paginated on `pageInfo`, so a rollup
    larger than one page is never truncated.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"pr status rollup requires owner/name repo: {repo!r}")
    contexts: list[dict] = []
    cursor: str | None = None
    while True:
        command = [
            "gh", "api", "graphql",
            "-f", f"query={PR_STATUS_CHECK_ROLLUP_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}",
            "-F", f"number={number}",
        ]
        if cursor is not None:
            command += ["-f", f"cursor={cursor}"]
        raw = run_command(command, cwd=cwd, timeout=timeout)
        data = json.loads(raw)
        pull = ((data.get("data") or {}).get("repository") or {}).get(
            "pullRequest")
        if not isinstance(pull, dict):
            raise ValueError("pr status rollup: no pullRequest in response")
        connection = (pull.get("statusCheckRollup") or {}).get("contexts") or {}
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError("pr statusCheckRollup contexts must be a JSON array")
        contexts.extend(node for node in nodes if isinstance(node, dict))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            raise ValueError(
                "pr statusCheckRollup contexts pageInfo has no endCursor")
    return {
        "state": pull.get("state"),
        "mergeable": pull.get("mergeable"),
        "headRefOid": pull.get("headRefOid"),
        "statusCheckRollup": contexts,
    }
