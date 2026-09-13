"""In-memory GitHub fake at the adapter seam (Issue #789).

``FakeGh`` replaces the ``gh`` CLI process for the ``orbi.github``
adapter: the test patches the ONE subprocess seam
(``monkeypatch.setattr(seam, "run_command", fake)`` — never
``setattr(runner, ...)``), arranges state with the ``add_*`` helpers,
runs the real public entry points, and asserts the public surface
(labels, comments, returns). The observable behavior mirrors real
GitHub: label edits are idempotent, comments append in order, issue
state is OPEN/CLOSED, ``gh issue list`` returns the requested fields
newest-first, ``blockedBy`` carries ``{"nodes": [...], "totalCount":
N}`` with explicit OPEN/CLOSED node states, and the authenticated
identity comes from ``gh auth status``.

Unsupported argv fails fast with ``CalledProcessError`` whose stderr
names the command — the same contract a mistyped ``gh`` invocation
gets from the real CLI, never a silent empty success. Failure stderr
never matches the adapter's transient-error classes (401/429/5xx/rate
limit), so a fake failure is never retried.
"""
from __future__ import annotations

import json
import re
import subprocess

# The search qualifiers the ready scans use and the fake understands:
# `label:<name>`, `-label:<name>` and `milestone:"<title>"` (quoted
# titles may contain spaces — the same contract `ready_searches` pins).
_SEARCH_TOKEN_RE = re.compile(r'milestone:"([^"]+)"|(\S+)')
# The one jq program the adapter runs against the milestones endpoint
# (`milestone_open_issue_count`): the title-select reading GitHub's own
# open-Issue counter.
_MILESTONE_JQ_RE = re.compile(
    r'^\.\[\] \| select\(\.title=="([^"]+)"\) \| \.open_issues$'
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
        self._clock = 0

    # --- state arrangement (what a test does instead of patching) -------

    def add_issue(self, number: int, *, title: str = "Issue", body: str = "",
                  labels: tuple[str, ...] = (), milestone: int | None = None,
                  state: str = "open") -> None:
        self.issues[number] = {
            "number": number, "title": title, "body": body,
            "state": state, "labels": list(labels), "milestone": milestone,
            "comments": [], "blocked_by": [],
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
        self.issues[number]["comments"].append({
            "author": {"login": login},
            "authorAssociation": association,
            "createdAt": self._next_stamp(),
            "body": body,
        })

    def add_pr(self, number: int, *, head: str, base: str = "main",
               state: str = "OPEN", oid: str = "0" * 40,
               url: str | None = None, checks: tuple[dict, ...] = ()) -> None:
        self.prs[number] = {
            "number": number, "state": state, "headRefName": head,
            "baseRefName": base, "headRefOid": oid,
            "url": url or f"https://github.com/{self.repo}/pull/{number}",
            "statusCheckRollup": list(checks), "comments": [],
            "_created": self._next_clock(),
        }

    def add_milestone(self, number: int, *, title: str,
                      open_issues: int = 0) -> None:
        """Seed one Milestone; `open_issues` is GitHub's OWN counter —
        the authority the release completeness gate reads (Issue #663),
        stored verbatim, never derived from the issue list."""
        self.milestones[number] = {
            "number": number, "title": title, "state": "open",
            "open_issues": open_issues,
        }

    def add_check_runs(self, commit: str, runs: list[dict]) -> None:
        self.check_runs[commit] = runs

    # --- the gh process seam ---------------------------------------------

    def __call__(self, command: list[str], *, cwd=None,
                 timeout=None) -> str:
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
        "edit": ("--repo", "--add-label", "--remove-label"),
        "comment": ("--repo", "--body"),
        "close": ("--repo",),
    }

    def _issue(self, args: list[str]) -> str:
        sub = args[0]
        if sub == "list":
            return self._issue_list(args[1:])
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
            issue["comments"].append({
                "author": {"login": self.login},
                "authorAssociation": "NONE",
                "createdAt": self._next_stamp(),
                "body": flags["--body"][0],
            })
            return ""
        # `close` — the last verb `_ISSUE_FLAGS` admits above.
        issue["state"] = "closed"
        return ""

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
        include, exclude, milestone_title = self._parse_search(search)
        matches = [
            issue for issue in self.issues.values()
            if self._matches(issue, state, label, include, exclude,
                             milestone_title)
        ]
        matches.sort(key=lambda issue: -issue["_created"])
        return json.dumps([
            self._issue_fields(issue, fields) for issue in matches[:limit]
        ])

    def _parse_search(self, search: str | None):
        """Split a search string into include/exclude labels plus the
        Milestone title. An unrecognized qualifier fails fast: a scan
        the fake cannot interpret must never silently match nothing."""
        include: list[str] = []
        exclude: list[str] = []
        milestone_title: str | None = None
        if not search:
            return include, exclude, milestone_title
        for qualifier, token in _SEARCH_TOKEN_RE.findall(search):
            if qualifier:
                if milestone_title is not None:
                    self._fail(
                        1, f"fake-gh: two milestone qualifiers: {search!r}"
                    )
                milestone_title = qualifier
            elif token.startswith("-label:"):
                exclude.append(token[len("-label:"):])
            elif token.startswith("label:"):
                include.append(token[len("label:"):])
            else:
                self._fail(
                    1, f"fake-gh: unsupported search qualifier: {search!r}"
                )
        return include, exclude, milestone_title

    def _matches(self, issue: dict, state: str, label: str | None,
                 include: list[str], exclude: list[str],
                 milestone_title: str | None) -> bool:
        if issue["state"] != state:
            return False
        if label is not None and label not in issue["labels"]:
            return False
        if any(name not in issue["labels"] for name in include):
            return False
        if any(name in issue["labels"] for name in exclude):
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
                flags, ("--state", "--head", "--json", "--limit"),
                ["gh", "pr", "list"],
            )
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
                {field: pr[field] for field in fields}
                for pr in matches[:limit]
            ])
        number = int(args[1])
        pr = self.prs.get(number)
        if pr is None:
            self._fail(
                1, f"gh: Could not resolve to a pull request with the "
                f"number of {number}."
            )
        flags = self._flags(args[2:])
        self._known_flags(flags, ("--repo", "--json"), ["gh", "pr", "view"])
        self._repo_or_fail((flags.get("--repo") or [self.repo])[0])
        fields = flags["--json"][0].split(",")
        rendered = {}
        for field in fields:
            if field not in pr:
                self._fail(2, f"fake-gh: unsupported pr field: {field!r}")
            rendered[field] = pr[field]
        return json.dumps(rendered)

    # --- api verbs -----------------------------------------------------------

    def _api(self, args: list[str]) -> str:
        path = args[0]
        flags = self._flags(args[1:])
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
        return self._unsupported(["gh", "api", path])

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

    def _fail(self, code: int, stderr: str):
        raise subprocess.CalledProcessError(
            code, ["gh"], output="", stderr=stderr
        )

    def _unsupported(self, command: list[str]):
        self._fail(
            2, "fake-gh: unsupported command: " + " ".join(command)
        )
