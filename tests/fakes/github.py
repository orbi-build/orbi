"""In-memory GitHub fake at the adapter seam (Issue #789).

``FakeGh`` replaces the ``gh`` CLI process for the ``orbi.github``
adapter: the test patches the ONE subprocess seam
(``monkeypatch.setattr(seam, "run_command", fake)`` — never
``setattr(runner, ...)``), arranges state with the ``add_*`` helpers,
runs the real public entry points, and asserts the public surface
(labels, comments, returns). The observable behavior mirrors real
GitHub: label edits are idempotent, comments append in order, issue
state is OPEN/CLOSED, ``gh issue list`` returns the requested fields
newest-created first (``sort:created-asc`` reverses that, Issue #1734),
``blockedBy`` carries ``{"nodes": [...], "totalCount":
N}`` with explicit OPEN/CLOSED node states, and the authenticated
identity comes from ``gh auth status``.

Unsupported argv fails fast with ``CalledProcessError`` whose stderr
names the command — the same contract a mistyped ``gh`` invocation
gets from the real CLI, never a silent empty success. Failure stderr
never matches the adapter's transient-error classes (401/429/5xx/rate
limit), so a fake failure is never retried.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from urllib.parse import unquote

# The search qualifiers the ready scans use and the fake understands:
# `label:<name>`, `-label:<name>`, a quoted phrase (with spaces in it)
# scoped by `in:body`/`in:title`, and `milestone:"<title>"` (quoted
# titles may contain spaces — the same contract `ready_searches` pins).
_SEARCH_TOKEN_RE = re.compile(
    r'milestone:"([^"]+)"|"([^"]+)"|(\S+)'
)
# The one jq program the adapter runs against the milestones endpoint
# (`milestone_open_issue_count`): the title-select reading GitHub's own
# open-Issue counter.
def _with_response_headers(payload: str, status: int = 200,
                           reason: str = "OK") -> str:
    """The `gh api --include` shape: status line, headers, blank, body."""
    return (
        f"HTTP/2.0 {status} {reason}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "X-Ratelimit-Remaining: 4999\r\n"
        "\r\n"
        f"{payload}"
    )


_MILESTONE_JQ_RE = re.compile(
    r'^\.\[\] \| select\(\.title=="([^"]+)"\) \| \.open_issues$'
)
# GitHub's answer to the branch-rules read of a private repository on a
# plan without rulesets (Issue #1361). It is a 403 like a permission
# failure; only this documented message tells them apart.
_PLAN_LIMIT_403 = (
    "gh: Upgrade to GitHub Pro or make this repository public to enable "
    "this feature. (HTTP 403)"
)


class FakeGh:
    """One repository's GitHub state, answering the adapter's gh argv."""

    def __init__(self, repo: str, *, login: str = "orbi-bot"):
        self.repo = repo
        self.login = login
        self.issues: dict[int, dict] = {}
        self.prs: dict[int, dict] = {}
        self.milestones: dict[int, dict] = {}
        self.check_runs: dict[str, list] = {}
        self.repo_config: str | None = None
        self.branches: dict[str, dict] = {}
        self.rulesets_failure: str | None = None
        # The repository permission GitHub reports per user
        # (`collaborators/{login}/permission`, Issue #1806) and the one
        # login whose read fails server-side.
        self.collaborators: dict[str, str] = {}
        self.permission_failure: str | None = None
        # The App permission the check read needs (Issue #1918): while
        # set, `gh pr view --json state,statusCheckRollup` answers GitHub's
        # documented App refusal instead of the rollup — production's own
        # shape (a GraphQL subcommand prints no response status line).
        self.check_read_failure: str | None = None
        # The repository owner's account type (`gh api repos/<repo>` ->
        # `owner.type`), which picks the installation approval URL form.
        self.owner_type = "Organization"
        self._clock = 0

    # --- state arrangement (what a test does instead of patching) -------

    def add_issue(self, number: int, *, title: str = "Issue", body: str = "",
                  labels: tuple[str, ...] = (), milestone: int | None = None,
                  state: str = "open",
                  author: str = "issue-author") -> None:
        self.issues[number] = {
            "number": number, "title": title, "body": body,
            "state": state, "labels": list(labels), "milestone": milestone,
            "comments": [], "blocked_by": [],
            # The claim scans fetch `author` for the thin-ticket gate
            # (Issue #1336); `login` is the field `clarify` mentions.
            "author": {"login": author},
            "_created": self._next_clock(),
        }

    def add_blocker(self, number: int, blocker: int, *,
                    state: str = "OPEN") -> None:
        """Record the native blockedBy relation GitHub keeps on `number`."""
        self.issues[number]["blocked_by"].append(
            {"number": blocker, "state": state}
        )

    def comment(self, number: int, body: str, *, login: str = "maintainer",
                association: str = "MEMBER") -> None:
        """Add a human comment with an explicit author association."""
        self._append_comment(
            self.issues[number], body, login=login,
            association=association,
        )

    def _append_comment(self, issue: dict, body: str, *, login: str,
                        association: str) -> dict:
        """Append one comment carrying GitHub's own id and URL.

        Every real comment has a REST id and a permalink; the in-place
        edit path (`repos/{repo}/issues/comments/{id}` PATCH) reads the
        id from the URL, so the fake must answer the same shape.
        """
        clock = self._next_clock()
        comment = {
            "id": 1000 + clock,
            "url": (
                f"https://github.com/{self.repo}/issues/{issue['number']}"
                f"#issuecomment-{1000 + clock}"
            ),
            "author": {"login": login},
            "authorAssociation": association,
            "createdAt": self._next_stamp(),
            "body": body,
        }
        issue["comments"].append(comment)
        return comment

    def add_pr(self, number: int, *, head: str, base: str = "main",
               state: str = "OPEN", oid: str = "0" * 40,
               url: str | None = None, checks: tuple[dict, ...] = (),
               merged_at: str | None = None, body: str = "",
               merge_commit: str | None = None,
               title: str = "Pull request") -> None:
        owner, name = self.repo.split("/", 1)
        self.prs[number] = {
            "number": number, "title": title, "state": state,
            "headRefName": head,
            "baseRefName": base, "headRefOid": oid,
            "url": url or f"https://github.com/{self.repo}/pull/{number}",
            "statusCheckRollup": list(checks), "comments": [],
            "mergedAt": merged_at,
            # The fields `_query_open_prs` asks for and the review gates
            # read: the PR body (run marker / Fixes keyword) and the
            # head-repo identity (`_pr_head_repo`'s same-repo check).
            "baseRefOid": "0" * 40,
            "body": body,
            "headRepository": {"name": name},
            "headRepositoryOwner": {"login": owner},
            # The merge commit (`gh pr view --json mergeCommit`): GitHub's
            # documented `{"oid": ...}` object for a merged PR, null for a
            # PR that never merged (Issue #1895).
            "mergeCommit": {"oid": merge_commit} if merge_commit else None,
            "_created": self._next_clock(),
        }

    def add_milestone(self, number: int, *, title: str,
                      open_issues: int = 0, state: str = "open") -> None:
        """Seed one Milestone; `open_issues` is GitHub's OWN counter —
        the authority the release completeness gate reads (Issue #663),
        stored verbatim, never derived from the issue list. `state` is
        the milestone's lifecycle ("open"/"closed") — the idle advance
        sweep and the CLI set command read it."""
        self.milestones[number] = {
            "number": number, "title": title, "state": state,
            "open_issues": open_issues,
        }

    def add_check_runs(self, commit: str, runs: list[dict]) -> None:
        self.check_runs[commit] = runs

    def add_branch(self, name: str, *, protected: bool = False,
                   protection: dict | None = None,
                   head: str | None = None) -> None:
        """Seed one branch as the merge-gate preflight reads it: the
        branch's `protected` flag and, for a protected branch, the
        classic protection resource. `protection=None` on a protected
        branch is GitHub's 404 for protection the token cannot see
        (Issue #1174); an unseeded branch is a 404 too — the fake never
        invents protection state.

        `head` is the branch's CURRENT head (the `branches/{branch}`
        resource's `commit.sha`, Issue #1825); it stays empty unless the
        test seeds it, and an empty head never reads as a real commit."""
        self.branches[name] = {"protected": protected,
                               "protection": protection, "head": head}

    def set_rulesets_failure(self, mode: str) -> None:
        """Make the branch-rules endpoint fail as GitHub does: `plan`
        is the documented plan-limit 403 (a private repository on
        GitHub Free cannot carry rulesets, Issue #1361), `forbidden`
        the permission 403 of a token without administration
        permission."""
        self.rulesets_failure = mode

    def add_collaborator(self, login: str, permission: str = "admin") -> None:
        """Seed one user's repository permission ("admin"/"write"/...).

        An unseeded login is GitHub's 404 for a user this token cannot
        see: the fake never invents access it was not given.
        """
        self.collaborators[login] = permission

    def set_check_read_failure(
        self, enabled: bool = True, *,
        message: str = "gh: Resource not accessible by integration (HTTP 403)",
    ) -> None:
        """Refuse the PR's check read like an unapproved App permission.

        `message` is the refusal the fake raises, so a test can also model
        every OTHER failed read (a 404, a 5xx) on the same call.
        """
        self.check_read_failure = message if enabled else None

    def set_owner_type(self, account_type: str) -> None:
        """Set the repository owner's account type (User/Organization)."""
        self.owner_type = account_type

    def set_permission_failure(self, login: str) -> None:
        """Make that user's permission read fail server-side (a 5xx)."""
        self.permission_failure = login

    def set_repo_config(self, text: str) -> None:
        """Seed the repository's default `.github/orbi.toml` — the
        contents API serves it back base64-encoded, the same shape
        `read_repo_config` parses. A `config_path` override is not
        modeled: a read of any other path fails fast as unsupported."""
        self.repo_config = text

    # --- the gh process seam ---------------------------------------------

    def __call__(self, command: list[str], *, cwd=None,
                 timeout=None, **kwargs) -> str:
        # The adapter appends `--include` to a `gh api` command so it can
        # read the response status (Issue #1760). gh prints the status line
        # and the headers in front of the body: the fake answers that same
        # documented shape, and a `--include` it was not given changes
        # nothing.
        include = command[-1:] == ["--include"]
        if include:
            command = command[:-1]
        payload = self._answer(command)
        return _with_response_headers(payload) if include else payload

    def _answer(self, command: list[str]) -> str:
        # `failure_log_level` and other adapter logging hints are not
        # command semantics — the fake answers argv, log decoration is
        # the caller's business.
        if len(command) < 2 or command[:1] != ["gh"]:
            return self._unsupported(command)
        verb = command[1]
        if verb == "issue" and len(command) > 2:
            return self._issue(command[2:])
        if verb == "pr" and len(command) > 2:
            return self._pr(command[2:])
        if verb == "api" and len(command) > 2:
            return self._api(command[2:])
        if verb == "auth" and command[2:4] == ["status", "--hostname"]:
            return (
                "github.com\n"
                f"  ✓ Logged in to github.com account {self.login}"
                " (keyring)\n"
                "  - Active account: true\n"
                "  - Git operations protocol: https\n"
            )
        return self._unsupported(command)

    # --- issue verbs -------------------------------------------------------

    # The flags each verb implements; anything else fails fast (real gh
    # errors on an unknown flag, and silently ignoring a filter would
    # widen the match set — the same wrong-answer class as an
    # uninterpreted search qualifier).
    _ISSUE_FLAGS = {
        "view": ("--repo", "--json"),
        "edit": ("--repo", "--add-label", "--remove-label", "--milestone"),
        "comment": ("--repo", "--body"),
        "close": ("--repo",),
        "create": ("--repo", "--title", "--body", "--milestone", "--label"),
    }

    def _issue(self, args: list[str]) -> str:
        sub = args[0]
        if sub == "list":
            return self._issue_list(args[1:])
        if sub == "create":
            return self._issue_create(args[1:])
        if sub not in self._ISSUE_FLAGS:
            self._unsupported(["gh", "issue", sub])
        number = int(args[1])
        issue = self._issue_or_fail(number)
        flags = self._flags(args[2:])
        self._known_flags(
            flags, self._ISSUE_FLAGS[sub], ["gh", "issue", sub]
        )
        self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
        if sub == "view":
            return json.dumps(
                self._issue_fields(issue, flags["--json"][0].split(","))
            )
        if sub == "edit":
            self._issue_edit(issue, flags)
            return ""
        if sub == "comment":
            self._append_comment(
                issue, flags["--body"][0], login=self.login,
                association="NONE",
            )
            return ""
        # `close` — the last verb `_ISSUE_FLAGS` admits above.
        issue["state"] = "closed"
        return ""

    def _issue_create(self, args: list[str]) -> str:
        """`gh issue create` for the `orbi add`/`orbi suggest` path.

        The new Issue takes the next free number and the requested
        Milestone (by title, the way the real CLI resolves it); the
        returned URL is the observable result.
        """
        flags = self._flags(args)
        self._known_flags(
            flags, self._ISSUE_FLAGS["create"], ["gh", "issue", "create"],
        )
        self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
        title = (flags.get("--title") or [""])[0]
        body = (flags.get("--body") or [""])[0]
        milestone = None
        if "--milestone" in flags:
            milestone = self._milestone_number(flags["--milestone"][0])
        number = max(self.issues) + 1 if self.issues else 1
        self.add_issue(number, title=title, body=body, milestone=milestone,
                       labels=tuple(flags.get("--label") or []))
        return f"https://github.com/{self.repo}/issues/{number}"

    def _milestone_number(self, title: str) -> int:
        for milestone in self.milestones.values():
            if milestone["title"] == title:
                return milestone["number"]
        self._fail(
            1,
            f"gh: Could not resolve to a Milestone with the title of "
            f"{title!r}.",
        )

    def _issue_list(self, args: list[str]) -> str:
        flags = self._flags(args)
        self._known_flags(
            flags,
            ("--repo", "--state", "--label", "--search", "--json",
             "--limit"),
            ["gh", "issue", "list"],
        )
        self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
        state = (flags.get("--state") or ["open"])[0]
        label = (flags.get("--label") or [None])[0]
        search = (flags.get("--search") or [None])[0]
        fields = flags["--json"][0].split(",")
        limit = int((flags.get("--limit") or ["30"])[0])
        (include, exclude, body_terms, title_terms, milestone_title,
         oldest_first) = self._parse_search(search)
        matches = [
            issue for issue in self.issues.values()
            if self._matches(issue, state, label, include, exclude,
                             body_terms, title_terms, milestone_title)
        ]
        matches.sort(key=lambda issue: issue["_created"],
                     reverse=not oldest_first)
        return json.dumps([
            self._issue_fields(issue, fields) for issue in matches[:limit]
        ])

    def _parse_search(self, search: str | None):
        """Split a search string into label groups, Milestone title, order.

        One `label:` qualifier may carry a comma-separated OR group
        (`label:a,b` — GitHub's documented OR within one qualifier, the
        same contract `orbi.claim` relies on); groups AND together. The
        order follows GitHub: results are newest-created first unless the
        documented `sort:created-asc` qualifier asks for the oldest first
        (Issue #1734's review scan). A quoted phrase is matched against
        the Issue BODY — the only form Orbi's scans use, the
        external-takeover prefilter `"orbi:external-pr" in:body`
        (Issue #842). An unrecognized qualifier fails fast: a scan the
        fake cannot interpret must never silently match nothing.
        """
        include: list[list[str]] = []
        exclude: list[str] = []
        body_terms: list[str] = []
        title_terms: list[str] = []
        milestone_title: str | None = None
        oldest_first = False
        if not search:
            return (include, exclude, body_terms, title_terms,
                    milestone_title, oldest_first)
        # `in:title` / `in:body` scope the quoted phrase written BEFORE
        # them (the documented GitHub qualifier the CI triage lookup
        # uses); an unscoped phrase matches the body, as before.
        pending: list[str] = []
        for qualifier, phrase, token in _SEARCH_TOKEN_RE.findall(search):
            if qualifier:
                if milestone_title is not None:
                    self._fail(
                        1, f"fake-gh: two milestone qualifiers: {search!r}"
                    )
                milestone_title = qualifier
            elif phrase:
                pending.append(phrase)
            elif token.startswith("-label:"):
                exclude.append(token[len("-label:"):])
            elif token.startswith("label:"):
                include.append(token[len("label:"):].split(","))
            elif token == "sort:created-asc":
                oldest_first = True
            elif token == "in:body":
                body_terms.extend(pending)
                pending = []
            elif token == "in:title":
                title_terms.extend(pending)
                pending = []
            else:
                self._fail(
                    1, f"fake-gh: unsupported search qualifier: {search!r}"
                )
        body_terms.extend(pending)
        return (include, exclude, body_terms, title_terms,
                milestone_title, oldest_first)

    def _matches(self, issue: dict, state: str, label: str | None,
                 include: list[list[str]], exclude: list[str],
                 body_terms: list[str], title_terms: list[str],
                 milestone_title: str | None) -> bool:
        # `--state all` lists both states (GitHub has no state filter to
        # apply); "open"/"closed" filter exactly.
        if state != "all" and issue["state"] != state:
            return False
        if label is not None and label not in issue["labels"]:
            return False
        for group in include:
            if not set(group) & set(issue["labels"]):
                return False
        if any(name in issue["labels"] for name in exclude):
            return False
        if any(term not in issue["body"] for term in body_terms):
            return False
        if any(term not in issue["title"] for term in title_terms):
            return False
        if milestone_title is not None:
            milestone = self.milestones.get(issue["milestone"])
            if milestone is None or milestone["title"] != milestone_title:
                return False
        return True

    def _issue_or_fail(self, number: int) -> dict:
        issue = self.issues.get(number)
        if issue is None:
            self._fail(
                1, f"gh: Could not resolve to an Issue with the number "
                f"of {number}."
            )
        return issue

    def _issue_edit(self, issue: dict, flags: dict) -> None:
        for label in flags.get("--add-label") or []:
            if label not in issue["labels"]:
                issue["labels"].append(label)
        for label in flags.get("--remove-label") or []:
            if label in issue["labels"]:
                issue["labels"].remove(label)
        if "--milestone" in flags:
            issue["milestone"] = self._milestone_number(
                flags["--milestone"][0]
            )

    def _issue_fields(self, issue: dict, fields: list[str]) -> dict:
        rendered: dict = {}
        for field in fields:
            if field == "labels":
                rendered[field] = [
                    {"name": name} for name in issue["labels"]
                ]
            elif field == "milestone":
                milestone = self.milestones.get(issue["milestone"])
                rendered[field] = (
                    {"number": milestone["number"],
                     "title": milestone["title"]}
                    if milestone else None
                )
            elif field == "blockedBy":
                nodes = issue["blocked_by"]
                rendered[field] = {
                    "nodes": list(nodes), "totalCount": len(nodes),
                }
            elif field == "state":
                rendered[field] = issue["state"].upper()
            elif field == "url":
                rendered[field] = (
                    f"https://github.com/{self.repo}/issues/"
                    f"{issue['number']}"
                )
            elif field in issue:
                rendered[field] = issue[field]
            else:
                self._fail(
                    2, f"fake-gh: unsupported issue field: {field!r}"
                )
        return rendered

    # --- pr verbs -----------------------------------------------------------

    def _pr(self, args: list[str]) -> str:
        sub = args[0]
        if sub == "list":
            flags = self._flags(args[1:])
            self._known_flags(
                flags, ("--repo", "--state", "--head", "--json", "--limit"),
                ["gh", "pr", "list"],
            )
            self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
            state = (flags.get("--state") or ["open"])[0]
            head = (flags.get("--head") or [None])[0]
            fields = flags["--json"][0].split(",")
            limit = int((flags.get("--limit") or ["30"])[0])
            matches = [
                pr for pr in self.prs.values()
                if pr["state"].lower() == state.lower()
                and (head is None or pr["headRefName"] == head)
            ]
            matches.sort(key=lambda pr: -pr["_created"])
            return json.dumps([
                {field: pr.get(field, False) if field == "isCrossRepository"
                 else pr[field] for field in fields}
                for pr in matches[:limit]
            ])
        number = int(args[1])
        pr = self.prs.get(number)
        if pr is None:
            self._fail(
                1, f"gh: Could not resolve to a pull request with the "
                f"number of {number}."
            )
        if sub == "comment":
            flags = self._flags(args[2:])
            self._known_flags(
                flags, ("--repo", "--body"), ["gh", "pr", "comment"]
            )
            self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
            pr["comments"].append({
                "author": {"login": self.login},
                "authorAssociation": "NONE",
                "createdAt": self._next_stamp(),
                "body": flags["--body"][0],
            })
            return ""
        flags = self._flags(args[2:])
        self._known_flags(flags, ("--repo", "--json"), ["gh", "pr", "view"])
        self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
        fields = flags["--json"][0].split(",")
        if self.check_read_failure and "statusCheckRollup" in fields:
            # The App permission refusal on the check read (Issue #1918),
            # verbatim: `gh pr view` is a GraphQL subcommand, so the
            # failure carries GitHub's sentence and no response status.
            self._fail(1, self.check_read_failure)
        rendered = {}
        for field in fields:
            if field not in pr:
                self._fail(2, f"fake-gh: unsupported pr field: {field!r}")
            rendered[field] = pr[field]
        return json.dumps(rendered)

    # --- api verbs -----------------------------------------------------------

    def _api(self, args: list[str]) -> str:
        if args[:2] == ["--method", "PUT"]:
            return self._api_put(args[2:])
        if "--method" in args and args[args.index("--method") + 1] == "PATCH":
            # gh accepts the flag on either side of the path; the adapter
            # writes `gh api <path> --method PATCH --field ...`.
            index = args.index("--method")
            return self._api_patch(args[:index] + args[index + 2:])
        path = args[0]
        flags = self._flags(args[1:])
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/collaborators/([^/]+)/permission", path,
        )
        if match:
            # The repository permission of one user (Issue #1806): the
            # documented effective permission, and GitHub's 404 for a user
            # this token cannot see.
            self._repo_or_fail(match.group(1))
            self._known_flags(flags, (), ["gh", "api", path])
            login = match.group(2)
            if self.permission_failure == login:
                self._fail(
                    1, "gh: Server Error (HTTP 500)", status=500,
                    reason="Internal Server Error",
                )
            permission = self.collaborators.get(login)
            if permission is None:
                self._fail(
                    1, f"gh: {login} is not a user (HTTP 404)", status=404,
                    reason="Not Found",
                )
            return json.dumps({"permission": permission,
                               "role_name": permission})
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/milestones\?"
            r"state=all&per_page=100",
            path,
        )
        if match:
            # The milestone LIST read (`list_milestones`):
            # `--paginate --slurp` wraps one page as an array of arrays,
            # insertion order (GitHub returns creation order).
            self._repo_or_fail(match.group(1))
            self._known_flags(flags, ("--paginate", "--slurp"),
                              ["gh", "api", path])
            return json.dumps([list(self.milestones.values())])
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/milestones", path)
        if match:
            self._repo_or_fail(match.group(1))
            return self._milestone_jq((flags.get("--jq") or [""])[0])
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs", path,
        )
        if match:
            self._repo_or_fail(match.group(1))
            if (flags.get("--jq") or [""])[0] != ".check_runs":
                self._unsupported(["gh", "api", path])
            runs = self.check_runs.get(match.group(2))
            if runs is None:
                self._fail(1, f"gh: HTTP 404: no commit {match.group(2)}")
            return json.dumps(runs)
        match = re.fullmatch(r"repos/([^/]+/[^/]+)", path)
        if match:
            # The repository resource: `owner.type` is what picks the
            # installation approval URL form (Issue #1918); a payload
            # without the merge-method booleans keeps the merge exec's
            # documented `--merge` fallback.
            self._repo_or_fail(match.group(1))
            return json.dumps({
                "owner": {
                    "login": match.group(1).split("/", 1)[0],
                    "type": self.owner_type,
                },
            })
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/contents/\.github/orbi\.toml", path,
        )
        if match:
            self._repo_or_fail(match.group(1))
            if self.repo_config is None:
                self._fail(1, "gh: HTTP 404: not found")
            return json.dumps({
                "sha": hashlib.sha1(
                    self.repo_config.encode("utf-8"),
                ).hexdigest(),
                "content": base64.b64encode(
                    self.repo_config.encode("utf-8"),
                ).decode("ascii"),
            })
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/branches/([^/]+)/protection", path,
        )
        if match:
            self._repo_or_fail(match.group(1))
            # GitHub's branch path carries the URL-encoded branch name
            # (`release%2F7`), the way the adapter quotes it.
            protection = self._branch_or_fail(
                unquote(match.group(2)))["protection"]
            if protection is None:
                self._fail(1, "gh: HTTP 404: Branch not protected")
            return json.dumps(protection)
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/branches/([^/]+)", path)
        if match:
            self._repo_or_fail(match.group(1))
            branch = self._branch_or_fail(unquote(match.group(2)))
            return json.dumps({
                "protected": branch["protected"],
                "commit": {"sha": branch.get("head")},
            })
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/rules/branches/([^/]+)", path,
        )
        if match:
            self._repo_or_fail(match.group(1))
            if self.rulesets_failure == "plan":
                self._fail(1, _PLAN_LIMIT_403)
            if self.rulesets_failure == "forbidden":
                self._fail(
                    1, "gh: Resource not accessible by integration (HTTP 403)"
                )
            # The documented empty result: no rule applies to the branch
            # (or the rulesets answered, and none blocks it).
            return "[]"
        return self._unsupported(["gh", "api", path])

    def _api_patch(self, args: list[str]) -> str:
        """The in-place Issue-comment edit (Issue #825's PATCH route).

        Only `repos/{repo}/issues/comments/{id}` with a single
        `--field body=<text>` is modelled: a mistyped write fails fast
        instead of silently editing something else.
        """
        if len(args) < 2:
            return self._unsupported(["gh", "api", "--method", "PATCH", *args])
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/issues/comments/(\d+)", args[0],
        )
        if match is None:
            return self._unsupported(["gh", "api", "--method", "PATCH", *args])
        self._repo_or_fail(match.group(1))
        comment_id = int(match.group(2))
        flags = self._flags(args[1:])
        self._known_flags(
            flags, ("--field",), ["gh", "api", "--method", "PATCH", args[0]],
        )
        field = (flags.get("--field") or [""])[0]
        key, _, body = field.partition("=")
        if key != "body":
            return self._unsupported(["gh", "api", "--method", "PATCH", *args])
        for issue in self.issues.values():
            for comment in issue["comments"]:
                if comment.get("id") == comment_id:
                    comment["body"] = body
                    return json.dumps(comment)
        self._fail(1, f"gh: Could not resolve an Issue comment {comment_id}.")

    def _api_put(self, args: list[str]) -> str:
        """The contents API PUT (create/update a file): `gh api --method
        PUT <path> -f key=value ...`. Only the repository policy path is
        modelled, and only for the three required fields, so a mistyped
        write fails fast instead of silently updating the wrong file."""
        path = args[0]
        match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/contents/\.github/orbi\.toml", path,
        )
        if match is None:
            return self._unsupported(["gh", "api", "--method", "PUT", *args])
        self._repo_or_fail(match.group(1))
        fields: dict[str, str] = {}
        index = 1
        while index < len(args):
            if args[index] != "-f" or index + 1 >= len(args):
                return self._unsupported(
                    ["gh", "api", "--method", "PUT", *args]
                )
            key, _, value = args[index + 1].partition("=")
            fields[key] = value
            index += 2
        if set(fields) != {"message", "content", "sha"}:
            return self._unsupported(["gh", "api", "--method", "PUT", *args])
        try:
            self.repo_config = base64.b64decode(
                fields["content"], validate=True,
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self._fail(1, "fake-gh: content is not base64-encoded UTF-8")
        return json.dumps({
            "content": {"sha": fields["sha"]},
            "commit": {"sha": "0" * 40},
        })

    def _milestone_jq(self, jq: str) -> str:
        """Answer the title-select jq with GitHub's own open_issues
        counter; an unknown title yields empty output (the adapter
        turns that into its not-found failure, never a silent 0)."""
        match = _MILESTONE_JQ_RE.fullmatch(jq)
        if match is None:
            self._unsupported(["gh", "api", "--jq", jq])
        for milestone in self.milestones.values():
            if milestone["title"] == match.group(1):
                return str(milestone["open_issues"])
        return ""

    # --- shared helpers -------------------------------------------------------

    def _repo_or_fail(self, repo: str) -> None:
        if repo != self.repo:
            self._fail(
                1, f"fake-gh: repository mismatch: {repo} != {self.repo}"
            )

    def _branch_or_fail(self, name: str) -> dict:
        branch = self.branches.get(name)
        if branch is None:
            self._fail(1, f"gh: HTTP 404: Branch not found: {name}")
        return branch

    def _known_flags(self, flags: dict[str, list[str]], known,
                     context: list[str]) -> None:
        unknown = sorted(set(flags) - set(known))
        if unknown:
            self._unsupported([*context, *unknown])

    def _flags(self, args: list[str]) -> dict[str, list[str]]:
        """Group `--flag value...` argv segments. A value that itself
        starts with `--` is not supported (none of the adapter's
        payloads start that way)."""
        flags: dict[str, list[str]] = {}
        index = 0
        while index < len(args):
            argument = args[index]
            if not argument.startswith("--"):
                self._unsupported(["gh", *args])
            index += 1
            values = flags.setdefault(argument, [])
            while index < len(args) and not args[index].startswith("--"):
                values.append(args[index])
                index += 1
        return flags

    def _next_clock(self) -> int:
        self._clock += 1
        return self._clock

    def _next_stamp(self) -> str:
        minutes, seconds = divmod(self._next_clock(), 60)
        return f"2026-09-13T00:{minutes:02d}:{seconds:02d}Z"

    def _fail(self, code: int, stderr: str, *,
              status: int | None = None, reason: str = "Error"):
        """Raise a failed call; `status` also emits the response headers.

        A failed `gh api --include` prints the status line and headers
        in front of the (empty) body, which is how the adapter classifies
        the failure (Issue #1760).
        """
        raise subprocess.CalledProcessError(
            code, ["gh"],
            output=(_with_response_headers("", status, reason)
                    if status is not None else ""),
            stderr=stderr,
        )

    def _unsupported(self, command: list[str]):
        self._fail(
            2, "fake-gh: unsupported command: " + " ".join(command)
        )


def no_current_issue(monkeypatch) -> None:
    """Fake the `gh issue list` boundary: no current Issue is observed.

    A `doctor` report over a real loaded config reads the current Issue
    per source repo. Keeping that fake here, in the sanctioned fakes
    home, means the test patches the gh boundary once instead of adding
    an internal `cli_status.current_issue` coupling (patch ratchet,
    Issue #789).
    """
    from orbi import github

    monkeypatch.setattr(github, "run_gh_read_command", lambda *a, **k: "[]")
