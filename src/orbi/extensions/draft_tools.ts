/**
 * Issue #2041: the drafting session's structured answer.
 *
 * orbi draft runs a read-only Pi session whose ONLY output is one of three
 * tool calls: write_draft when the model has a draft, ask_user when it
 * needs the person to answer something first, reply when the person asked
 * for no code change and the answer is a reply alone (Issue #2201). Each
 * writes its (already validated) arguments to the file the engine names in
 * ORBI_DRAFT_OUT and terminates the run, so the engine reads a JSON
 * document instead of parsing prose out of stdout.
 *
 * The schema is the contract:
 *
 * - parameters is a TypeBox schema Pi validates BEFORE execute runs
 *   (validateToolArguments -> Compile(parameters).Check), so a draft with
 *   no Issue, an Issue with no title or body, an unknown answer kind or a
 *   stray field never reaches the file;
 * - one model turn answers exactly one question: the tools are separate,
 *   so a single call cannot carry questions AND a draft;
 * - after is the 1-based number of an EARLIER Issue (the engine rejects
 *   forward references too, which a schema cannot express).
 *
 * Pi aliases typebox to its own pinned copy when it loads an extension, so
 * this file needs no package.json of its own.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { writeFileSync } from "node:fs";

/** The environment variable naming the file the answer is written to. */
export const DRAFT_OUT_ENV = "ORBI_DRAFT_OUT";

/** The three tool names the drafting prompt tells the model to call. */
export const WRITE_DRAFT_TOOL = "write_draft";
export const ASK_USER_TOOL = "ask_user";
export const REPLY_TOOL = "reply";

/** One drafted Issue: a title, the complete body and its place in order. */
const draftIssue = Type.Object(
	{
		title: Type.String({
			minLength: 1,
			description: "One line: the Issue title",
		}),
		body: Type.String({
			minLength: 1,
			description:
				"The complete Issue body: the observable result and its acceptance condition",
		}),
		after: Type.Optional(
			Type.Integer({
				minimum: 1,
				description:
					"The 1-based number of an EARLIER Issue in this same draft this one waits for; omit when nothing comes first",
			}),
		),
	},
	{ additionalProperties: false },
);

const draftParameters = Type.Object(
	{
		reply: Type.String({
			description:
				"One plain-language sentence of what will change, in the person's language",
		}),
		issues: Type.Array(draftIssue, {
			minItems: 1,
			description:
				"One Issue per outcome, in delivery order; several Issues when the request bundles independent deliverables",
		}),
	},
	{ additionalProperties: false },
);

/**
 * Issues #2136, #2147: a question is a sentence, with an OPTIONAL header
 * naming the decision and OPTIONAL choices. The engine accepts a bare string
 * as a question with no header and no options, so the schema does too; there
 * is no "Other" option, because the person always answers in their own words.
 */
const askOption = Type.Object(
	{
		label: Type.String({
			minLength: 1,
			description: "One to five words: the choice the person picks",
		}),
		description: Type.String({
			minLength: 1,
			description: "One sentence: what it means or its trade-off",
		}),
	},
	{ additionalProperties: false },
);

const askQuestionObject = Type.Object(
	{
		question: Type.String({
			minLength: 1,
			description: "One sentence, in the person's language",
		}),
		header: Type.Optional(
			Type.String({
				minLength: 1,
				description:
					"Optional: one to three words naming the decision, e.g. Sharing",
			}),
		),
		options: Type.Optional(
			Type.Array(askOption, {
				minItems: 2,
				maxItems: 4,
				description:
					"Optional, when the person has to pick: the 2 to 4 choices; a recommended one ends its label with (Recommended)",
			}),
		),
	},
	{ additionalProperties: false },
);

/** A question is its sentence, or an object carrying it. */
const askQuestion = Type.Union([
	Type.String({ minLength: 1 }),
	askQuestionObject,
]);

const askParameters = Type.Object(
	{
		reply: Type.String({
			description: "One plain-language sentence for the person",
		}),
		questions: Type.Array(askQuestion, {
			minItems: 1,
			maxItems: 3,
			description: "The questions to ask, in the person's language",
		}),
	},
	{ additionalProperties: false },
);

/**
 * Issue #2201: a request that must not change any code is answered with a
 * reply alone. Its own tool, with no Issue and no question in the schema, so
 * the reply-only answer is deliberate and a draft round still carries at
 * least one Issue.
 */
const replyParameters = Type.Object(
	{
		reply: Type.String({
			minLength: 1,
			description:
				"The reply the person reads, in their language: one line each of what the reading found worth fixing, and that Orbi delivers by opening a pull request",
		}),
	},
	{ additionalProperties: false },
);

function writeAnswer(kind: string, args: object) {
	const out = process.env[DRAFT_OUT_ENV];
	if (!out) {
		throw new Error(`${DRAFT_OUT_ENV} is not set`);
	}
	writeFileSync(out, JSON.stringify({ kind, ...args }), "utf8");
	return {
		content: [{ type: "text" as const, text: "Answer recorded." }],
		details: undefined,
		terminate: true,
	};
}

/** The tool that hands the engine the drafted Issues. */
export const writeDraftTool = {
	name: WRITE_DRAFT_TOOL,
	label: "Write draft",
	description:
		"Return the drafted Issues. Call this exactly once when the draft " +
		"is ready, and never in the same turn as ask_user or reply.",
	parameters: draftParameters,
	async execute(_toolCallId: string, args: object) {
		return writeAnswer("draft", args);
	},
};

/** The tool that asks the person the questions the draft needs. */
export const askUserTool = {
	name: ASK_USER_TOOL,
	label: "Ask the person",
	description:
		"Ask the person up to three questions in one call. A question is " +
		"one sentence, either bare or as an object with an OPTIONAL header " +
		"of one to three words naming the decision and OPTIONAL 2 to 4 " +
		"options, every option a label plus one sentence of what it means " +
		"or its trade-off. When one option is the reasonable default, its " +
		"label ends with (Recommended). Never add an Other option, and never " +
		"say the person chose the recommended option: the person always " +
		"answers in their own words. Call this only " +
		"when the request itself does not say what to build and no " +
		"deliverable default can be written, and never in the same turn as " +
		"write_draft or reply.",
	parameters: askParameters,
	async execute(_toolCallId: string, args: object) {
		return writeAnswer("questions", args);
	},
};

/** The tool that answers a request not to change any code. */
export const replyTool = {
	name: REPLY_TOOL,
	label: "Reply",
	description:
		"Return a reply alone when the person explicitly asks for no code " +
		"change (\"just review it and tell me\", \"only explain\", \"don't " +
		"touch the code\"): that request takes the no-repository-change " +
		"route. Write the reply in the person's language, one line for each " +
		"thing the reading found worth fixing, and say that Orbi delivers by " +
		"opening a pull request, so the person can name one item for Orbi to " +
		"fix. The nearest-deliverable rule applies only to a request that " +
		"asks for a change. Call this exactly once when the answer is that " +
		"reply, and never in the same turn as write_draft or ask_user.",
	parameters: replyParameters,
	async execute(_toolCallId: string, args: object) {
		return writeAnswer("reply", args);
	},
};

export default function draftTools(pi: ExtensionAPI) {
	pi.registerTool(writeDraftTool);
	pi.registerTool(askUserTool);
	pi.registerTool(replyTool);
}
