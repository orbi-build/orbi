# set_key corrupts values containing backslashes (Windows paths, regexes) on round-trip

## Problem

Writing a value with `set_key` (or the `dotenv set` CLI) and reading it back does not give
the original value when the value contains a backslash. Windows paths and regular
expressions are the most common victims.

## Reproduction

```python
import dotenv

dotenv.set_key(".env", "PATH", r"C:\Users")
print(dotenv.get_key(".env", "PATH"))   # prints "C:Users" -- the backslash is gone

dotenv.set_key(".env", "RE", r"\d+")
print(dotenv.get_key(".env", "RE"))     # prints "d+"
```

After the first call the file contains `PATH='C:\Users'`. When the file is read back,
the backslash inside the single-quoted value is decoded as an escape sequence, so it is
lost.

A value that **ends** with a backslash is worse: it does not just lose a character, the
following entries in the file can stop being read correctly too:

```python
dotenv.set_key(".env", "A", "back\\")      # value is: back\
dotenv.set_key(".env", "B", "sentinel")
dotenv.get_key(".env", "A")   # expected "back\\"
dotenv.get_key(".env", "B")   # expected "sentinel"
```

## Expected behavior

- For any string `v`, `set_key(path, key, v)` followed by `get_key(path, key)` returns
  exactly `v`. This includes values with backslashes anywhere (middle, end, several in a
  row), values that mix backslashes and single quotes (e.g. `a\'b`), values with double
  quotes, and the empty string.
- Writing such a value must not affect how other keys in the same file are read.
- Existing, correctly written `.env` files must keep parsing as before. In particular, an
  escaped backslash (`\\`) inside a single- or double-quoted value is read as one literal
  backslash, including when it is the last character before the closing quote, e.g.
  `a='b\\'` on one line followed by `c='d'` on the next yields `a = b\` and `c = d`.

## Acceptance

- Fix the problem described above.
- Add tests covering these scenarios (round-trip through `set_key`/`get_key`, and parsing
  of quoted values that contain / end with an escaped backslash).
- The repository's own test suite passes: `pytest tests`
