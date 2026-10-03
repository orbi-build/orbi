# Fidelity pass

Independent check of one drafted Issue against the request it came from. Run
it after the draft exists and before the draft is shown to the person, as a
separate call with no tools and no access to the drafting conversation: its
input is only the person's request and the complete draft. In a harness with
sub-agents, invoke a sub-agent with this file and those two inputs. Otherwise
make it the next turn with no repository tools and no previous messages.

Input:

1. the person's request, verbatim;
2. the complete draft.

Return the complete revised draft, never a diff or a list of comments.

Apply these rules in order:

1. **User outcome restates the request only.** If it names an effect, a
   benefit or a behaviour the person did not ask for, remove that. Keep it
   observable.
2. **Acceptance keeps the requested behaviour and at most one failure path of
   it.** Keep the items that describe exactly what was asked, plus at most the
   single failure path of that behaviour. Move every other item, one line
   each, to `Not included (suggestions)`. Do not delete an item silently and
   do not invent a replacement.
3. **A bigger reading of an ambiguity is replaced by the smallest reading
   plus one question.** If the draft resolved an ambiguous request towards the
   larger interpretation, rewrite it to the smallest reading and add the
   question that would settle it.
4. **Every other section is kept as is** — `Request`, `Current state`,
   `Preconditions`, `Evidence`, `Out of scope`, `Assumptions`, `Decisions`,
   the named commit and the section order are not changed.

When the draft already satisfies all four rules, return it unchanged.
