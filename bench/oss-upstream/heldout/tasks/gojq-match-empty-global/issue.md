# match($re; "g") drops an empty match that follows a non-empty one

## Problem

When a regular expression can match the empty string at some positions but not at others
(for example `a*`, `[a-z]*`, `\s*`, `-*`), a global `match($re; "g")` silently drops any
empty match that sits immediately after a non-empty match. Every builtin that is built on top
of global matching — `scan`, `splits`, `split/2`, `sub`, `gsub` — inherits the problem, so their
output disagrees with jq (Oniguruma semantics, which Ruby `String#scan`, Python `re.findall`
and Perl `//g` share).

## Reproduction

Compared against jq 1.7:

| query | input | gojq (current) | jq (expected) |
| --- | --- | --- | --- |
| `[scan("[a-z]*")]` | `"a1b"` | `["a","b"]` | `["a","","b",""]` |
| `[scan("[a-z]*")]` | `"ab-cd"` | `["ab","cd"]` | `["ab","","cd",""]` |
| `gsub("[0-9]*";"#")` | `"a1b"` | `"#a#b#"` | `"#a##b#"` |
| `split("-*";"")` | `"ab-cd"` | `["","a","b","c","d",""]` | `["","a","b","","c","d",""]` |
| `[splits("\\s*")]` | `"a b"` | `["","a","b",""]` | `["","a","","b",""]` |
| `[match("a*";"g").offset]` | `"a1b"` | `[0,2,3]` | `[0,1,2,3]` |

```sh
$ echo '"a1b"' | gojq -c '[match("a*";"g").offset]'
[0,2,3]        # expected [0,1,2,3]
```

The existing tests do not catch this because every global-match pattern in the test suite either
can never match empty, or is `""` (which matches empty everywhere and non-empty nowhere, so both
behaviours give the same output).

## Expected behaviour

- A global match reports every match jq reports, including an empty match directly after a
  non-empty one, for `match(...; "g")`, `scan`, `splits`, `split/2`, `sub` and `gsub`.
- Offsets stay codepoint-based for multibyte input (e.g. `[match("[☆★]*"; "g").offset]` on
  `"☆★1☆★"` is `[0,2,3,5]`).
- Patterns using anchors and assertions (`^`, `(?m)^`, `\b`, alternations with `^`), capture
  groups (including unmatched optional groups) and flags such as `"i"` must keep producing
  jq-compatible results; nothing that works today may regress.

## Acceptance

- Fix the problem described above.
- Add tests covering this scenario (patterns that match empty at some offsets but not others).
- The repository's own test suite passes: `go test ./...` (and `go vet ./...`).
