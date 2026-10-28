/**
 * Issue #1955: the shipped `clarify_verdict` Pi tool.
 *
 * The clarification gate no longer parses free text: the model answers by
 * calling one schema-checked tool, and Pi validates the arguments against
 * this tool's TypeBox schema BEFORE `execute` runs
 * (`validateToolArguments` -> `Compile(parameters).Check`, the same
 * typebox the pinned dependency here provides).
 *
 * The test loads the real extension file — the one the engine ships and
 * passes with `--extension` — and:
 *   - rejects the arguments that broke the free-text path (a `kind`/
 *     `items` proposal at the top level, an unknown check id, an unusable
 *     proposal) with the production validator;
 *   - accepts the answer shape the prompt asks for;
 *   - proves `execute` writes the validated arguments to the environment
 *     path the engine passes and terminates the run.
 *
 * Run with `node --test tests/js` (CI step "Run the Node extension test").
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Value } from "typebox/value";

import clarifyVerdict, {
  CLARIFY_TOOL_NAME,
  CLARIFY_VERDICT_ENV,
  clarifyVerdictTool,
} from "../../src/orbi/extensions/clarify_verdict.ts";

const SPLIT = {
  missing: ["single_outcome"],
  suggestion: {
    kind: "split",
    items: [
      {
        result: "Users can tag a note",
        acceptance: "filtering by the tag finds the note",
      },
      {
        result: "Users can export a note as Markdown",
        acceptance: "export downloads a .md file with title, body and tags",
        after: 1,
      },
    ],
  },
};

const CRITERIA = {
  missing: ["acceptance_condition"],
  suggestion: {
    kind: "criteria",
    items: ["Adding a tag makes the note findable", "The tag survives a reload"],
  },
};

/** The production validator: Pi validates extension tool arguments with
 *  this same typebox before it calls `execute`. */
function validates(args) {
  return Value.Check(clarifyVerdictTool.parameters, args);
}

function verdictFile() {
  const directory = mkdtempSync(join(tmpdir(), "orbi-clarify-js-"));
  return join(directory, "verdict.json");
}

test("the schema accepts the answer shape the prompt asks for", () => {
  assert.equal(validates({ missing: [] }), true);
  assert.equal(validates({ missing: ["observable_result"] }), true);
  assert.equal(validates(SPLIT), true);
  assert.equal(validates(CRITERIA), true);
});

test("the schema rejects the proposal that was dropped on the beta", () => {
  // Beta evidence: the model put kind/items at the TOP level next to
  // missing, where the old parser never looked.
  assert.equal(
    validates({
      missing: ["single_outcome"],
      kind: "split",
      items: [{ result: "A", acceptance: "a" }, { result: "B", acceptance: "b" }],
    }),
    false,
  );
  // A top-level kind alone is not an answer either.
  assert.equal(validates({ missing: ["single_outcome"], kind: "split" }), false);
});

test("the schema rejects an unknown check id or a wrong missing type", () => {
  assert.equal(validates({ missing: ["whatever"] }), false);
  assert.equal(validates({ missing: "single_outcome" }), false);
  assert.equal(validates({}), false);
});

test("the schema rejects an unusable proposal", () => {
  // A split needs two slices, each with a result and an acceptance line.
  assert.equal(
    validates({
      missing: ["single_outcome"],
      suggestion: { kind: "split" },
    }),
    false,
  );
  assert.equal(
    validates({
      missing: ["single_outcome"],
      suggestion: {
        kind: "split",
        items: [{ result: "A", acceptance: "a" }],
      },
    }),
    false,
  );
  assert.equal(
    validates({
      missing: ["single_outcome"],
      suggestion: {
        kind: "split",
        items: [{ result: "A" }, { result: "B", acceptance: "b" }],
      },
    }),
    false,
  );
  // Criteria items are plain strings.
  assert.equal(
    validates({
      missing: ["acceptance_condition"],
      suggestion: { kind: "criteria", items: [{ result: "A" }] },
    }),
    false,
  );
  // An unknown proposal kind.
  assert.equal(
    validates({
      missing: ["single_outcome"],
      suggestion: { kind: "rewrite", items: ["A", "B"] },
    }),
    false,
  );
});

test("execute writes the validated arguments and terminates the run", async () => {
  const out = verdictFile();
  process.env[CLARIFY_VERDICT_ENV] = out;
  try {
    const result = await clarifyVerdictTool.execute("call-1", SPLIT);
    assert.equal(result.terminate, true);
    assert.deepEqual(JSON.parse(readFileSync(out, "utf8")), SPLIT);
  } finally {
    delete process.env[CLARIFY_VERDICT_ENV];
  }
});

test("execute fails fast when the engine passed no output path", async () => {
  delete process.env[CLARIFY_VERDICT_ENV];
  await assert.rejects(
    () => clarifyVerdictTool.execute("call-1", { missing: [] }),
    new RegExp(CLARIFY_VERDICT_ENV),
  );
});

test("the extension registers exactly this one tool", () => {
  const registered = [];
  clarifyVerdict({ registerTool: (tool) => registered.push(tool) });
  assert.deepEqual(registered, [clarifyVerdictTool]);
  assert.equal(clarifyVerdictTool.name, CLARIFY_TOOL_NAME);
});
