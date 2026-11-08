/**
 * Issue #2041: the shipped draft tools of the drafting session.
 *
 * orbi draft's Pi session answers by calling exactly one of two tools —
 * write_draft or ask_user — and Pi validates the arguments against this
 * TypeBox schema BEFORE execute runs (validateToolArguments ->
 * Compile(parameters).Check, the same typebox the pinned dependency here
 * provides). The engine then reads the JSON the tool wrote to
 * ORBI_DRAFT_OUT.
 *
 * The test loads the real extension file — the one the engine ships and
 * passes with --extension — and:
 *   - rejects an answer the engine cannot create from (no Issue, an Issue
 *     without a title or body, an unknown field, a non-integer after);
 *   - accepts the shapes the prompt asks for, including several Issues in
 *     delivery order;
 *   - proves both tools write the validated arguments plus their kind to
 *     the environment path and terminate the run.
 *
 * Run with: node --test tests/js/*.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Value } from "typebox/value";

import draftTools, {
  ASK_USER_TOOL,
  DRAFT_OUT_ENV,
  WRITE_DRAFT_TOOL,
  askUserTool,
  writeDraftTool,
} from "../../src/orbi/extensions/draft_tools.ts";

const TWO_ISSUES = {
  reply: "Notes will get tags and a tag filter.",
  issues: [
    {
      title: "Tag a note",
      body: "## Outcome\n\nA user can tag a note.\n\n## Acceptance\n\n- A tag shows on the note.",
    },
    {
      title: "Filter notes by tag",
      body: "## Outcome\n\nA user can filter notes.\n\n## Acceptance\n\n- The filter finds the tagged note.",
      after: 1,
    },
  ],
};

// Issue #2136: a question is the object the hosted page renders as option
// buttons, the recommended option first.
const QUESTIONS = {
  reply: "One thing is not in the repository.",
  questions: [
    {
      question: "Which store should the tags live in?",
      header: "Tag store",
      options: [
        {
          label: "A local file (Recommended)",
          description: "Keeps the tags in the repository's own file.",
        },
        {
          label: "A remote service",
          description: "Reads and writes the tags over the network.",
        },
      ],
    },
  ],
};

/** The production validator: Pi validates tool arguments with this same
 *  typebox before it calls execute. */
function validates(tool, args) {
  return Value.Check(tool.parameters, args);
}

function answerFile() {
  const directory = mkdtempSync(join(tmpdir(), "orbi-draft-js-"));
  return join(directory, "answer.json");
}

test("the two tools are the names the prompt names", () => {
  assert.equal(writeDraftTool.name, WRITE_DRAFT_TOOL);
  assert.equal(askUserTool.name, ASK_USER_TOOL);
  assert.notEqual(WRITE_DRAFT_TOOL, ASK_USER_TOOL);
});

test("the schema accepts a draft of one or several Issues", () => {
  assert.equal(validates(writeDraftTool, TWO_ISSUES), true);
  assert.equal(
    validates(writeDraftTool, { reply: "x", issues: [TWO_ISSUES.issues[0]] }),
    true,
  );
});

test("the schema rejects a draft the engine cannot create", () => {
  // No Issue at all: a draft round must carry at least one.
  assert.equal(validates(writeDraftTool, { reply: "x", issues: [] }), false);
  // An Issue without its title or body.
  assert.equal(
    validates(writeDraftTool, {
      reply: "x",
      issues: [{ body: "## Outcome\n\na" }],
    }),
    false,
  );
  assert.equal(
    validates(writeDraftTool, {
      reply: "x",
      issues: [{ title: "A", body: "" }],
    }),
    false,
  );
  // A field the engine never reads is never silently accepted either.
  assert.equal(
    validates(writeDraftTool, {
      reply: "x",
      issues: [{ title: "A", body: "b", milestone: "v1" }],
    }),
    false,
  );
  // after is a 1-based Issue NUMBER, not a string.
  assert.equal(
    validates(writeDraftTool, {
      reply: "x",
      issues: [{ title: "A", body: "b", after: "1" }],
    }),
    false,
  );
});

test("the schema keeps the questions a round may ask", () => {
  assert.equal(validates(askUserTool, QUESTIONS), true);
  assert.equal(validates(askUserTool, { reply: "x", questions: [] }), false);
  const [question] = QUESTIONS.questions;
  // A bare string question carries no options the page can render.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: ["Which store should the tags live in?"],
    }),
    false,
  );
  // No header: the page drops the whole round's questions.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{ question: question.question, options: question.options }],
    }),
    false,
  );
  // One option is not a choice, and five is more than the page shows.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{ ...question, options: [question.options[0]] }],
    }),
    false,
  );
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [...question.options, ...question.options,
                  ...question.options, question.options[0]],
      }],
    }),
    false,
  );
  // An option without its label or description is not a choice either.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [{ description: "no label" }, question.options[1]],
      }],
    }),
    false,
  );
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [{ label: "no description" }, question.options[1]],
      }],
    }),
    false,
  );
  // Four questions in one round is one too many.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [question, question, question, question],
    }),
    false,
  );
});

test("write_draft writes the draft answer and terminates", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  const result = await writeDraftTool.execute("call-1", TWO_ISSUES);
  const written = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(written.kind, "draft");
  assert.deepEqual(written.issues, TWO_ISSUES.issues);
  assert.equal(written.reply, TWO_ISSUES.reply);
  assert.equal(result.terminate, true);
});

test("ask_user writes the questions and terminates", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  const result = await askUserTool.execute("call-2", QUESTIONS);
  const written = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(written.kind, "questions");
  assert.deepEqual(written.questions, QUESTIONS.questions);
  assert.equal(result.terminate, true);
});

test("a tool without the output path fails instead of losing the answer", async () => {
  delete process.env[DRAFT_OUT_ENV];
  await assert.rejects(
    () => writeDraftTool.execute("call-3", TWO_ISSUES),
    new RegExp(DRAFT_OUT_ENV),
  );
});

test("the extension registers exactly the two tools", () => {
  const names = [];
  draftTools({ registerTool: (tool) => names.push(tool.name) });
  assert.deepEqual(names, [WRITE_DRAFT_TOOL, ASK_USER_TOOL]);
});
