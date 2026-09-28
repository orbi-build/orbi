---
name: orbi-regression-guard
description: Use on every delivery or review of a change to existing behavior (bug fix, new rule, changed format). Finds inputs the change makes worse than the base and checks the target repository's contribution rules before commit or verdict.
---

# Regression guard

The Issue's example is one input, not the specification of the code path. A
change must leave every input it was not asked to change exactly as it was and
must not make any input worse than the base.

## 1. List the input classes

List what the changed code path really receives: every caller, producer, rule
or data source that reaches it, not only the one the Issue names. For each
class, add its neighbours:

| Class | Samples to try |
|---|---|
| Size | empty, one element, very short values, very long values |
| Syntax look-alikes | escapes, backslashes (even and odd runs), delimiters, quotes, placeholders |
| Measured text | CJK and fullwidth, combining marks, emoji modifiers, multi-code-point sequences, ANSI codes |
| Counts | zero / one / many, even / odd |
| Other producers | every other rule, caller or format that reaches the same code |

## 2. Probe base against head

Write a throwaway probe (script or temporary test) and run it on the base
(`git show <base>:<file>` or a base worktree) and on the change, with a
concrete sample of each class the change could treat differently. Put the
samples and both outputs in the delivery or review notes.

- An input the change handles worse than the base is a regression: fix it
  (implementer) or report it as a Major finding (reviewer; a Blocker when the
  code guards secrets, security or data integrity).
- "Out of scope" means "must behave as before", shown by a probe, never
  "unchecked".
- Claims about library, Unicode, regex or encoding behavior are verified by
  running code, never from memory.
- Keep a regression test for every class whose behavior must not change.

## 3. Contribution rules

Before the first commit (implementer) and before the verdict (reviewer), read
the target repository's `CONTRIBUTING.md`, `AI_POLICY.md` or equivalent, and
its changelog convention. Follow the file-level rules they state, for example
adding a changelog fragment instead of editing a generated changelog. A
violation is a Major finding.
