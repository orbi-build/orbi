"""The child tickets one split proposal becomes (Issue #1927, #2207).

A `kind=split` proposal — the one the clarification gate attaches to its stop
comment (Issue #1926) — becomes child tickets through the steps this module
owns, in the order the ticket depends on:

1. find or open every child;
2. attach it as a native sub-issue of the parent;
3. add its `blockedBy` edge to the earlier item's child;
4. add the dispatch label to a child that never had a delivery label;
5. mark the parent `ai-epic` and release its dispatch label.

Every step reads the current GitHub state first, so an interrupted apply is
finished by the next one: a child is adopted only when THIS runner authored it,
its hidden marker names this parent and item, and its title, acceptance lines and
dependency still equal the item **and the proposal as it reads now**. Two
implementations ride these steps and must never drift apart:

- `/orbi split` (Issue #1927) applies the proposal of a stop comment a person
  replied to, and posts its own receipt keyed on that command comment;
- the clarification gate applies the proposal it just judged, in the same claim
  (Issue #2207), and posts the same receipt listing the children.

So the receipt TEXT lives here too (`split_receipt`) while its idempotency key
stays with each caller: a command receipt is keyed on the command comment, and
the gate has no comment at all.

Issue #2207 moved the engine out of `split_command`, which sits at its recorded
size ceiling (`tools/size_ratchet.py`), and this module is the reason both
callers can share it: it imports no gate module, so no import cycle exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from orbi import clarify_proposal, language
from orbi.delivery_labels import (
    BUG_LABEL,
    EPIC_LABEL,
    LIFECYCLE_STATES,
    P0_LABEL,
    label_names,
)
from orbi.github import (
    edit_issue,
    issue_view,
    list_issues,
    run_gh_write_command,
)
from orbi.journal import run_command
from orbi.ticket_command import (
    TicketCommandError,
    author_login_key,
    run_step,
    runner_login,
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


def acceptance_lines(item: clarify_proposal.SuggestionItem) -> tuple[str, ...]:
    """The item's acceptance lines: its one line, never re-wrapped."""
    text = item.acceptance.strip()
    return (text,) if text else ()


def render_child_body(
    item: clarify_proposal.SuggestionItem, *, parent: int, item_number: int,
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


def parent_number(state: dict) -> int | None:
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, dict) else None
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    return None


def blocked_numbers(state: dict) -> set[int]:
    """The native blockedBy edges of one issue, as numbers."""
    blocked_by = state.get("blockedBy")
    nodes = blocked_by.get("nodes") if isinstance(blocked_by, dict) else None
    numbers: set[int] = set()
    for node in nodes if isinstance(nodes, list) else []:
        number = node.get("number") if isinstance(node, dict) else None
        if isinstance(number, int) and not isinstance(number, bool):
            numbers.add(number)
    return numbers


def child_state(repo: str, number: int) -> dict:
    """One child ticket as the adoption check and the steps read it."""
    return run_step(
        "find or open the child",
        lambda: issue_view(number, CHILD_FIELDS, repo=repo),
    )


def _existing_candidates(repo: str, parent: int) -> list[dict]:
    """Every issue whose body carries a child marker naming this parent.

    The phrase is matched by its TOKENS (GitHub drops the punctuation), so
    it finds the marker of EVERY item of this parent — including one the
    proposal no longer has — and no other parent's, whose marker carries a
    different number token. One query replaces one search per item.
    """
    return run_step(
        "find or open the child",
        lambda: list_issues(
            repo, state="all", search=f'"{CHILD_MARKER}:{parent}" in:body',
            json_fields="number,title,body,author", limit=SEARCH_LIMIT,
        ),
    )


def claimed_item(body: object, parent: int) -> int | None:
    """The proposal item this body's child marker names for this parent."""
    if not isinstance(body, str):
        return None
    for claimed_parent, item in CHILD_MARKER_RE.findall(body):
        if int(claimed_parent) == parent:
            return int(item)
    return None


def find_children(
    repo: str, parent: int, *, language_code: str,
) -> dict[int, dict]:
    """The existing child of every item marker written for this parent.

    The item number comes from the marker, not from the current proposal, so
    a child of an item that a later edit removed is found too (it is
    verified against the proposal and stops the command). Only an issue THIS
    runner authored counts: the marker is public text, so a copied one is
    never adopted. Two counted matches stop the command (there is no way to
    know which one the earlier run meant).
    """
    runner = run_step("identify this runner", runner_login)
    claimed: dict[int, list[dict]] = {}
    for issue in _existing_candidates(repo, parent):
        if author_login_key(issue) != runner:
            continue
        item_number = claimed_item(issue.get("body"), parent)
        if item_number is None:
            continue
        claimed.setdefault(item_number, []).append(issue)
    found: dict[int, dict] = {}
    for item_number, counted in sorted(claimed.items()):
        if len(counted) > 1:
            # The receipt lists what exists (the published contract), so the
            # reader picks the intended Issue instead of searching the marker.
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
        found[item_number] = child_state(repo, int(counted[0]["number"]))
    return found


def _mismatch(
    item: clarify_proposal.SuggestionItem, state: dict, *,
    parent: int, item_number: int,
) -> str | None:
    """The first field where one existing child differs from its item.

    The dependency is compared separately: a missing expected edge is a
    recoverable interrupted step, while an unexpected one is a conflict
    that names its blocker.
    """
    child = parse_child_body(state.get("body"))
    if child is None or child.parent != parent or child.item != item_number:
        return "split marker"
    if state.get("title") != item.text or child.outcome != item.text:
        return "title"
    if child.acceptance != acceptance_lines(item):
        return "acceptance lines"
    attached_to = parent_number(state)
    if attached_to is not None and attached_to != parent:
        return "parent"
    return None


def _expected_blocker(
    item: clarify_proposal.SuggestionItem, child_numbers: dict[int, int],
) -> set[int]:
    """The one sibling edge the proposal asks this item's child to carry.

    An item with no ``after`` asks for none. An ``after`` whose item has no
    child yet cannot be compared: the edge is added by the dependency step
    of this run (a missing edge is a recoverable interrupted step).
    """
    if item.after is None:
        return set()
    expected = child_numbers.get(item.after)
    return set() if expected is None else {expected}


def verify_existing(
    items: tuple[clarify_proposal.SuggestionItem, ...], found: dict[int, dict], *,
    parent: int, language_code: str,
) -> None:
    """Stop before any write when one existing child no longer matches.

    Every child found for this parent is compared with the WHOLE proposal:
    a marker for an item the proposal no longer has, and a native dependency
    the proposal does not ask for, are conflicts like an edited title.
    """
    child_numbers = {
        item_number: int(state["number"]) for item_number, state in found.items()
    }
    for item_number, state in sorted(found.items()):
        marker = child_marker(parent, item_number)
        if not 1 <= item_number <= len(items):
            raise TicketCommandError(
                "find or open the child",
                language.text(
                    language_code, "split.reason_extra_child",
                    number=state["number"], marker=marker,
                    item=item_number, count=len(items),
                ),
            )
        item = items[item_number - 1]
        field = _mismatch(
            item, state, parent=parent, item_number=item_number,
        )
        if field is not None:
            raise TicketCommandError(
                "find or open the child",
                language.text(
                    language_code, "split.reason_differs",
                    number=state["number"], marker=marker,
                    field=field, item=item_number,
                ),
            )
        extra = blocked_numbers(state) - _expected_blocker(
            item, child_numbers,
        )
        if extra:
            raise TicketCommandError(
                "find or open the child",
                language.text(
                    language_code, "split.reason_extra_dependency",
                    number=state["number"], marker=marker,
                    item=item_number,
                    blockers=", ".join(f"#{number}" for number in sorted(extra)),
                ),
            )


def attach_sub_issue(child: int, *, parent: int, repo: str) -> None:
    """Attach one Issue as a native sub-issue of ``parent``.

    The documented `gh issue edit --add-sub-issue` spelling: the edited
    Issue is the parent and the argument is its child.
    """
    run_step("attach the child as a sub-issue", lambda: run_gh_write_command([
        "gh", "issue", "edit", str(parent), "--repo", repo,
        "--add-sub-issue", str(child),
    ]))


def add_blocked_by(issue: int, blocker: int, *, repo: str) -> None:
    """Record one native `blockedBy` edge: `issue` waits for `blocker`.

    GitHub clears the relation's effect when the blocker closes, so there
    is no local dependency bookkeeping to keep.
    """
    run_step("add the blockedBy edge", lambda: run_gh_write_command([
        "gh", "issue", "edit", str(issue), "--repo", repo,
        "--add-blocked-by", str(blocker),
    ]))


def create_ticket(
    repo: str, *, title: str, body: str, labels: list[str] | None = None,
    milestone: str | None = None, step: str = "create the child ticket",
) -> int:
    """Open one ticket; `gh issue create` prints its URL.

    The ONE creation leaf both the split command and `orbi draft` use, so
    the "open it WITHOUT the dispatch label" rule has one implementation:
    the caller queues the ticket with its own later step.

    Only the labels the caller names and the Milestone are passed; the
    dispatch label is never one of them.
    """
    command = [
        "gh", "issue", "create", "--repo", repo,
        "--title", title, "--body", body,
    ]
    for label in labels or []:
        command += ["--label", label]
    if milestone:
        command += ["--milestone", milestone]
    url = run_step(
        step, lambda: run_command(command, timeout=30).strip(),
    )
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise TicketCommandError(
            step, f"gh returned no issue URL: {url!r}",
        ) from None


def create_child(
    repo: str, item: clarify_proposal.SuggestionItem, *, parent: int, item_number: int,
    parent_body: str, parent_labels: list[str], milestone: str | None,
) -> int:
    """Open one child ticket; the two inherited labels and the Milestone
    travel with it, and the child is queued by its own step below."""
    return create_ticket(
        repo, title=item.text,
        body=render_child_body(
            item, parent=parent, item_number=item_number,
            parent_body=parent_body,
        ),
        labels=[
            label for label in (P0_LABEL, BUG_LABEL)
            if label in parent_labels
        ],
        milestone=milestone,
    )


def milestone_title(issue: dict) -> str | None:
    milestone = issue.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    return title if isinstance(title, str) and title else None


def process_child(
    repo: str, state: dict, *, parent: int | None, blocker: int | None,
    dispatch_label: str,
) -> None:
    """Steps 2-4 for one child: sub-issue, dependency, queue label.

    Each step reads the child's own state first, so a re-run after a
    partial run only does what is still missing. `parent` is None for a
    ticket that is nobody's child (`orbi draft` creates them standalone):
    the sub-issue step is then the one step that does not apply.
    """
    number = int(state["number"])
    if parent is not None and parent_number(state) != parent:
        attach_sub_issue(number, parent=parent, repo=repo)
    if blocker is not None and blocker not in blocked_numbers(state):
        add_blocked_by(number, blocker, repo=repo)
    if not LIFECYCLE_STATES & set(label_names(state)):
        run_step(
            "add the dispatch label",
            lambda: edit_issue(number, repo=repo, add=dispatch_label),
        )


def apply_proposal(
    items: tuple[clarify_proposal.SuggestionItem, ...], *, repo: str,
    issue: dict, dispatch_label: str,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> list[tuple[int, str]]:
    """Steps 1-5 over one proposal, from what is already on GitHub.

    Returns the children in proposal order — the receipt's content. Every step
    is idempotent over GitHub state, so this is the ONE implementation both the
    command and the gate's automatic apply ride: whichever runs second finishes
    what the other left (Issue #2207). The parent is left `ai-epic` without the
    dispatch label, which is what keeps it out of the claim queue.
    """
    parent = int(issue["number"])
    found = find_children(repo, parent, language_code=language_code)
    verify_existing(items, found, parent=parent, language_code=language_code)
    parent_labels = label_names(issue)
    parent_body = issue.get("body") or ""
    milestone = milestone_title(issue)
    child_numbers: dict[int, int] = {
        item_number: int(state["number"])
        for item_number, state in found.items()
    }
    children: list[tuple[int, str]] = []
    for item_number, item in enumerate(items, start=1):
        state = found.get(item_number)
        if state is None:
            number = create_child(
                repo, item, parent=parent, item_number=item_number,
                parent_body=parent_body, parent_labels=parent_labels,
                milestone=milestone,
            )
            state = child_state(repo, number)
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
        process_child(
            repo, state, parent=parent, blocker=blocker,
            dispatch_label=dispatch_label,
        )
        children.append((int(state["number"]), item.text))
    add = None if EPIC_LABEL in parent_labels else EPIC_LABEL
    remove = dispatch_label if dispatch_label in parent_labels else None
    if add is not None or remove is not None:
        run_step(
            "mark the parent ai-epic",
            lambda: edit_issue(parent, repo=repo, add=add, remove=remove),
        )
    return children


def split_receipt(
    children: list[tuple[int, str]], language_code: str,
) -> str:
    """The ONE receipt: every child, in proposal order.

    Both callers post this text as their success receipt; only the hidden marker
    under it differs (a command keys it on the comment a person replied with).
    """
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
