# A bare "-" argument silently discards all following arguments

## Problem

In the v3 command-line parser, a lone `-` argument (conventionally "read from stdin") ends up
as the only positional argument: every argument after it is dropped without any error.

```console
$ myapp - foo bar
```

Inside the action, `cmd.Args().Slice()` is `["-"]` — `foo` and `bar` never reach the action.
Nothing errors, so callers just see truncated arguments.

Minimal reproduction:

```go
cmd := &cli.Command{
	Commands: []*cli.Command{{
		Name:  "cmd",
		Flags: []cli.Flag{&cli.StringFlag{Name: "option"}},
		Action: func(_ context.Context, c *cli.Command) error {
			fmt.Printf("%q %q\n", c.String("option"), c.Args().Slice())
			return nil
		},
	}},
}
_ = cmd.Run(context.Background(), []string{"app", "cmd", "-", "foo", "bar"})
// prints: "" ["-"]     expected: "" ["-" "foo" "bar"]
```

Every other branch of argument parsing (empty argument, ordinary positional argument, `--`,
negative-number-like arguments) keeps the rest of the command line; only the bare dash loses it.

## Expected behaviour

A bare `-` is treated as an ordinary positional operand, and parsing carries on after it:

- `cmd - foo bar` → args `["-", "foo", "bar"]`
- `cmd - --option my-option foo` → option is `my-option`, args `["-", "foo"]` (flags after the dash are still parsed)
- `cmd - -- --option leftover` → args `["-", "--option", "leftover"]` (`--` after the dash still terminates flag parsing)
- `cmd " - " foo` → args `[" - ", "foo"]` (positional arguments are passed through exactly as given, like any other positional argument)
- `cmd - --undefined` → error `flag provided but not defined: -undefined`

## Acceptance

- Fix the problem described above.
- Add tests covering this scenario.
- The repository's own test suite passes: `go test ./...` (and `go vet ./...`).
