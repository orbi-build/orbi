# `stringify()` collapses a stepped range like `6-18/6` into the non-standard `6/6`

## Problem

When a field contains a stepped range that does not start at the field's minimum,
`CronExpression#stringify()` sometimes drops the range end and emits the shorthand
`start/step`. That shorthand is not part of standard cron syntax (crontab(5) only allows
`*/step` or `first-last/step`), so the output is rejected by other cron implementations, and it
hides the explicit upper bound the user wrote.

## Reproduction

```ts
import { CronExpressionParser } from 'cron-parser';

CronExpressionParser.parse('0 6-18/6 * * *').stringify();            // '0 6/6 * * *'
CronExpressionParser.parse('0 3,6,9,12,15,18,21 * * *').stringify(); // '0 3/3 * * *'
CronExpressionParser.parse('0 0 2/2 * *').stringify();               // '0 0 2/2 * *'
CronExpressionParser.parse('5/5 * * * *').stringify();               // '5/5 * * * *'
CronExpressionParser.parse('* * 9-20/3 16-26/5 jun *').stringify(true); // '* * 9-18/3 16/5 6 *'
```

Interestingly `0 6-18/3 * * *` is rendered correctly as `0 6-18/3 * * *` — the shorthand only
appears when the last value of the range happens to be the last value reachable in the field
with that step.

## Expected behavior

A stepped range whose start is not the field minimum is always rendered with explicit bounds,
`first-last/step`:

| input | expected `stringify()` |
|---|---|
| `0 6-18/6 * * *` | `0 6-18/6 * * *` |
| `0 3,6,9,12,15,18,21 * * *` | `0 3-21/3 * * *` |
| `0 0 2/2 * *` | `0 0 2-30/2 * *` |
| `5/5 * * * *` | `5-55/5 * * * *` (`stringify(true)`: `0 5-55/5 * * * *`) |
| `* * 9-20/3 16-26/5 jun *` | `stringify(true)`: `* * 9-18/3 16-26/5 6 *` |
| `0 0 0 H/2 * *` parsed with `{ hashSeed: 'F00D' }` (hash resolves the start to 2) | `stringify(true)`: `0 0 0 2-30/2 * *` |

Re-parsing the output and stringifying again must give the same string. Ranges that cover the
whole field keep using `*/step` (e.g. `*/5 * * * *` and `0/5 * * * *` still render as
`*/5 * * * *`), and other existing stringify behaviour is unchanged.

## Acceptance

- Fix the problem described above.
- Add tests covering these scenarios (update any existing expectations that encoded the old shorthand).
- The repository's own tests pass: `npm ci && TZ=UTC npx jest` (i.e. `npm run test:unit`).
