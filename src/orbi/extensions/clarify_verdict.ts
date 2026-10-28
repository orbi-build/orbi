/**
 * Issue #1955: the clarification gate's structured answer.
 *
 * The gate asks a model whether a ticket states enough to deliver it, and
 * the answer used to be free text the engine parsed as JSON. The model put
 * `kind`/`items` at the top level next to `missing`, the parser only read
 * `suggestion`, and every proposal was dropped silently.
 *
 * This extension ships WITH the engine and is the only extension the gate
 * session loads (`--no-extensions --extension <this file>
 * --no-builtin-tools`). It registers ONE tool, `clarify_verdict`:
 *
 * - `parameters` is the existing answer shape as a TypeBox schema. Pi
 *   validates the model's arguments against it BEFORE `execute` runs
 *   (`validateToolArguments` -> `Compile(parameters).Check`), so a
 *   top-level `kind`, an unknown check id or a single-slice split never
 *   reaches the file.
 * - `execute` writes the validated arguments as JSON to the path the
 *   engine names in `ORBI_CLARIFY_OUT` and returns `terminate: true`, so
 *   the run ends on the tool call instead of paying for a follow-up turn.
 *
 * The engine reads that file instead of stdout; a missing or invalid file
 * is the same unusable answer as before (the gate fails open).
 *
 * Pi aliases `typebox` to its own pinned copy when it loads an extension,
 * so this file needs no package.json of its own.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { writeFileSync } from "node:fs";

/** The environment variable naming the file the verdict is written to. */
export const CLARIFY_VERDICT_ENV = "ORBI_CLARIFY_OUT";

/** The tool name the gate's prompt tells the model to call. */
export const CLARIFY_TOOL_NAME = "clarify_verdict";

/** The three check identifiers the gate can report (clarify.MISSING). */
export const CHECK_IDS = [
	"observable_result",
	"acceptance_condition",
	"single_outcome",
] as const;

const splitItem = Type.Object(
	{
		result: Type.String({
			description: "One line: the observable result this new ticket delivers",
		}),
		acceptance: Type.String({
			description: "One line: how a finished delivery of this slice is recognised",
		}),
		after: Type.Optional(
			Type.Integer({
				minimum: 1,
				description:
					"The 1-based number of an EARLIER item this slice depends on; omit when it depends on nothing",
			}),
		),
	},
	{ additionalProperties: false },
);

const suggestion = Type.Union([
	Type.Object(
		{
			kind: Type.Literal("split"),
			items: Type.Array(splitItem, {
				minItems: 2,
				description: "One item per new ticket; at least two slices",
			}),
		},
		{ additionalProperties: false },
	),
	Type.Object(
		{
			kind: Type.Literal("criteria"),
			items: Type.Array(Type.String(), {
				minItems: 1,
				description: "One acceptance condition per line",
			}),
		},
		{ additionalProperties: false },
	),
]);

const parameters = Type.Object(
	{
		missing: Type.Array(
			Type.Union([
				Type.Literal(CHECK_IDS[0]),
				Type.Literal(CHECK_IDS[1]),
				Type.Literal(CHECK_IDS[2]),
			]),
			{
				description:
					"The failed checks; empty when all three checks pass",
			},
		),
		suggestion: Type.Optional(suggestion),
	},
	{ additionalProperties: false },
);

/**
 * The one tool the gate session exposes.
 *
 * `execute` receives arguments Pi already validated against
 * `parameters`; it writes exactly those arguments, so the engine reads the
 * same shape the schema describes.
 */
export const clarifyVerdictTool = {
	name: CLARIFY_TOOL_NAME,
	label: "Clarify verdict",
	description:
		"Return the ticket-gate verdict. Call this exactly once with the " +
		"failed checks and, only when the ticket text allows one, the " +
		"optional proposal.",
	parameters,
	async execute(_toolCallId: string, args: object) {
		const out = process.env[CLARIFY_VERDICT_ENV];
		if (!out) {
			throw new Error(`${CLARIFY_VERDICT_ENV} is not set`);
		}
		writeFileSync(out, JSON.stringify(args), "utf8");
		return {
			content: [
				{ type: "text" as const, text: "Verdict recorded." },
			],
			details: undefined,
			terminate: true,
		};
	},
};

export default function clarifyVerdict(pi: ExtensionAPI) {
	pi.registerTool(clarifyVerdictTool);
}
