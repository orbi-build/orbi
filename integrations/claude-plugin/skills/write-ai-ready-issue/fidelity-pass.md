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
   do not invent a replacement. An item that only repeats an added constraint
   is removed by rule 3, not parked here.
3. **An added constraint is removed everywhere, with the places that depend on
   it.** An added constraint is a requirement the person did not make — most
   often a repository observation restated as an outcome, such as how many
   lines the file has today, which line the new one becomes, or a total the
   person never named. Remove it from every section that states a
   requirement — `User outcome`, `Preconditions`, `Acceptance`, `Evidence`,
   `Assumptions` and `Decisions` — so no section is left out, and reword or
   remove each place that repeats or checks it (an evidence step verifying
   the invented total, an assumption stating it, a decision phrased with
   it), so the draft reads as one consistent specification. Keep the
   person's `Request` verbatim, and keep unrelated grounded content — a
   `Current state` fact, an unrelated assumption or decision, and the effect
   the person did ask for all stay. When an item mixes the requested effect
   with an invented total, keep the requested effect and drop only the
   total. An added constraint is nobody's suggestion: it is not moved to
   `Not included (suggestions)`.
4. **A quantity the person stated stays.** When the request itself says
   "exactly 17 lines", that total is a requirement and rule 3 leaves it
   alone. Numbers are not the problem, and this pass removes no number the
   request carries: only constraints the request does not carry are added
   constraints.
5. **A bigger reading of an ambiguity is replaced by the smallest reading,
   recorded under `Assumptions`.** If the draft resolved an ambiguous request
   towards the larger interpretation, rewrite it to the smallest reading and
   add one line to `Assumptions` naming the ambiguity and the reading chosen.
   Never add a question to the draft: the draft is the future Issue body, and
   the question that would settle it belongs in the reply to the person.
6. **Every remaining section is kept as is** — `Request`, `Current state`,
   `Preconditions`, `Evidence`, `Out of scope`, `Assumptions`,
   `Decisions`, the named commit and the section order are not changed,
   except for the removals rule 3 makes.

When the draft already satisfies every rule above, return it unchanged.
