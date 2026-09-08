---
name: User outcome
about: Describe one user-visible outcome and its evidence
title: ""
labels: ""
---

## User outcome
When [user/context], the user should [observable result].

## Preconditions
- User has [installation/configuration/provider/access prerequisite].
- User runs: `[real command or interaction]`.

## Acceptance
### Success path
- System action: [what the system actually does].
- User sees: [specific result and where it appears].

### Failure path
- Trigger: [failure condition].
- User sees: [specific error/journal/Issue/PR/UI evidence].
- Repair action: [concrete fix the user can take].

## Evidence
- Real entry point: [CLI, setup, provider, concurrency scenario, UI, or public caller].
- Test/log/journal/Issue/PR/UI evidence: [link or command and expected output].
- Verification depth matches the impact; unit tests alone do not replace the user path.
