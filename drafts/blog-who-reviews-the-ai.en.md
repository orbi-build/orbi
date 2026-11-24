# AI now approves pull requests. Who reviews the AI?

> Blog draft · for small teams · companion page: https://docs.orbi.build/trust

This month, "AI reviews your PRs, fixes them, and approves the ones your policy allows" became a standard pitch for AI coding tools. We're glad. It means the market has accepted an idea that sounded radical two years ago: **code delivery can be done end to end by AI.**

It also puts a question in front of every team. If AI writes the code, reviews it and approves the merge, what exactly are you trusting when something goes wrong?

We've been answering that question since Orbi's first day. Here is our answer, and the less glamorous design choices behind it.

## We don't ask you to trust the model

Models change. The next release from the same vendor may be stronger overall and weaker at exactly the thing you care about. A delivery system whose safety rests on "the model got it right this time" ties its reliability to someone else's release schedule.

So Orbi splits the roles:

- **The model does the work**: writing, testing, reviewing, fixing.
- **The Runner holds the line**: a small, deterministic program decides what may merge. The review verdict is bound to the exact commit it reviewed, CI must be green on that commit, and the merge is refused if the head changes.
- **GitHub enforces the boundary**: branch protection and rulesets are the final layer. Orbi works with them, not instead of them.

The model can be wrong. The gates don't open because of it.

## An AI approving itself is the wrong default

"AI reviews AI" sounds like a double check. But if the same model writes and reviews, they share the same training, the same habits and the same blind spots. It's a student grading their own homework.

In Orbi the reviewer is always a separate session, and it can run on **a different model from a different provider** with two lines of config. We're model-agnostic not because we dislike any vendor, but because **independent review requires independence**. A system that can only review a vendor's model with the same vendor's model can't provide that.

## The ledger is your repository

Many tools give you a polished dashboard. We don't, because **you shouldn't have to trust a vendor's dashboard to know what happened in your own repository.**

Orbi has no database. GitHub Issues, labels, comments and PRs are the whole delivery record. Each run has a `run_id` that threads through the Issue comments, the PR and the logs. The round the reviewer rejected, the fix commits, the moment it merged: all of it is in your repository, readable with tools you already use.

A side effect: **you can't get locked in.** Stop using Orbi and there's nothing to export, because we never held anything.

## Stopping beats guessing

A confident wrong merge is much worse than an honest "I'm stuck", especially for a small team. A large company has on-call, a platform team and staging to catch mistakes. A five-person team usually doesn't.

So Orbi's defaults lean toward stopping:

- command errors fail fast, with no silent fallback;
- a delivery it can't finish is labelled `ai-blocked` with the reason, for a person;
- review-and-fix is capped at five rounds;
- a vague ticket gets a question back, not a guess.

If you want a person to sign off on every merge, turn on the human acceptance gate. Each delivery then lists what was verified automatically and what only a person can confirm, such as business intent and UI copy, and waits for your go-ahead.

## We publish our own gaps

This may be the most important part. Orbi's docs include a [safety matrix](https://docs.orbi.build/safety-matrix): each row names a failure, the layer that catches it, and **the test that proves it**. Rename or delete that test and CI fails, so the table can't quietly drift out of date.

At the bottom is a list of known gaps, including:

- Orbi does **not** stop an agent session from pushing your protected branch by itself. The agents are told not to, but that's an instruction, not an enforcement. Your branch protection enforces it; without it, nothing in Orbi does.
- Releases don't go through the PR review gate. They have their own gates.
- Green tests aren't correct intent.

Why write these down? Because **trust is built from evidence you can check, not from promises.** Documentation that names its own weaknesses is more credible than documentation that only lists strengths. When a gap closes, it stays in the table with the fix, so the history isn't erased.

## Advice for small teams

Whether or not you choose Orbi, before you let any AI tool merge code:

1. **Protect your main branch** and require status checks. It's the last line, and the only one that truly enforces.
2. **Use a fine-grained token** scoped to the repositories and permissions actually needed.
3. **Check the review is independent**: ask whether the reviewer and the author are the same model.

## Check it yourself

We don't want you to trust Orbi because of this post. We want you to verify:

- Follow one real loop: [Issue #1367](https://github.com/orbi-build/orbi/issues/1367) → [PR #1370](https://github.com/orbi-build/orbi/pull/1370) → [Release v0.5.47](https://github.com/orbi-build/orbi/releases/tag/v0.5.47)
- Browse Orbi's own [merged PRs](https://github.com/orbi-build/orbi/pulls?q=is:merged). Orbi is built with Orbi, rejected reviews included
- Pick any row in the safety matrix and run its test
- Read the full [trust page](https://docs.orbi.build/trust)

Then try it on a throwaway repository with branch protection on: [Managed Cloud](https://orbi.build/?ref=blog-trust) is free for your first three merged deliveries, and [self-hosting](https://docs.orbi.build/quickstart) is free forever.

AI will keep writing code faster. What's scarce is a reason to let go of the wheel.
