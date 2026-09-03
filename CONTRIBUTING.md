# Contributing to Orbi

How to build, test, and open a pull request lives in
[docs/contributing.mdx](docs/contributing.mdx), and the working agreement for
agents and humans alike is [AGENTS.md](AGENTS.md). This file covers only the
legal side, because that part has to be settled before your first patch lands.

## Licence of your contribution

Orbi is [fair-code](https://faircode.io) under the **Sustainable Use License**
v1.0 ([LICENSE.md](LICENSE.md)). By opening a pull request you agree that your
contribution is licensed under those same terms.

## Developer Certificate of Origin

Every commit must be signed off. Add `-s` when you commit:

```bash
git commit -s -m "your message"
```

That appends a line naming you as the author:

```
Signed-off-by: Your Name <your.email@example.com>
```

The sign-off is your statement that you wrote the patch, or otherwise have the
right to submit it under this project's licence — the full text is the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).

## Relicensing

You grant the maintainers the right to distribute your contribution under a
different licence in the future, provided that licence continues to allow
self-hosted use free of charge.

This clause exists because of what happens without it. Elastic, Redis, and
Sourcegraph each reached a point where their licence no longer fit their
business, and each had to negotiate with contributors, rewrite code, or take
the reputational hit of changing terms unilaterally. Agreeing up front costs
you nothing today and keeps that door open without a fight.

The condition is the part that binds us: **whatever the licence becomes,
running Orbi on your own machine stays free.** A relicense that broke that
promise would violate this clause.

## Questions

If any of this is unclear, ask in
[Discussions](https://github.com/orbi-build/orbi/discussions) before you start
work. A licence question is cheaper to answer than to unwind.
