// Issue #1093 acceptance units for the command-deadline extension.
// Run: node --test src/orbi/pi_extensions/command_deadline.test.mjs
// The extension imports nothing at runtime, so the units load it directly
// with node's TypeScript type stripping (node >= 22.18).
import test from "node:test";
import assert from "node:assert/strict";

import default_export, {
  DEFAULT_DEADLINE_SECONDS,
  boundBashInput,
  commandDeadlineSeconds,
  hasTimeoutWrapper,
  shellQuote,
} from "./command_deadline.ts";

// The extension's default export wires the bash tool_call handler; the
// tests drive it through a minimal fake of the ExtensionAPI.
function loadHandler() {
  const handlers = {};
  const pi = { on: (name, fn) => { handlers[name] = fn; } };
  default_export(pi);
  return handlers.tool_call;
}

test("unwrapped bash command gets the default deadline on both fields", () => {
  const input = { command: "sleep 740; cat x" };
  boundBashInput(input, commandDeadlineSeconds({}));
  assert.equal(input.timeout, 3600);
  assert.ok(
    input.command.startsWith("timeout 3600 bash -c "),
    `rewritten command: ${input.command}`,
  );
  // The rewrite must preserve the original command verbatim (quoted).
  assert.ok(input.command.endsWith(`'sleep 740; cat x'`));
});

test("a declared timeout 600 stays (the smaller value wins)", () => {
  const input = { command: "timeout 600 pytest -x tests/", timeout: 600 };
  boundBashInput(input, 3600);
  assert.equal(input.timeout, 600);
  assert.equal(input.command, "timeout 600 pytest -x tests/");
});

test("ORBI_COMMAND_DEADLINE_SECONDS=120 is honored for both fields", () => {
  const env = { ORBI_COMMAND_DEADLINE_SECONDS: "120" };
  assert.equal(commandDeadlineSeconds(env), 120);
  const input = { command: "sleep 740; cat x" };
  boundBashInput(input, commandDeadlineSeconds(env));
  assert.equal(input.timeout, 120);
  assert.ok(input.command.startsWith("timeout 120 bash -c "));
});

test("an invalid ORBI_COMMAND_DEADLINE_SECONDS falls back to 3600", () => {
  assert.equal(commandDeadlineSeconds({ ORBI_COMMAND_DEADLINE_SECONDS: "nope" }), 3600);
  assert.equal(commandDeadlineSeconds({ ORBI_COMMAND_DEADLINE_SECONDS: "0" }), 3600);
  assert.equal(commandDeadlineSeconds({ ORBI_COMMAND_DEADLINE_SECONDS: "-5" }), 3600);
  assert.equal(commandDeadlineSeconds({ ORBI_COMMAND_DEADLINE_SECONDS: "" }), 3600);
  assert.equal(commandDeadlineSeconds({}), DEFAULT_DEADLINE_SECONDS);
});

test("a declared timeout above the deadline is capped at the deadline", () => {
  const input = { command: "sleep 99999", timeout: 7200 };
  boundBashInput(input, 3600);
  assert.equal(input.timeout, 3600);
  assert.ok(input.command.startsWith("timeout 3600 bash -c "));
});

test("both timeout_duration wrapper shapes are left unwritten", () => {
  // Shape 1: the command IS the timeout wrapper.
  assert.equal(hasTimeoutWrapper("timeout 600 pytest -x"), true);
  // Shape 2: the bash -c payload form.
  assert.equal(hasTimeoutWrapper("bash -c timeout 600 pytest -x"), true);
  // Absolute-path wrappers parse too (mirrors timeout_duration).
  assert.equal(hasTimeoutWrapper("/usr/bin/timeout 600 pytest"), true);
  // A `timeout` token that is NOT a contract wrapper is still rewritten
  // (an option in between or a buried pair is not a clear deadline).
  assert.equal(hasTimeoutWrapper("timeout --kill-after=5m 300 pytest"), false);
  assert.equal(hasTimeoutWrapper("git commit -m 'fix timeout 300 bug'"), false);
  assert.equal(hasTimeoutWrapper("echo timeout"), false);

  const input = { command: "timeout --kill-after=5m 300 pytest" };
  boundBashInput(input, 3600);
  assert.ok(input.command.startsWith("timeout 3600 bash -c "));
});

test("compound commands survive the rewrite with their semantics", () => {
  const input = { command: "sleep 740; cat x && echo \"it's here\"" };
  boundBashInput(input, 3600);
  // The single quotes escape so the outer shell hands bash one script.
  assert.ok(input.command.includes(`'\\''`));
  assert.equal(input.timeout, 3600);
});

test("shellQuote wraps in single quotes and escapes embedded quotes", () => {
  assert.equal(shellQuote("pytest -x"), `'pytest -x'`);
  assert.equal(shellQuote(`echo "it's here"`), `'echo "it'\\''s here"'`);
});

test("an empty command is left untouched", () => {
  const input = { command: "   " };
  boundBashInput(input, 3600);
  assert.equal(input.command, "   ");
});

test("the tool_call handler rewrites only bash tool calls", async () => {
  const handler = loadHandler();
  const bashInput = { command: "sleep 740; cat x" };
  await handler({ toolName: "bash", input: bashInput });
  assert.ok(bashInput.command.startsWith("timeout 3600 bash -c "));
  assert.equal(bashInput.timeout, 3600);

  const readInput = { path: "/tmp/some-file" };
  await handler({ toolName: "read", input: readInput });
  assert.deepEqual(readInput, { path: "/tmp/some-file" });

  const writeInput = { path: "/tmp/other", content: "timeout 300" };
  await handler({ toolName: "write", input: writeInput });
  assert.deepEqual(writeInput, { path: "/tmp/other", content: "timeout 300" });
});
