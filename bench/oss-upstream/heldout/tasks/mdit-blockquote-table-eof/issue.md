# IndexError: table inside a blockquote at end of input

## Problem

Parsing input that ends on a bare blockquote marker while a table is open inside that
blockquote raises `IndexError: string index out of range` instead of producing tokens.

## Reproduction

```python
from markdown_it import MarkdownIt

MarkdownIt().enable("table").parse("> | a | b |\n> |---|---|\n>")
# IndexError: string index out of range

MarkdownIt("commonmark", {"html": False}).enable("table").parse("> | a | b |\n> |---|---|\n>")
# IndexError: string index out of range  (a different code path, same symptom)
```

The `table` rule has to be enabled, so the `gfm-like` and `js-default` presets are affected;
plain `commonmark` is not.

Variants (all with `MarkdownIt().enable("table")`):

| input | result |
|---|---|
| `"> \| a \| b \|\n> \|---\|---\|\n>"` | IndexError |
| `"> \| a \| b \|\n> \|---\|---\|\n> "` (marker + space) | IndexError |
| `"> > \| a \| b \|\n> > \|---\|---\|\n> >"` (nested) | IndexError |
| `"> \| a \| b \|\n> \|---\|---\|\n> \| 1 \| 2 \|\n>"` (body row, then bare marker) | IndexError |
| `"> \| a \| b \|\n> \|---\|---\|\n>\n"` (trailing newline) | ok |
| `"> \| a \| b \|\n> \|---\|---\|\n> \| 1 \| 2 \|"` (complete) | ok |
| `"\| a \| b \|\n\|---\|---\|"` (no blockquote) | ok |
| `"> text\n>"` (no table) | ok |

A trailing newline makes the error disappear, which is why it is easy to miss when rendering
whole documents but hard to avoid when rendering incrementally: while a quoted table is being
streamed a token at a time (e.g. an LLM reply rendered live), `> |---|---|\n>` exists for a
moment, and any renderer that parses at construction time crashes.

## Expected behavior

`parse()` / `render()` never raise for these inputs, with or without the `html` option; the
output for inputs that already worked is unchanged.

## Acceptance

- Fix the problem described above (for both reproductions: `html` enabled and `html` disabled).
- Add tests covering these scenarios.
- The repository's own test suite passes: `pytest tests/`
