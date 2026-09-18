// Command deadline (Issue #1093): the execution layer enforces the
// command-deadline contract the prompt used to only ask for. Every bash
// tool call is bounded at ORBI_COMMAND_DEADLINE_SECONDS (set by the
// runner; default 3600 = PI_IDLE_WAIT_MAX_SECONDS, the single
// absolute-timeout constant): `input.timeout` is capped at the deadline
// and a command that does not already start with a coreutils `timeout`
// wrapper is rewritten to `timeout <deadline> bash -c '<original>'` so
// the declared deadline is visible on the process command line and the
// runner's idle recovery (`timeout_duration` / `_pending_timeout_targets`)
// reads it. A legitimately slow command now exits 124 with its own
// output at the deadline instead of the session being SIGTERMed by the
// 15-minute no-output escalation; an agent-declared shorter deadline
// wins (the smaller of the two).
//
// No runtime imports: the handler guards on `event.toolName` (the
// documented quick-start pattern — `isToolCallEventType` is compile-time
// only), so this file stays loadable by plain `node --test` type
// stripping and by Pi's jiti loader across versions.
//
// `event.input` is mutable on Pi's `tool_call` event and mutations affect
// the actual tool execution (pi docs, "Tool Events"); the built-in bash
// tool's input is `{ command: string; timeout?: number }` with the
// timeout in SECONDS (pi 0.85.1 `dist/core/tools/bash.js`).

// The absolute deadline when the runner set no env value: the same
// number `PI_IDLE_WAIT_MAX_SECONDS` carries in `pi_process.py`.
export const DEFAULT_DEADLINE_SECONDS = 3600;

const DURATION_SUFFIX_SECONDS = { s: 1, m: 60, h: 3600, d: 86400 };

// The basename form of `timeout_duration`'s `token.rsplit("/", 1)[-1]`.
function basename(token) {
  return token.slice(token.lastIndexOf("/") + 1);
}

// A coreutils duration token (`240`, `4m`, `1.5m`) in seconds, or null —
// mirrors `pi_recovery._parse_duration` so both sides accept exactly the
// same wrapper shapes.
export function parseDurationSeconds(token) {
  let suffix = 1;
  const last = token.charAt(token.length - 1);
  if (Object.prototype.hasOwnProperty.call(DURATION_SUFFIX_SECONDS, last)) {
    suffix = DURATION_SUFFIX_SECONDS[last];
    token = token.slice(0, -1);
  }
  if (token === "" || !/^[0-9.]+$/.test(token) || token.split(".").length > 2) {
    return null;
  }
  const seconds = Number(token) * suffix;
  return seconds > 0 ? seconds : null;
}

// Whether the command already starts with a `timeout` wrapper in one of
// the two shapes `pi_recovery.timeout_duration` accepts: `timeout <d> ...`
// (or an absolute path to it) and `bash -c timeout <d> ...`. A duration
// that cannot be parsed is NOT a clear wrapper (an option in between, a
// `timeout` token that is data) — the rewrite adds the deadline to those.
export function hasTimeoutWrapper(command) {
  const tokens = command.trim().split(/\s+/);
  let durationToken = null;
  if (tokens.length > 0 && basename(tokens[0]) === "timeout") {
    durationToken = tokens[1];
  } else if (
    tokens.length >= 3 &&
    basename(tokens[0]) === "bash" &&
    tokens[1] === "-c" &&
    basename(tokens[2]) === "timeout"
  ) {
    durationToken = tokens[3];
  }
  return durationToken != null && parseDurationSeconds(durationToken) != null;
}

// POSIX single-quoting: the rewrite hands the original command to an
// inner `bash -c`, so embedded quotes must not end the quoted script.
export function shellQuote(value) {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

// The deadline from the runner's environment; an absent or invalid value
// falls back to the default (bypass-grade — a bad value must never break
// the session).
export function commandDeadlineSeconds(env = process.env) {
  const raw = env.ORBI_COMMAND_DEADLINE_SECONDS;
  if (typeof raw !== "string" || raw.trim() === "") {
    return DEFAULT_DEADLINE_SECONDS;
  }
  const value = Number(raw);
  return Number.isFinite(value) && value > 0
    ? value
    : DEFAULT_DEADLINE_SECONDS;
}

// One bash tool input, mutated in place: the tool-level timeout is the
// smaller of the declared one and the deadline, and an unwrapped command
// gains the wrapper. Already-wrapped commands are left alone so an
// agent-declared shorter deadline survives; an empty command does
// nothing and stays untouched.
export function boundBashInput(input, deadlineSeconds) {
  if (typeof input.timeout !== "number" || !Number.isFinite(input.timeout) || input.timeout > deadlineSeconds) {
    input.timeout = deadlineSeconds;
  }
  const command = input.command;
  if (
    typeof command === "string" &&
    command.trim() !== "" &&
    !hasTimeoutWrapper(command)
  ) {
    input.command = `timeout ${deadlineSeconds} bash -c ${shellQuote(command)}`;
  }
}

// Pi loads extensions via jiti; `import type` is erased, so the file has
// no module resolution at runtime at all.
export default function commandDeadline(pi) {
  pi.on("tool_call", async (event) => {
    if (
      event.toolName !== "bash" ||
      event.input == null ||
      typeof event.input !== "object"
    ) {
      return;
    }
    boundBashInput(event.input, commandDeadlineSeconds());
  });
}
