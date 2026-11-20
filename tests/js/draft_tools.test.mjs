/**
 * Issue #2041: the shipped draft tools of the drafting session.
 *
 * orbi draft's Pi session answers by calling exactly one of three tools —
 * write_draft, ask_user or reply — and Pi validates the arguments against this
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
 *     delivery order and a reply alone for a request not to change code
 *     (Issue #2201);
 *   - proves every tool writes the validated arguments plus their kind to
 *     the environment path and terminates the run;
 *   - Issue #2303: proves the two answer tools run the ENGINE's validation
 *     command before recording, so a rule the schema cannot express comes back
 *     as an error result the model corrects in the same session (no answer
 *     file, no terminate), and the rejection reason is kept next to the answer.
 *
 * The validation command is stubbed here: the real entry is the engine's own
 * `python -m orbi.draft_validate` over `parse_answer`, which its Python test
 * (`tests/test_draft_validate.py`) drives directly. This test owns the tool
 * side of the contract — run the command with the answer, then either record
 * it or hand the reason back.
 *
 * Run with: node --test tests/js/*.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Value } from "typebox/value";

import draftTools, {
  ASK_USER_TOOL,
  DRAFT_OUT_ENV,
  DRAFT_REJECTION_SUFFIX,
  DRAFT_VALIDATE_ENV,
  REPLY_TOOL,
  WRITE_DRAFT_TOOL,
  askUserTool,
  replyTool,
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

// Issues #2136, #2147: a question is its sentence, with an optional header
// and optional choices; a recommended option carries the label suffix.
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

/**
 * Issue #2303: the stub of the engine's validation command.
 *
 * The tools run `<command from the environment>` with the answer on stdin and
 * take the exit code as the verdict. This stub records the document it read and
 * answers with the exit code and reason the current test set, so the test never
 * restates an engine rule — the engine's own rules are proven by its Python
 * test.
 */
function stubValidator() {
	const directory = mkdtempSync(join(tmpdir(), "orbi-draft-validate-"));
	const script = join(directory, "validate.mjs");
	const record = join(directory, "seen.json");
	writeFileSync(
		script,
		[
			'import { writeFileSync } from "node:fs";',
			'let input = "";',
			'process.stdin.setEncoding("utf8");',
			'process.stdin.on("data", (chunk) => { input += chunk; });',
			'process.stdin.on("end", () => {',
			"  writeFileSync(process.env.STUB_RECORD, input);",
			'  process.stdout.write(process.env.STUB_REASON ?? "");',
			'  process.exit(Number(process.env.STUB_EXIT ?? "0"));',
			"});",
			"",
		].join("\n"),
		"utf8",
	);
	// The engine hands the command over as a JSON argv array (Issue #2303).
	process.env[DRAFT_VALIDATE_ENV] = JSON.stringify([process.execPath, script]);
	process.env.STUB_RECORD = record;
	const stub = {
		command: process.env[DRAFT_VALIDATE_ENV],
		script,
		record,
		/** The stub accepts (the default) and forgets any reason. */
		accept() {
			process.env.STUB_EXIT = "0";
			delete process.env.STUB_REASON;
		},
		/** The stub rejects every answer with this reason. */
		reject(reason) {
			process.env.STUB_EXIT = "1";
			process.env.STUB_REASON = reason;
		},
		/** The answer document the tools last handed the validator. */
		seen() {
			return JSON.parse(readFileSync(record, "utf8"));
		},
	};
	stub.accept();
	return stub;
}

const VALIDATOR = stubValidator();
// The stub rejects an answer with this reason, the engine's own words for a
// self-referenced `after` on a one-Issue draft.
const SELF_REFERENCE = "Issue 1: after must name an EARLIER Issue (1..0)";

test("the three tools are the names the prompt names", () => {
  assert.equal(writeDraftTool.name, WRITE_DRAFT_TOOL);
  assert.equal(askUserTool.name, ASK_USER_TOOL);
  assert.equal(replyTool.name, REPLY_TOOL);
  assert.equal(
    new Set([WRITE_DRAFT_TOOL, ASK_USER_TOOL, REPLY_TOOL]).size,
    3,
  );
});

test("the reply tool carries only a reply (Issue #2201)", () => {
  // An explicit "do not change code" request is answered with a reply alone:
  // no Issue and no question can travel with it.
  assert.equal(
    validates(replyTool, { reply: "Nothing needs changing." }),
    true,
  );
  assert.equal(validates(replyTool, { reply: "x", issues: [] }), false);
  assert.equal(
    validates(replyTool, { reply: "x", issues: TWO_ISSUES.issues }),
    false,
  );
  assert.equal(validates(replyTool, { reply: "x", questions: [] }), false);
  assert.equal(validates(replyTool, { reply: "" }), false);
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
  // A bare string question is a question with no header and no options, and
  // a question object may carry neither (Issue #2147).
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: ["Which store should the tags live in?"],
    }),
    true,
  );
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{ question: question.question }],
    }),
    true,
  );
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{ question: question.question, options: question.options }],
    }),
    true,
  );
  // Where the recommended option sits is the caller's business: reversed
  // options are still a valid question.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [question.options[1], question.options[0]],
      }],
    }),
    true,
  );
  // Issue #2228: drafting and stops read ONE question parser, so an option
  // may mark the reasonable default with the `recommended` field instead of
  // the label suffix — the engine takes that option exactly as before, and
  // the field must reach it rather than be refused here.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [
          {
            ...question.options[0],
            label: "A local file",
            recommended: true,
          },
          question.options[1],
        ],
      }],
    }),
    true,
  );
  // The field is a boolean, like the stop option's.
  assert.equal(
    validates(askUserTool, {
      reply: "x",
      questions: [{
        ...question,
        options: [
          { ...question.options[0], recommended: "yes" },
          question.options[1],
        ],
      }],
    }),
    false,
  );
  // One option is not a choice, and five is more than a question offers.
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

test("the reply tool writes the reply answer and terminates", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  const result = await replyTool.execute(
    "call-4",
    { reply: "Nothing to change." },
  );
  assert.deepEqual(JSON.parse(readFileSync(out, "utf8")), {
    kind: "reply",
    reply: "Nothing to change.",
  });
  assert.equal(result.terminate, true);
});

test("a tool without the output path fails instead of losing the answer", async () => {
  delete process.env[DRAFT_OUT_ENV];
  await assert.rejects(
    () => writeDraftTool.execute("call-3", TWO_ISSUES),
    new RegExp(DRAFT_OUT_ENV),
  );
});

test("the extension registers exactly the three tools", () => {
  const names = [];
  draftTools({ registerTool: (tool) => names.push(tool.name) });
  assert.deepEqual(names, [WRITE_DRAFT_TOOL, ASK_USER_TOOL, REPLY_TOOL]);
});

// --- Issue #2303: a rule violation returns to the model in the session ----

// The engine's own words for two recommended options (question.py).
const TWO_RECOMMENDED = "question 1: at most one option may be recommended";

const SELF_REFERENCED_DRAFT = {
  reply: "One Issue.",
  issues: [
    {
      title: "Tag a note",
      body: "## Outcome\n\nA user can tag a note.",
      after: 1,
    },
  ],
};

const TWO_RECOMMENDED_QUESTIONS = {
  reply: "Which store?",
  questions: [
    {
      question: "Which store should the tags live in?",
      options: [
        {
          label: "A local file (Recommended)",
          description: "Keeps the tags in the repository's own file.",
        },
        {
          label: "A remote service (Recommended)",
          description: "Reads and writes the tags over the network.",
        },
      ],
    },
  ],
};

test("an invalid draft returns the reason instead of recording it", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  VALIDATOR.reject(SELF_REFERENCE);

  const rejected = await writeDraftTool.execute(
    "call-5",
    SELF_REFERENCED_DRAFT,
  );

  // The reason the engine would have raised only after the session now
  // reaches the model as the tool's own answer...
  assert.equal(rejected.isError, true);
  assert.ok(rejected.content[0].text.includes(SELF_REFERENCE));
  // ...the turn does NOT end and nothing is recorded...
  assert.notEqual(rejected.terminate, true);
  assert.equal(existsSync(out), false);
  // ...and what the validator judged is the answer document the engine reads.
  assert.equal(VALIDATOR.seen().kind, "draft");
  assert.deepEqual(VALIDATOR.seen().issues, SELF_REFERENCED_DRAFT.issues);
  // The reason is kept next to the answer file, so a session that ends here
  // fails with it instead of a bare timeout.
  assert.equal(
    readFileSync(`${out}${DRAFT_REJECTION_SUFFIX}`, "utf8"),
    SELF_REFERENCE,
  );

  // The model corrects it and calls the tool again in the same session.
  VALIDATOR.accept();
  const accepted = await writeDraftTool.execute("call-6", TWO_ISSUES);
  assert.equal(accepted.isError, undefined);
  assert.equal(accepted.terminate, true);
  assert.deepEqual(
    JSON.parse(readFileSync(out, "utf8")).issues,
    TWO_ISSUES.issues,
  );
});

test("an invalid question answer returns the reason too", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  VALIDATOR.reject(TWO_RECOMMENDED);

  const rejected = await askUserTool.execute(
    "call-7",
    TWO_RECOMMENDED_QUESTIONS,
  );

  assert.equal(rejected.isError, true);
  assert.ok(rejected.content[0].text.includes(TWO_RECOMMENDED));
  assert.notEqual(rejected.terminate, true);
  assert.equal(existsSync(out), false);
  assert.equal(VALIDATOR.seen().kind, "questions");

  VALIDATOR.accept();
  const accepted = await askUserTool.execute("call-8", QUESTIONS);
  assert.equal(accepted.terminate, true);
  assert.deepEqual(
    JSON.parse(readFileSync(out, "utf8")).questions,
    QUESTIONS.questions,
  );
});

test("a valid answer is written and leaves no rejection record", async () => {
  const directory = mkdtempSync(join(tmpdir(), "orbi-draft-js-"));
  const out = join(directory, "answer.json");
  process.env[DRAFT_OUT_ENV] = out;
  VALIDATOR.accept();

  const result = await writeDraftTool.execute("call-9", TWO_ISSUES);

  assert.equal(result.terminate, true);
  assert.equal(existsSync(`${out}${DRAFT_REJECTION_SUFFIX}`), false);
});

test("the reply tool has no cross-field rule and is not validated", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  VALIDATOR.reject(SELF_REFERENCE);
  rmSync(VALIDATOR.record, { force: true });

  const result = await replyTool.execute(
    "call-10",
    { reply: "Nothing to change." },
  );

  assert.equal(result.terminate, true);
  assert.equal(JSON.parse(readFileSync(out, "utf8")).kind, "reply");
  // The validator never ran: a reply carries no cross-field rule.
  assert.equal(existsSync(VALIDATOR.record), false);
});

test("a tool without the validation command fails instead of recording it", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  delete process.env[DRAFT_VALIDATE_ENV];

  await assert.rejects(
    () => writeDraftTool.execute("call-11", TWO_ISSUES),
    new RegExp(DRAFT_VALIDATE_ENV),
  );
  assert.equal(existsSync(out), false);

  process.env[DRAFT_VALIDATE_ENV] = VALIDATOR.command;
});

test("a validation command whose path carries a space still validates", async () => {
  // Issue #2303: the command travels as a JSON argv array, so an interpreter
  // or script path containing whitespace (a home directory with a space) is
  // not mis-split into a nonexistent executable.
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  const spaced = join(
    mkdtempSync(join(tmpdir(), "orbi draft space-")),
    "validate.mjs",
  );
  writeFileSync(spaced, readFileSync(VALIDATOR.script, "utf8"), "utf8");
  process.env[DRAFT_VALIDATE_ENV] = JSON.stringify([process.execPath, spaced]);
  VALIDATOR.accept();

  const result = await writeDraftTool.execute("call-13", TWO_ISSUES);

  assert.equal(result.terminate, true);
  assert.deepEqual(
    JSON.parse(readFileSync(out, "utf8")).issues,
    TWO_ISSUES.issues,
  );
  process.env[DRAFT_VALIDATE_ENV] = VALIDATOR.command;
});

test("a validation command that cannot run fails the tool", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  // A command that exists nowhere: an answer must never be recorded
  // unchecked, so this fails the tool instead of passing it.
  const missing = join(
    mkdtempSync(join(tmpdir(), "orbi-draft-missing-")),
    "validate.mjs",
  );
  process.env[DRAFT_VALIDATE_ENV] = JSON.stringify([missing]);

  await assert.rejects(
    () => writeDraftTool.execute("call-12", TWO_ISSUES),
    new RegExp(DRAFT_VALIDATE_ENV),
  );
  assert.equal(existsSync(out), false);

  process.env[DRAFT_VALIDATE_ENV] = VALIDATOR.command;
});

test("a validation command that is not a JSON argv fails the tool", async () => {
  const out = answerFile();
  process.env[DRAFT_OUT_ENV] = out;
  // The engine hands the command over as a JSON argv array; anything else
  // names the variable so the breakage is readable, never a silent pass.
  process.env[DRAFT_VALIDATE_ENV] = "python3 -m orbi.draft_validate";

  await assert.rejects(
    () => writeDraftTool.execute("call-14", TWO_ISSUES),
    new RegExp(DRAFT_VALIDATE_ENV),
  );
  assert.equal(existsSync(out), false);

  process.env[DRAFT_VALIDATE_ENV] = VALIDATOR.command;
});
