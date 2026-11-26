#!/usr/bin/env python3
"""The drafting session's prompt text (Issues #2041, #2364).

The drafting rules, the fidelity protocol, the rewrite instruction and the
three tool names the shipped draft_tools extension registers live here so
orbi.draft keeps being one readable engine: its recorded size may only
shrink (tools/size_ratchet.py, Issue #2167), the same split that produced
orbi.draft_document.

The rules are the engine's own: every drafted Issue is judged by the
pre-work check the claim runs, so the prompt teaches exactly that check's
criteria and the drafting decisions that make a draft pass it the first
time.
"""
from __future__ import annotations

from pathlib import Path

from orbi import clarify
from orbi.question import RECOMMENDED_MARK

# The three tools the shipped extension registers; the prompt names them and
# the drafting session's tool allowlist must carry them (one spelling, two
# places).
WRITE_DRAFT_TOOL = "write_draft"
ASK_USER_TOOL = "ask_user"
# Issue #2201: the answer to a request that must not change any code.
REPLY_TOOL = "reply"

# The shipped fidelity rules; the pass runs them with the engine's protocol.
FIDELITY_REL = Path(
    "integrations/claude-plugin/skills/write-ai-ready-issue/fidelity-pass.md"
)

# The one rule that replaced the skill's "draft only the first slice": the
# check reports single_outcome for a request that bundles independent
# deliverables, so the drafter splits it BEFORE the check ever sees it.
SINGLE_OUTCOME_RULE = (
    "A request that fails the single_outcome check becomes SEVERAL Issues "
    "in one draft: one Issue per outcome, in delivery order, never one "
    "Issue whose acceptance list covers independent deliverables. A draft "
    "may therefore hold one Issue or many."
)

# One rule holds for the whole draft, whichever step writes the body
# (Issue #2433): every section heading is in the person's language. The
# drafter got it from "write in the person's language" alone, while the
# fidelity pass that rewrites the body afterwards named the two sections it
# adds in English -- a Chinese draft came back with Chinese headings and
# `## Not included (suggestions)` / `## Assumptions`. The same sentence is
# stated to both sessions, and in the shipped skill and its fidelity rules,
# so the later step cannot override the earlier one.
HEADING_LANGUAGE_RULE = (
    "A section heading is written in the person's language, the same "
    "language as the body around it: one body never mixes a heading in one "
    "language with a heading in another. The English section names "
    "(`Not included (suggestions)`, `Assumptions` and the rest) name a "
    "section; they are never the heading text to copy into a body written "
    "in another language."
)

# The one thing a draft may classify, and the closed set of names it may
# classify with (Issue #2445). The set itself lives in the label table
# (`draft_document.matched_labels`, which the parser enforces); the prompt
# states it in prose because the model reads sentences. So the model
# decides WHETHER the request is a defect and whether it is urgent, never
# which label to invent.
MATCHED_LABELS_RULE = (
    "Classify each Issue from the request itself, with labels, and only "
    "these two: \"bug\" when the request describes behaviour that is supposed "
    "to work today and does not (it worked before, or the repository's own "
    "rule says it must), and \"p0\" only when the request says the problem is "
    "urgent or blocking (an outage, data loss, every delivery stopped). A "
    "plain new feature carries neither. When the request does not clearly "
    "say the problem is urgent, leave \"p0\" out: never guess, and never "
    "add the label because a request sounds serious. Put the matched names "
    "in that Issue's \"labels\" field, a list of names; omit the field when "
    "nothing matches. Any other label name is refused."
)

# The drafting rules are the engine's own (Issues #2041, #2147).
DRAFTING_RULES = (
    "Rules for every draft:\n"
    "- Write in the person's language: the Issue titles, the Issue bodies, "
    "your reply and your questions.\n"
    "- " + HEADING_LANGUAGE_RULE + "\n"
    "- A request that explicitly asks for NO code change (\"just review it "
    "and tell me\", \"only explain\", \"don't touch the code\") takes the "
    "no-repository-change route: call " + REPLY_TOOL + " and return a reply "
    "alone, never a draft. In the person's language, list in one line each "
    "what your reading found worth fixing and say that Orbi delivers by "
    "opening a pull request, so the person can name one item for Orbi to "
    "fix.\n"
    "- The nearest-deliverable rule applies only to a request that asks "
    "for a change: when the code cannot deliver such a request as asked, "
    "draft the nearest version it can deliver and record the change under "
    "Assumptions. A request not to change any code is never such a "
    "draft.\n"
    "- Refer to files by their repository-relative path.\n"
    "- Do not write the commit SHA.\n"
    "- Open a draft's reply with one plain-language sentence of what "
    "will change; an answer with no code change opens with the first thing "
    "worth fixing.\n"
    "- An Assumptions section appears only when you took at least one "
    "decision: one line per decision you took instead of asking, saying "
    "what you chose and why. With no decision taken, the Issue body "
    "carries no Assumptions section.\n"
    "- Ask a question only when, after reading the repository, the request "
    "itself still does not say what to build — it is empty or contradicts "
    "itself — and no deliverable default version can be written. Then ask "
    "at most three questions in one call. A question is one sentence in "
    "the person's language, with an OPTIONAL header of one to three words "
    "naming the decision, and OPTIONAL 2 to 4 options when the person has "
    "to pick: every option a label of one to five words plus one sentence "
    "of what it means or its trade-off. When one option is the reasonable "
    "default, its label ends with \"" + RECOMMENDED_MARK + "\", and that "
    "marker is never translated: it is the engine's, not the person's. "
    "Never add an \"Other\" option and never say the person chose the "
    "recommended option: the person always answers in their own words.\n"
    "- Every other disagreement — which library, font or place to use, "
    "what to leave out, how to word a deliverable version of a change "
    "request that cannot be done as asked — is a decision: take the most "
    "reasonable default and record it under Assumptions instead of "
    "asking.\n"
    "- Do not repeat the draft or the questions in the reply.\n"
    "- Never tell the person to create the Issue themselves, and never "
    "give a new-Issue link.\n"
    "- Each Issue body carries its own outcome and its own acceptance "
    "condition, so the pre-work check accepts it as it stands.\n"
    "- When the person attached files (the \"The person attached\" line "
    "lists them as 1.png = screen.png, 2.html = spec.html), give each "
    "Issue an \"attachments\" field: the 1-based numbers of the files "
    "that Issue uses. Omit the field when the Issue uses all of them, "
    "and use an empty list when it uses none.\n"
    "- The after field is the 1-based number of an EARLIER Issue in this "
    "same draft that this one waits for; omit it when nothing comes "
    "first.\n"
    "- " + MATCHED_LABELS_RULE
)

FIDELITY_PROTOCOL = (
    "Input: one JSON object {\"request\": <the person's words verbatim>, "
    "\"issues\": [{\"title\", \"body\", \"after\", \"attachments\", "
    "\"labels\"}]}.\n"
    "Return ONLY a JSON object {\"issues\": [{\"title\", \"body\", "
    "\"after\", \"attachments\", \"labels\"}]} holding the complete "
    "revised draft, "
    "never a diff and never a comment. Keep the same Issues the input has, "
    "and keep each Issue's \"attachments\" numbers and \"labels\" unchanged: "
    "this pass "
    "removes what the person did not ask for, it never drops an outcome, "
    "a file the Issue uses or a label the request matched.\n"
    + HEADING_LANGUAGE_RULE
)

REWRITE_INSTRUCTION = (
    "The pre-work check judged the current draft and these checks failed: "
    "{reasons}. Rewrite the WHOLE draft so every Issue passes them, keep "
    "exactly what the request asks for, and call write_draft again."
)

def draft_system_prompt() -> str:
    """The drafting prompt: the check's OWN criteria plus the draft rules."""
    return "\n\n".join((
        "You are Orbi's drafting session: read-only, no shell. Turn one "
        "request into GitHub Issues that Orbi's pre-work check accepts. "
        "Answer only by calling exactly one tool: " + WRITE_DRAFT_TOOL
        + " when you have a draft, " + ASK_USER_TOOL + " when you need "
        "the person to answer something first, " + REPLY_TOOL + " when "
        "the person asked for no code change and the answer is a reply "
        "alone. Never more than one in a turn.",
        clarify.TICKET_CHECK_CRITERIA,
        SINGLE_OUTCOME_RULE,
        DRAFTING_RULES,
    ))
