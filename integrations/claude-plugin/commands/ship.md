---
description: Turn the current task into a GitHub Issue an AI agent can deliver, then hand it to Orbi when Orbi already delivers this repository.
---

# /orbi:ship

Run the `write-ai-ready-issue` skill on the current task, following steps 1-5
of the skill: write the Issue, check the target repository, and hand it off
only when the repository is delivered by Orbi.

The hand-off resolves the repository's `active_milestone` from the default
branch, never from a working copy:

```
gh api repos/<owner>/<repo>/contents/.github/orbi.toml \
  -H "Accept: application/vnd.github.raw"

gh api "repos/<owner>/<repo>/milestones?state=open&per_page=100" \
  --jq '.[].title'
```

It adds `--milestone` only while the value is among the open milestones.
