/**
 * Issue #2041: the drafting session's structured answer.
 *
 * orbi draft runs a read-only Pi session whose ONLY output is one of two
 * tool calls: write_draft when the model has a draft, ask_user when it
 * needs the person to answer something first. Both write their (already
 * validated) arguments to the file the engine names in ORBI_DRAFT_OUT and
 * terminate the run, so the engine reads a JSON document instead of
 * parsing prose out of stdout.
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

/** The two tool names the drafting prompt tells the model to call. */
export const WRITE_DRAFT_TOOL = "write_draft";
export const ASK_USER_TOOL = "ask_user";

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

const askParameters = Type.Object(
	{
		reply: Type.String({
			description: "One plain-language sentence for the person",
		}),
		questions: Type.Array(Type.String({ minLength: 1 }), {
			minItems: 1,
			maxItems: 3,
			description: "The numbered questions to ask, in the person's language",
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
		"is ready, and never in the same turn as ask_user.",
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
		"Ask the person up to three questions. Call this exactly once when " +
		"the repository cannot answer them, and never in the same turn as " +
		"write_draft.",
	parameters: askParameters,
	async execute(_toolCallId: string, args: object) {
		return writeAnswer("questions", args);
	},
};

export default function draftTools(pi: ExtensionAPI) {
	pi.registerTool(writeDraftTool);
	pi.registerTool(askUserTool);
}
