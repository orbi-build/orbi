---
name: orbi-upstream-contribution
description: Use whenever the change may be sent to another project (the Issue mentions an upstream issue, a fork, or the repository has CONTRIBUTING.md / AI_POLICY.md). Makes the commits and PR acceptable to that project's maintainers - contribution rules, commit format, sign-off, AI disclosure, changelog, references.
---

# Upstream contribution

Code that works can still be rejected. Before the first commit, read the
project's own rules and follow them to the letter; do not rely on habits from
other repositories.

## 1. Read, then write down the rules

Read, in this order, whichever exist: `CONTRIBUTING.md` (also `.github/` and
`docs/`), `AI_POLICY.md`, `AGENTS.md` / `CLAUDE.md`, the pull request template
in `.github/`, and `git log -20 --format='%s%n%b'` of the default branch. Write
the rules that apply to this change into `.orbi/upstream.md` as a checklist:

- commit subject format: Conventional Commits (`type(scope): subject`) or the
  opposite (some projects forbid prefixes); length limit; imperative mood;
- required trailers: `Signed-off-by:` (DCO), `Assisted-by:` (AI disclosure,
  with the exact format the policy gives), issue references (`Fixes #N`,
  `Closes #N`, or a permalink URL);
- changelog convention: a fragment file (`changes.d/`, `changelog.d/`,
  `.changeset/`), an entry in `CHANGELOG.md`, or nothing (the maintainer writes
  it). When the project ships a changelog manager (`sacho`, `changeset`,
  `towncrier`, ...), use it: create the fragment with it, let it
  materialize or sync the changelog file if its config says so, and commit
  what it produces; its `check` command is the authority;
- target branch rules (for example bug fixes go to a maintenance branch) and
  anything the policy says about AI-generated contributions.

## 2. Apply them

- Every commit you create follows the subject format and carries the required
  trailers. When the policy asks for AI disclosure, add one trailer per tool
  you used, in the policy's format, and describe the extent in the PR body.
- Add a `Signed-off-by:` trailer when the project uses DCO; use the commit
  author's identity.
- Reference the upstream issue, never a fork-local number: in a fork or mirror,
  `#1` points at the wrong issue upstream. Write the upstream issue as the
  project expects (for example `Fixes #1438` only when the PR will be opened
  upstream, or a permalink), and never paste `owner/repo#N` into titles or
  bodies of fork-local issues and commits.
- Add the changelog entry in the project's form, with the contributor credit
  the convention asks for, through the project's changelog tool when it has
  one.
- Keep one logical change per PR; squash fix-up commits if the project asks
  for it.

## 3. Verify before pushing

Run the repository's own check tooling (changelog manager `check`, the
`check`/`lint` task in `mise.toml`, `Makefile`, `package.json` or `deno.json`).
Then re-read `.orbi/upstream.md` and check each rule against
`git log <base>..HEAD --format='%s%n%b'` and `git diff --stat <base>...HEAD`.
Fix any miss with `git commit --amend` or an interactive-free rebase before
pushing. A reviewer treats a missed rule as a Major finding.
