"""`/orbi split`: turn the proposed split into child tickets (Issue #1927).

The clarification gate stops a thin ticket and may attach a `kind=split`
proposal to its stop comment (Issue #1926). This module registers the one
command that acts on it: a person with write permission replies
``/orbi split`` and every item of the proposal — **as the comment reads at
that moment**, so editing it first changes what is created — becomes a
child ticket.

The command runs from the stopped ticket's own scan
(`ticket_command.process_labeled_commands`): the ticket carries
`ai-needs-detail`, so no claim scan looks at it, and that scan is the only
place a comment on it is read.

It is not a second state machine. The steps ride GitHub's own primitives:
`gh issue create` for the child, the native sub-issue relation for the
parent link, the native blockedBy relation for an item's ``(after N)`` and
the existing delivery labels for the queue. What this module owns is the
ORDER and the idempotency of the seven steps:

1. find or open every child;
2. attach it as a sub-issue of this ticket;
3. add its ``blockedBy`` edge to the earlier item's child;
4. add the dispatch label to a child that never had a delivery label;
5. mark the parent `ai-epic` and release its dispatch label;
6. post ONE receipt listing the children;
7. remove `ai-needs-detail` LAST.

Because the scan label goes last, a run interrupted at any earlier step is
still scanned on the next tick and the same command continues from what is
already on GitHub; a ticket that already carries this command's success
receipt only gets step 7. The success receipt uses the command layer's own
marker keyed on the command comment AND must be a comment this runner wrote
(`_runner_receipt`): the marker is public text, so "has this command already
succeeded?" is a marker lookup over the raw bodies, never prose, and never a
copy someone else pasted (Issue #1938).

A re-run adopts an existing child only when THIS runner authored it, its
hidden marker names this parent and item, and its title, acceptance lines
and dependency still equal the item. Anything else — a copied marker, an
edited item, two matches — is never adopted blindly: the command stops with
a receipt that lists what already exists.

Every GitHub call a step makes is wrapped in `run_step`: the shared layer posts
its one failed receipt only for a `TicketCommandError` (the same rule
`/orbi fix-base` follows), so a raw `gh` refusal must never escape into the
tick's bypass log — the person who replied would see no receipt at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from orbi import clarify, language
from orbi.delivery_labels import (
    BUG_LABEL,
    EPIC_LABEL,
    LIFECYCLE_STATES,
    NEEDS_DETAIL_LABEL,
    P0_LABEL,
    READY_LABEL,
    label_names,
)
from orbi.github import (
    authenticated_github_login,
    comment_issue,
    edit_issue,
    issue_view,
    list_issues,
    run_gh_write_command,
    strip_bot_suffix,
)
from orbi.journal import run_command
from orbi.ticket_command import (
    CommandSpec,
    TicketCommandError,
    command_comment_key,
    command_receipt_marker,
    register_command,
    run_step,
)

# The hidden marker every child carries: it names the parent ticket and the
# proposal item, so a re-run finds exactly its own children instead of
# guessing from a title.
CHILD_MARKER = "orbi:split-child"
CHILD_MARKER_RE = re.compile(r"<!-- orbi:split-child:([0-9]+):([0-9]+) -->")
# The fields one child needs: the adoption check reads the body (marker,
# outcome, acceptance), the steps read the relations and the labels.
CHILD_FIELDS = "number,title,body,author,labels,parent,blockedBy"
PARENT_FIELDS = "number,title,body,labels,author,parent,milestone"
SEARCH_LIMIT = 200
# The structural child-body headings (`orbi add`'s own template uses the
# same English section names, so a child ticket reads like a filed Issue)
# and the origin line every child carries.
OUTCOME_HEADING = "## Outcome"
ACCEPTANCE_HEADING = "## Acceptance"
ORIGIN_LINE = "Split from #{parent}"


def child_marker(parent: int, item: int) -> str:
    """The hidden marker of one child ticket."""
    return f"<!-- {CHILD_MARKER}:{parent}:{item} -->"


@dataclass(frozen=True)
class SplitChild:
    """The machine fields a rendered child body carries."""

    parent: int
    item: int
    outcome: str
    acceptance: tuple[str, ...]


def acceptance_lines(item: clarify.SuggestionItem) -> tuple[str, ...]:
    """The item's acceptance lines: its one line, never re-wrapped."""
    text = item.acceptance.strip()
    return (text,) if text else ()


def render_child_body(
    item: clarify.SuggestionItem, *, parent: int, item_number: int,
    parent_body: str,
) -> str:
    """The child ticket body: the item as Outcome, its acceptance, its
    origin, the parent text folded away and the hidden marker.

    The headings are structural English, like `orbi add`'s template; the
    proposal lines themselves are copied verbatim, in whatever language the
    stop comment was written in.
    """
    lines = [
        OUTCOME_HEADING,
        item.text,
        "",
        ACCEPTANCE_HEADING,
        *[f"- {line}" for line in acceptance_lines(item)],
        "",
        ORIGIN_LINE.format(parent=parent),
        "",
        "<details><summary>Parent ticket text</summary>",
        "",
        (parent_body or "").strip() or "(the parent ticket has no body)",
        "",
        "</details>",
        "",
        child_marker(parent, item_number),
    ]
    return "\n".join(lines)


def _section(lines: list[str], heading: str) -> list[str]:
    """The lines under one `## ` heading, up to the next blank line."""
    try:
        index = lines.index(heading) + 1
    except ValueError:
        return []
    found: list[str] = []
    while index < len(lines) and lines[index].strip():
        found.append(lines[index])
        index += 1
    return found


def parse_child_body(body: object) -> SplitChild | None:
    """The fields a rendered child body carries, or None when it is not one.

    The body IS the record: the marker names the origin and the two
    sections are its content, so a person editing the child cannot
    desynchronize a hidden copy of the same text.
    """
    if not isinstance(body, str):
        return None
    match = CHILD_MARKER_RE.search(body)
    if match is None:
        return None
    lines = body.splitlines()
    outcome = " ".join(
        line.strip() for line in _section(lines, OUTCOME_HEADING)
    )
    acceptance = tuple(
        line.strip()[2:].strip()
        for line in _section(lines, ACCEPTANCE_HEADING)
        if line.strip().startswith("- ")
    )
    if not outcome or not acceptance:
        # A marker without both sections is not a child this engine wrote:
        # it is never adopted, whatever it claims.
        return None
    return SplitChild(
        parent=int(match.group(1)), item=int(match.group(2)),
        outcome=outcome, acceptance=acceptance,
    )


# The App prefix gh puts on an author login. A real call against a ticket
# this runner opened reads {"is_bot": true, "login": "app/orbi-build"} from
# the issue view author field, while the SAME credential's comments and
# auth status say "orbi-build" / "orbi-build[bot]": three shapes, one
# account. The author is the adoption trust signal, so every comparison
# normalizes the shapes first (Issue #1927).
API_LOGIN_PREFIX = "app/"


def _login_key(login: str) -> str:
    """The comparable form of one GitHub account login."""
    if login.startswith(API_LOGIN_PREFIX):
        login = login[len(API_LOGIN_PREFIX):]
    return strip_bot_suffix(login)


def _runner_login() -> str:
    """The login a child must carry to be adopted by a re-run."""
    return _login_key(authenticated_github_login())


def _author_login(state: dict) -> str | None:
    author = state.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return _login_key(login) if isinstance(login, str) and login else None


def _runner_receipt(comments: list[dict], receipt: str) -> bool:
    """True when a comment THIS runner wrote carries the success receipt.

    The marker is public text on a public ticket, so anyone who can comment
    can copy it. Only the authenticated account's own comment authenticates
    a completion record — a different or unreadable author is never one, so
    a copied marker cannot make an authorized split look done, skip its
    children or release its scan label (Issue #1938).
    """
    candidates = [
        comment for comment in comments
        if isinstance(comment, dict)
        and isinstance(comment.get("body"), str)
        and receipt in comment["body"]
    ]
    if not candidates:
        return False
    runner = _step("identify this runner", _runner_login)
    return any(_author_login(comment) == runner for comment in candidates)


def _parent_number(state: dict) -> int | None:
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, dict) else None
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    return None


def _blocked_numbers(state: dict) -> set[int]:
    """The native blockedBy edges of one issue, as numbers."""
    blocked_by = state.get("blockedBy")
    nodes = blocked_by.get("nodes") if isinstance(blocked_by, dict) else None
    numbers: set[int] = set()
    for node in nodes if isinstance(nodes, list) else []:
        number = node.get("number") if isinstance(node, dict) else None
        if isinstance(number, int) and not isinstance(number, bool):
            numbers.add(number)
    return numbers


def _child_state(repo: str, number: int) -> dict:
    """One child ticket as the adoption check and the steps read it."""
    return run_step(
        "find or open the child",
        lambda: issue_view(number, CHILD_FIELDS, repo=repo),
    )


def _existing_candidates(repo: str, parent: int, item: int) -> list[dict]:
    """Every issue whose body carries this parent-and-item marker."""
    marker = child_marker(parent, item)
    return run_step(
        "find or open the child",
        lambda: list_issues(
            repo, state="all", search=f'"{marker}" in:body',
            json_fields="number,title,body,author", limit=SEARCH_LIMIT,
        ),
    )


def _find_children(
    repo: str, parent: int, items: tuple[clarify.SuggestionItem, ...],
    *, language_code: str,
) -> dict[int, dict]:
    """The existing child of every item, keyed by the item's position.

    Only an issue THIS runner authored counts: the marker is public text,
    so a copied one is never adopted. Two counted matches stop the command
    (there is no way to know which one the earlier run meant).
    """
    runner = run_step("identify this runner", _runner_login)
    found: dict[int, dict] = {}
    for item_number in range(1, len(items) + 1):
        counted = [
            issue for issue in _existing_candidates(repo, parent, item_number)
            if _author_login(issue) == runner
        ]
        if not counted:
            continue
        if len(counted) > 1:
            # The receipt lists what exists (the published contract), so the
            # reader closes the right Issue instead of searching the marker.
            numbers = ", ".join(
                f"#{number}" for number in sorted(
                    int(issue["number"]) for issue in counted
                )
            )
            raise TicketCommandError(
                "find or open the child",
                language.text(
                    language_code, "split.reason_duplicate",
                    count=len(counted), numbers=numbers,
                    marker=child_marker(parent, item_number),
                ),
            )
        found[item_number] = _child_state(repo, int(counted[0]["number"]))
    return found


def _mismatch(
    item: clarify.SuggestionItem, state: dict, *,
    parent: int, item_number: int, child_numbers: dict[int, int],
) -> str | None:
    """The first field where one existing child differs from its item."""
    child = parse_child_body(state.get("body"))
    if child is None or child.parent != parent or child.item != item_number:
        return "split marker"
    if state.get("title") != item.text or child.outcome != item.text:
        return "title"
    if child.acceptance != acceptance_lines(item):
        return "acceptance lines"
    parent_number = _parent_number(state)
    if parent_number is not None and parent_number != parent:
        return "parent"
    sibling_edges = _blocked_numbers(state) & set(child_numbers.values())
    if item.after is None:
        return "`after` dependency" if sibling_edges else None
    expected = child_numbers.get(item.after)
    # An `after` whose item has no child yet cannot be compared: the edge
    # is added by the dependency step of this run.
    if expected is not None and sibling_edges not in ({expected}, set()):
        return "`after` dependency"
    return None


def _verify_existing(
    items: tuple[clarify.SuggestionItem, ...], found: dict[int, dict], *,
    parent: int, language_code: str,
) -> None:
    """Stop before any write when one existing child no longer matches."""
    child_numbers = {
        item_number: int(state["number"]) for item_number, state in found.items()
    }
    for item_number, state in sorted(found.items()):
        field = _mismatch(
            items[item_number - 1], state, parent=parent,
            item_number=item_number, child_numbers=child_numbers,
        )
        if field is None:
            continue
        raise TicketCommandError(
            "find or open the child",
            language.text(
                language_code, "split.reason_differs",
                number=state["number"],
                marker=child_marker(parent, item_number),
                field=field, item=item_number,
            ),
        )


def _attach_sub_issue(child: int, *, parent: int, repo: str) -> None:
    """Attach one Issue as a native sub-issue of ``parent``.

    The documented `gh issue edit --add-sub-issue` spelling: the edited
    Issue is the parent and the argument is its child.
    """
    run_step("attach the child as a sub-issue", lambda: run_gh_write_command([
        "gh", "issue", "edit", str(parent), "--repo", repo,
        "--add-sub-issue", str(child),
    ]))


def _add_blocked_by(issue: int, blocker: int, *, repo: str) -> None:
    """Record one native `blockedBy` edge: `issue` waits for `blocker`.

    GitHub clears the relation's effect when the blocker closes, so there
    is no local dependency bookkeeping to keep.
    """
    run_step("add the blockedBy edge", lambda: run_gh_write_command([
        "gh", "issue", "edit", str(issue), "--repo", repo,
        "--add-blocked-by", str(blocker),
    ]))


def _create_child(
    repo: str, item: clarify.SuggestionItem, *, parent: int, item_number: int,
    parent_body: str, parent_labels: list[str], milestone: str | None,
) -> int:
    """Open one child ticket; `gh issue create` prints its URL.

    Only the two inherited labels and the Milestone are passed: the child
    is queued by its own step below, and no other parent label is its
    business.
    """
    command = [
        "gh", "issue", "create", "--repo", repo,
        "--title", item.text,
        "--body", render_child_body(
            item, parent=parent, item_number=item_number,
            parent_body=parent_body,
        ),
    ]
    if P0_LABEL in parent_labels:
        command += ["--label", P0_LABEL]
    if BUG_LABEL in parent_labels:
        command += ["--label", BUG_LABEL]
    if milestone:
        command += ["--milestone", milestone]
    url = run_step(
        "create the child ticket",
        lambda: run_command(command, timeout=30).strip(),
    )
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise TicketCommandError(
            "create the child ticket",
            f"gh returned no issue URL: {url!r}",
        ) from None


def _milestone_title(issue: dict) -> str | None:
    milestone = issue.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    return title if isinstance(title, str) and title else None


def _process_child(
    repo: str, state: dict, *, parent: int, blocker: int | None,
    dispatch_label: str,
) -> None:
    """Steps 2-4 for one child: sub-issue, dependency, queue label.

    Each step reads the child's own state first, so a re-run after a
    partial run only does what is still missing.
    """
    number = int(state["number"])
    if _parent_number(state) != parent:
        _attach_sub_issue(number, parent=parent, repo=repo)
    if blocker is not None and blocker not in _blocked_numbers(state):
        _add_blocked_by(number, blocker, repo=repo)
    if not LIFECYCLE_STATES & set(label_names(state)):
        run_step(
            "add the dispatch label",
            lambda: edit_issue(number, repo=repo, add=dispatch_label),
        )


def _split_context(
    *, repo: str, issue_number: int, comments: list[dict],
    issue: dict | None = None, suggestion: object = None, **_context: object,
) -> dict[str, object]:
    """The command's own context: the ticket and its stop proposal.

    The comment scan hands neither over (it reads the comments, not the
    Issue's relations), so the command reads both from the ticket itself —
    the CLI entry does the same, and no caller passes a placeholder.
    """
    if not isinstance(issue, dict):
        issue = issue_view(issue_number, PARENT_FIELDS, repo=repo)
    if suggestion is None:
        suggestion = clarify.stop_suggestion(issue, repo, comments=comments)
    return {"issue": issue, "suggestion": suggestion}


def _split_validate(
    argument: str, *, issue: dict, suggestion: object,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> str | None:
    """The command's own rule: a valid split proposal on a parent ticket.

    Everything the steps need is checked HERE, before any write: the
    newest trusted stop comment must carry a proposal that parses, the
    proposal must be a split, and the ticket must not itself be a native
    child (re-splitting a child is refused).
    """
    if suggestion is None:
        return language.text(language_code, "split.reason_no_suggestion")
    if isinstance(suggestion, int):
        return language.text(
            language_code, "split.reason_invalid_line", line=suggestion,
        )
    kind = suggestion.kind if isinstance(suggestion, clarify.Suggestion) else ""
    if kind != clarify.SUGGESTION_SPLIT:
        return language.text(
            language_code, "split.reason_not_split", kind=kind,
        )
    parent = _parent_number(issue)
    if parent is not None:
        return language.text(
            language_code, "split.reason_child", parent=parent,
        )
    return None


def _split_receipt(
    children: list[tuple[int, str]], language_code: str,
) -> str:
    """The ONE success receipt: every child, in proposal order."""
    lines = [
        language.text(
            language_code, "split.receipt_title", count=len(children),
        ),
        "",
    ]
    lines.extend(
        language.text(
            language_code, "split.receipt_child", number=number, title=title,
        )
        for number, title in children
    )
    lines.extend(["", language.text(language_code, "split.receipt_parent")])
    return "\n".join(lines)


def _split_apply(
    argument: str, *, repo: str, issue_number: int, command_comment: dict,
    comments: list[dict], issue: dict, suggestion: object,
    dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> None:
    """Do the seven steps, in the order the ticket depends on."""
    if not isinstance(suggestion, clarify.Suggestion):
        raise TicketCommandError(
            "find or open the child",
            language.text(language_code, "split.reason_no_suggestion"),
        )
    receipt = (
        f"<!-- {command_receipt_marker(SPLIT_COMMAND)} "
        f"comment={command_comment_key(command_comment)} -->"
    )
    if not _runner_receipt(comments, receipt):
        items = suggestion.items
        found = _find_children(
            repo, issue_number, items, language_code=language_code,
        )
        _verify_existing(
            items, found, parent=issue_number, language_code=language_code,
        )
        parent_labels = label_names(issue)
        parent_body = issue.get("body") or ""
        milestone = _milestone_title(issue)
        child_numbers: dict[int, int] = {
            item_number: int(state["number"])
            for item_number, state in found.items()
        }
        children: list[tuple[int, str]] = []
        for item_number, item in enumerate(items, start=1):
            state = found.get(item_number)
            if state is None:
                number = _create_child(
                    repo, item, parent=issue_number, item_number=item_number,
                    parent_body=parent_body, parent_labels=parent_labels,
                    milestone=milestone,
                )
                state = _child_state(repo, number)
                child_numbers[item_number] = number
            blocker = None
            if item.after is not None:
                blocker = child_numbers.get(item.after)
                if blocker is None:
                    raise TicketCommandError(
                        "add the blockedBy edge",
                        f"item {item_number} depends on item {item.after}, "
                        "which has no child ticket",
                    )
            _process_child(
                repo, state, parent=issue_number, blocker=blocker,
                dispatch_label=dispatch_label,
            )
            children.append((int(state["number"]), item.text))
        add = None if EPIC_LABEL in parent_labels else EPIC_LABEL
        remove = dispatch_label if dispatch_label in parent_labels else None
        if add is not None or remove is not None:
            run_step(
                "mark the parent ai-epic",
                lambda: edit_issue(
                    issue_number, repo=repo, add=add, remove=remove,
                ),
            )
        comment_issue(
            issue_number, repo=repo,
            body=f"{_split_receipt(children, language_code)}\n\n{receipt}",
        )
    # The scan label is the LAST step: while it is there the next tick sees
    # the command again and finishes whatever is still missing. A failure
    # here is not wrapped in a step receipt — the split itself succeeded,
    # and a "failed" receipt beside the success would read as a second run.
    if NEEDS_DETAIL_LABEL in label_names(issue):
        edit_issue(issue_number, repo=repo, remove=NEEDS_DETAIL_LABEL)


SPLIT_COMMAND = CommandSpec(
    name="split",
    subcommand="split",
    arity=0,
    permission="write",
    validate=_split_validate,
    apply=_split_apply,
    usage="/orbi split",
    description=(
        "turn the split proposal on this ticket's stop comment into child "
        "tickets"
    ),
    context=_split_context,
)

register_command(SPLIT_COMMAND)
