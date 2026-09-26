# Orbi

This plugin writes GitHub Issues that an autonomous agent can deliver without
supervision.

The `write-ai-ready-issue` skill turns the current conversation into an Issue
with three sections: Background, Acceptance criteria and Out of scope. The
acceptance criteria are a checklist, and each item is written so it can be
verified inside the repository with its tests. The skill uses the twelve
factors at https://aiready.sh/ as the bar. If a task has more than one
independently mergeable result, the skill proposes splitting it into several
Issues first. This part needs no Orbi account and works in any repository
whose Issues are enabled.

## Commands

- `/orbi:ship` runs the skill on the current task.
- `/orbi:status` lists this repository's Orbi delivery Issues. It reads the
  `ai-ready`, `ai-in-progress`, `ai-pr-opened` and `ai-blocked` labels, plus
  Issues labeled `ai-merged` in the last 7 days, and shows a table with the
  state, the linked pull request if any, and the last update.

These two commands only add behavior in a repository that Orbi delivers — one
that already has the `ai-ready` label.

## After hand-off

When you confirm the draft in a repository Orbi delivers, the skill creates
the Issue with the `ai-ready` label. Orbi claims it, implements it on its
runner, and opens a pull request. An independent reviewer checks that
delivery, and only the reviewed head is merged. After the merge, Orbi
publishes a tagged GitHub release.

## Requirements

- An authenticated `gh` CLI.
- For hand-off, a repository delivered by Orbi Cloud or by a self-hosted
  Orbi. Without one, the skill still produces the Issue text or a prefilled
  "new issue" URL.
- More: https://orbi.build/?ref=plugin-readme or
  https://github.com/orbi-build/orbi.

## Data

The plugin runs only `gh repo view`, `gh label list`, `gh issue create` and
`gh issue list`. All four talk to GitHub for the current repository through
your own `gh` authentication. The plugin sends nothing to any other service
and stores nothing.

## License

AGPL-3.0-only. See LICENSE.
