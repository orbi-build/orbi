## Background

Which tickets vX.Y.Z includes and why this release is being cut.

## Not included in this release

List tickets left in other milestones so they are not included accidentally.

## Preconditions

- All milestone vX.Y.Z Issues except this ticket are closed
- CI is green for the release commit

If a precondition is not met, enter the recoverable waiting state described in
#381; do not burn the ticket into terminal `ai-blocked`.

**This ticket carries `ai-ready`**: it must be claimable as soon as it is opened.

## Acceptance

- The remote contains tag `vX.Y.Z` and the corresponding GitHub Release
- Release notes include the ticket numbers in this release's scope
- Every ticket in scope has state `ai-merged`

## Release

- version: vX.Y.Z
- base_branch: main
- scope_from_milestone: vX.Y.Z

## Release section reference (for maintainers; remove this section when filing)

- `version` is the exact tag name and `base_branch` is the branch from which
the release commit is frozen. Neither may contain spaces or be empty.
- Choose exactly one of `scope` (a hand-written list) and
`scope_from_milestone` (a Milestone title). The parser rejects both or neither.
- A hand-written `scope` contains at least one `#N` item:

```markdown
## Release

- version: vX.Y.Z
- base_branch: main
- scope:
  - #123
  - #124
```

- Non-Python projects must declare `version_file` (the default is
`pyproject.toml`; omitting it stalled orbi-build/orbi-cloud#246):

```markdown
## Release

- version: vX.Y.Z
- base_branch: main
- scope_from_milestone: vX.Y.Z
- version_file: package.json
```

- Supported `version_file` values are `pyproject.toml` (default),
`package.json`, `pom.xml`, `build.gradle`, `build.gradle.kts`,
`gradle.properties`, `Cargo.toml`, `composer.json`, `pubspec.yaml`, and `none`.
