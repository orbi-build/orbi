Upstream issue: `chainloop-dev/chainloop issue 3481` (read it before starting; this fork exists to send the fix upstream as a PR).

## Background

When a JWT inside a nested JSON string leaf is followed by an escaped quote (`\"`) or a `\n`/`\r`/`\t` escape, the betterleaks `jwt` rule's greedy secret group captures the trailing backslash. `redactLeaf` (`internal/redaction/redaction.go`) then replaces that secret inside the JSON-encoded leaf, leaving an unescaped quote; `json.Unmarshal` fails and the fail-closed fallback replaces the whole leaf with `[REDACTED:jwt]`, losing all context (e.g. the host of a presigned URL). The upstream issue has the full root cause and a reproduction.

## Acceptance criteria

1. Before replacement, each finding's secret is normalised so it does not end inside an escape sequence: trailing backslashes are trimmed (in `pendingSecrets` or `betterleaksScanner.Scan`, wherever fits the code best).
2. The whole-leaf fallback stays as the fail-closed guard for cases that still cannot be decoded.
3. Go tests in `internal/redaction` cover: a JWT followed by `\"` in a nested JSON string leaf, where only the JWT is replaced and the URL host remains; a JWT followed by `\n` in a multi-line string leaf; idempotency (redacting the output again changes nothing).
4. `go test ./internal/redaction/...` passes, and `make test-unit` if it runs in this environment.

## Out of scope

Changing the betterleaks rule definitions themselves, or any other redaction rule's behaviour beyond the backslash trim.

