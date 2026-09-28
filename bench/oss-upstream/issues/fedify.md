Upstream issue: `fedify-dev/fedify issue 1098` (read it before starting; this fork exists to send the fix upstream as a PR).

## Background

`MockFederation.setActorDispatcher()` in `packages/testing/src/mock.ts` returns a setters object whose `setKeyPairsDispatcher()`, `mapHandle()`, `mapAlias()` and `authorize()` return the `MockFederation` instead of the setters object, so chaining them (as the real `Federation` API allows) fails with `TypeError: mapHandle is not a function`. The setters returned by `setObjectDispatcher()` and the collection dispatchers return the federation from `authorize()` in the same way. `mapPortableActorId()` already returns the setters object, as the real implementation does. The actor setters also lack `mapActorAlias()`.

## Acceptance criteria

1. Every method on every setters object the mock returns (actor, object, collection dispatchers) returns that setters object, matching the real implementation in `packages/fedify`.
2. The actor setters gain `mapActorAlias()`, with the same signature and behaviour as the real implementation.
3. Tests in the testing package cover: chaining every actor setter in any order; chaining `authorize()` on object and collection setters; the registered callbacks still being used by the mock context.
4. The repository's own checks pass for the touched package (see AGENTS.md / CONTRIBUTING.md for the commands, e.g. `mise run test:deno`).

## Out of scope

Changes outside `packages/testing`, and any change to the real `Federation` implementation.

