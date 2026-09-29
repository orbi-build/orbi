Upstream issue: `pyinfra-dev/pyinfra issue 1438` (read it before starting; this fork exists to send the fix upstream as a PR). The fork's default branch is `3.x`.

## Background

`print_rows()` in `src/pyinfra_cli/prints.py` sizes each column with `len()`. CJK characters occupy two terminal cells, so a row containing them (for example `server.shell (echo 随便输出一些中文试试)`) pushes its later cells right of the header and the table looks misaligned.

## Acceptance criteria

1. Column widths and padding in `print_rows()` use the display width of each cell (East Asian wide and fullwidth characters count as 2 cells, via the standard library `unicodedata.east_asian_width`), after the ANSI stripping the function already does.
2. No new dependency; the table format is otherwise unchanged (the maintainer mentioned `rich`, but that larger rewrite is out of scope here).
3. A unit test shows that a row containing CJK text is padded so the following columns start at the same display column as in an ASCII-only row.
4. `uv run pytest --disable-warnings -m 'not end_to_end'` (from AGENTS.md) passes, or the equivalent pytest run if uv is unavailable.

## Out of scope

Replacing the table printer with rich or adding borders.

