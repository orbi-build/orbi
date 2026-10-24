"""The machine-readable review verdict protocol (Issue #1644).

The reviewer session ends with one `REVIEW_VERDICT {json}` payload; this
module owns the ONE parser for it and the one predicate the merge gate
reads. `review_merge` imports both for the review loop; the parser
touches no file, no process and no GitHub state — it is a pure function
of the session output, so the protocol is reviewed and tested on its own.

The move out of `review_merge` changed no behaviour: the accepted shapes,
the rejections and their `ValueError` texts are byte for byte the same
(Constitution Article 3.2: an extraction changes no behaviour).
"""
import json

# Machine-readable verdict line the reviewer session must end with.
VERDICT_MARKER = "REVIEW_VERDICT"


def _is_code_fence_line(line: str) -> bool:
    """True when a stripped line is only a Markdown code fence.

    Reviewers commonly wrap the machine-readable verdict in a fence
    (```` ``` ````, ```` ```json ```` or `~~~`); a fence line carries no
    review content, so the tail scan skips it without relaxing the verdict checks.
    """
    stripped = line.strip()
    for fence_char in ("`", "~"):
        if stripped.startswith(fence_char * 3):
            remainder = stripped.lstrip(fence_char)
            if fence_char == "`":
                # A backtick fence's info string must not contain backticks.
                return "`" not in remainder
            return True
    return False


def _json_dict_span(segment: str) -> dict | None:
    """The `{...}` dict embedded in a text segment, or None.

    Wrapper noise around a verdict payload — a leading text
    prefix, Markdown inline-code backticks, trailing CJK/Western
    punctuation — all sit OUTSIDE the braces, so the first-`{`…last-`}`
    span isolates the JSON. A segment whose braces are reversed or whose
    span does not parse (code snippets, prose examples) returns None.
    """
    start = segment.find("{")
    end = segment.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        parsed = json.loads(segment[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _validated_verdict(parsed: dict) -> dict:
    """The semantic checks every verdict payload must pass."""
    if parsed.get("verdict") not in (
        "pass", "findings", "blocked_on_human_decision",
    ):
        raise ValueError(
            "verdict must be 'pass', 'findings' or "
            "'blocked_on_human_decision'"
        )
    head = parsed.get("head")
    if not isinstance(head, str) or not head:
        raise ValueError("head must be the reviewed commit SHA")
    for key in ("blockers", "majors", "minors"):
        value = parsed.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not isinstance(parsed.get("findings", []), list):
        raise ValueError("findings must be a list")
    blockers = parsed["blockers"]
    majors = parsed["majors"]
    if parsed["verdict"] == "pass" and (blockers > 0 or majors > 0):
        raise ValueError("pass verdict cannot have blockers or majors")
    verdict = parsed["verdict"]
    if verdict in ("findings", "blocked_on_human_decision") \
            and blockers == 0 and majors == 0:
        raise ValueError(f"{verdict} verdict requires blockers or majors")
    if verdict == "blocked_on_human_decision":
        findings = parsed["findings"]
        if blockers != 0 or majors != 1 or len(findings) != 1:
            raise ValueError(
                "blocked_on_human_decision verdict requires exactly one "
                "Major finding and no blockers"
            )
        finding = findings[0]
        if (not isinstance(finding, dict)
                or finding.get("level") != "Major"
                or not isinstance(finding.get("note"), str)
                or not finding["note"].strip()
                or not isinstance(finding.get("fix"), str)
                or not finding["fix"].strip()):
            raise ValueError(
                "blocked_on_human_decision finding must include a non-empty "
                "Major note and fix"
            )
    return parsed


def parse_review_verdict(text: str) -> dict:
    """Extract the REVIEW_VERDICT JSON from a review session's output.

    The output is scanned BACKWARDS: the verdict may be the
    last line, wrapped in the reviewer's natural-language phrasing
    (leading CJK prefix, inline-code backticks, trailing punctuation —
    the orbi-cloud#287 scene), or followed by trailing prose. The
    semantics do not relax with the shape: a line that only
    MENTIONS `REVIEW_VERDICT` without starting it (a quote from the
    Issue body, a diff hunk, an echo) is never adopted, a verdict-shaped
    but invalid JSON blob in prose is never adopted, every payload must
    pass the full semantic validation, and the verdict must still name
    the head it covers (`head`); the merge gate checks it against the PR
    head. A `REVIEW_VERDICT`-marked line is the explicit verdict channel:
    a malformed or semantically invalid payload there fails fast instead
    of being skipped. Two DIFFERENT verdicts in one output are ambiguous
    and fail without picking one; identical duplicates agree and are
    accepted. Missing or malformed verdicts fail fast; a review that
    cannot be read as a pass is never treated as a pass.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    while lines and _is_code_fence_line(lines[-1]):
        lines.pop()
    candidates = []
    last_index = len(lines) - 1
    for index in range(last_index, -1, -1):
        stripped = lines[index].strip()
        marked = stripped.startswith(VERDICT_MARKER)
        if not marked and VERDICT_MARKER in stripped:
            continue  # a marker mention, never a verdict
        payload = stripped[len(VERDICT_MARKER):] if marked else stripped
        parsed = _json_dict_span(payload)
        if marked:
            # The explicit verdict channel: a malformed payload fails
            # fast, it is never silently skipped.
            if parsed is None:
                raise ValueError("malformed REVIEW_VERDICT JSON")
            candidates.append(_validated_verdict(parsed))
        elif parsed is not None:
            # Issue #837: an unmarked verdict-shaped JSON is adoptable only
            # from the LAST non-empty line embedded in natural-language
            # phrasing (the orbi-cloud#287 scene). Anywhere else — a
            # mid-body quote, a fenced display block, a bare payload line —
            # it is quotation, never the reviewer's own conclusion: it
            # enters no candidate pool, so it can neither be adopted nor
            # kill the real verdict by conflict.
            if index != last_index:
                continue
            if stripped.startswith("{") and stripped.endswith("}"):
                continue  # a bare payload line, not phrased prose
            try:
                candidates.append(_validated_verdict(parsed))
            except ValueError:
                continue  # verdict-shaped prose, not the channel
    if not candidates:
        raise ValueError("no REVIEW_VERDICT line in review output")
    if len({json.dumps(v, sort_keys=True) for v in candidates}) > 1:
        raise ValueError("conflicting REVIEW_VERDICT verdicts in review "
                         "output")
    return candidates[0]


def review_has_findings(verdict: dict) -> bool:
    """True when a verdict still blocks the merge gate (Blocker or Major)."""
    return verdict["blockers"] > 0 or verdict["majors"] > 0
