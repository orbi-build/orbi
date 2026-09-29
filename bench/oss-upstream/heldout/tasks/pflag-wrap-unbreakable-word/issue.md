# FlagUsagesWrapped stops wrapping the rest of a usage string after a word wider than the description column

## Problem

When a flag's usage string contains a single word that is too wide to fit in the remaining
description column, `FlagUsagesWrapped` gives up wrapping for the *entire rest* of the usage
string, not just for that one word. Everything after the over-wide word ends up on one long line.

## Reproduction

```go
package main

import (
	"fmt"

	"github.com/spf13/pflag"
)

func main() {
	fs := pflag.NewFlagSet("example", pflag.ContinueOnError)
	fs.String("mount", "", "a mount specification e.g. 'type=bind,source=/opt,destination=/hostopt'. The rest of this description is never wrapped.")
	fmt.Print(fs.FlagUsagesWrapped(60))
}
```

Actual output (the second line is 116 columns wide):

```
      --mount string   a mount specification e.g.
                       'type=bind,source=/opt,destination=/hostopt'. The rest of this description is never wrapped.
```

## Expected behaviour

The unbreakable word overflows on a line of its own, and wrapping resumes for the words after it:

```
      --mount string   a mount specification e.g.
                       'type=bind,source=/opt,destination=/hostopt'.
                       The rest of this description is
                       never wrapped.
```

Existing wrapping behaviour for usage strings without over-wide words (including embedded
newlines) must not change.

## Acceptance

- Fix the problem described above.
- Add tests covering this scenario.
- The repository's own test suite passes: `go test ./...` (and `go vet ./...`).
