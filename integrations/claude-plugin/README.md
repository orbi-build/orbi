# Orbi

This plugin writes GitHub Issues that an autonomous agent can deliver without
supervision.

The `write-ai-ready-issue` skill turns the current conversation into an Issue
with three sections: Background, Acceptance criteria and Out of scope. The
acceptance criteria are a checklist, and each item is written so it can be
verified inside the repository with its tests. The skill uses the twelve
factors at https://aiready.sh/ as the bar. If a task has more than one
independently mergeable result, the skill proposes splitting it into several
Issues first.

## Install

The same folder installs in GitHub Copilot
(`copilot plugin install orbi-build/orbi:integrations/claude-plugin`) and in
Cursor; the `write-ai-ready-issue` skill loads in all of them, while the
`/orbi:` commands load in Claude Code and Cursor only.

## Write an Issue (no Orbi needed)

The skill works for anyone, in any repository whose Issues are enabled. It
drafts Background, Acceptance criteria and Out of scope, and when a task has
several independently mergeable results it proposes splitting it into several
Issues first.

- In Claude Code with an authenticated `gh`: the skill resolves the repository
  and checks that Issues are enabled. By default it hands you the Issue text,
  and it creates an unlabeled Issue only if you ask.
- In claude.ai chat: there is no shell. You name the repository, and the skill
  gives you the text plus a prefilled "new issue" URL. It cannot check the
  repository, and a very long body may have to be pasted instead of carried in
  the URL.

## Hand it to Orbi (repositories Orbi delivers)

A repository is delivered by Orbi when it has the `ai-ready` label: it is
handled by Orbi Cloud or by a self-hosted Orbi. There:

- `/orbi:ship` runs the skill on the current task. After you confirm, it
  creates the Issue with the `ai-ready` label, in the open milestone that the
  default branch's `.github/orbi.toml` names.
- `/orbi:status` lists this repository's Orbi delivery Issues. It reads the
  `ai-ready`, `ai-in-progress`, `ai-pr-opened` and `ai-blocked` labels, plus
  Issues labeled `ai-merged` in the last 7 days, and shows a table with the
  state, the linked pull request if any, and the last update.

Without the label, `/orbi:ship` still writes the Issue and says in one line
how Orbi could take it; `/orbi:status` says the repository isn't delivered by
Orbi.

Once Orbi has the Issue, it claims it, implements it on its runner, and opens
a pull request. An independent reviewer checks that delivery, and only the
reviewed head is merged. After the merge, Orbi publishes a tagged GitHub
release.

## Requirements

- An authenticated `gh` CLI, for the Claude Code path.
- For hand-off and status, a repository delivered by Orbi Cloud or by a
  self-hosted Orbi.
- More: https://orbi.build/?ref=plugin-readme or
  https://github.com/orbi-build/orbi.

## Data

The plugin runs only `gh repo view`, `gh label list`, `gh api`,
`gh issue create` and `gh issue list`. All of them talk to GitHub for the
current repository through your own `gh` authentication. The plugin sends
nothing to any other service and stores nothing.

## License

AGPL-3.0-only. See LICENSE.
