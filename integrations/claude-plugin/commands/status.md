---
description: List this repository's Orbi delivery Issues and their state from the ai-ready, ai-in-progress, ai-pr-opened, ai-blocked and ai-merged labels.
---

# /orbi:status

If the repository has no `ai-ready` label, say it is not delivered by Orbi and
stop.

Otherwise list the repository's Issues for each delivery label:

```
gh issue list --limit 200 --state all \
  --json number,title,labels,updatedAt,url --label ai-ready
gh issue list --limit 200 --state all \
  --json number,title,labels,updatedAt,url --label ai-in-progress
gh issue list --limit 200 --state all \
  --json number,title,labels,updatedAt,url --label ai-pr-opened
gh issue list --limit 200 --state all \
  --json number,title,labels,updatedAt,url --label ai-blocked
```

Then the Issues labeled `ai-merged`: keep the ones whose `updatedAt` falls
within the last 7 days.

```
gh issue list --limit 200 --state all \
  --json number,title,labels,updatedAt,url --label ai-merged
```

Show a short table with the state read from the labels, the linked PR if the
Issue text or comments name one, and the last update.
