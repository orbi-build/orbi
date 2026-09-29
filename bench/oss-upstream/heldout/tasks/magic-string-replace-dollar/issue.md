# `replace` / `replaceAll` mishandle `$` substitution patterns and insert `undefined` for an unmatched group

## Problem

`MagicString#replace` and `MagicString#replaceAll` accept a string replacement and are documented
to behave like `String.prototype.replace` / `replaceAll`. In practice the `$` substitution patterns
in the replacement string are only partially supported, and the result differs from what the
native string methods produce for the same input.

### An optional group that did not participate writes `undefined`

```js
import MagicString from 'magic-string'

new MagicString('ac').replace(/a(b)?/, '[$1]').toString() // '[undefined]c'
'ac'.replace(/a(b)?/, '[$1]')                             // '[]c'
```

Nothing is thrown, so generated code silently contains the text `undefined`.

### Other substitution patterns are wrong or ignored (regexp search value)

```js
// `$0` is not a group reference and must stay literal
new MagicString('abcabc').replace(/(a)/, '[$0]').toString() // '[a]bcabc'
'abcabc'.replace(/(a)/, '[$0]')                             // '[$0]bcabc'

// `$nn` must fall back to `$n` followed by a digit when group nn does not exist
new MagicString('ab').replace(/(a)/, '[$12]').toString()    // '[$12]b'
'ab'.replace(/(a)/, '[$12]')                                // '[a2]b'

// `$<name>` (named groups) is not recognised
new MagicString('ab').replace(/(?<first>a)/, '[$<first>]').toString() // '[$<first>]b'
'ab'.replace(/(?<first>a)/, '[$<first>]')                             // '[a]b'

// `` $` `` and `$'` are not recognised
new MagicString('abcabc').replace(/b/, '$`').toString()  // 'a$`cabc'
'abcabc'.replace(/b/, '$`')                              // 'aacabc'
new MagicString('abcabc').replace(/b/, "$'").toString()  // "a$'cabc"
'abcabc'.replace(/b/, "$'")                              // 'acabccabc'
```

### A string search value expands nothing at all

With a string (not regexp) search value the replacement is inserted verbatim, so the same
replacement behaves differently depending only on the type of the search value:

```js
new MagicString('abcabc').replace('b', '$$').toString() // 'a$$cabc'
new MagicString('abcabc').replace(/b/, '$$').toString() // 'a$cabc'
'abcabc'.replace('b', '$$')                             // 'a$cabc'
```

`$&`, `` $` `` and `$'` are affected the same way (for both `replace` and `replaceAll`, including
an empty-string search value). For a string search value there are no capture groups, so `$1`,
`$<name>` etc. correctly stay literal there.

## Expected behavior

For any input, `new MagicString(s).replace(pattern, replacement).toString()` and
`.replaceAll(...)` with a **string** replacement produce exactly the same string as
`s.replace(pattern, replacement)` / `s.replaceAll(pattern, replacement)`, covering the full
table of `$` patterns: `$$`, `$&`, `` $` ``, `$'`, `$n` / `$nn` (including unmatched groups,
`$0`, and out-of-range indices), and `$<name>` (only when the regexp has named groups). This
must hold for regexp and string search values, global and non-global, and for every match of
`replaceAll`. Replacement functions and source-map behaviour must not change.

## Acceptance

- Fix the problem described above.
- Add tests covering these scenarios.
- The repository's own tests pass: `pnpm install && pnpm test` (equivalently `pnpm run build && pnpm exec vitest run`).
