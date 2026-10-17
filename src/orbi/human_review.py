"""The human acceptance gate's checklist.

The gate's carrier is the human-only label `ai-human-review`
(`orbi.delivery_labels`); this module is its readable face: the two
columns of the acceptance checklist posted to the Issue when a delivery
completes, plus the machine-parsable block a future migration tool reads
when a column-2 item becomes machine-verifiable (the artifact shape
stays, the items migrate).

The split between the columns is DATA-DRIVEN per delivery -- derived
from the delivered diff paths and the run's test evidence -- never a
hardcoded "human-only items" list: an item leaves column 2 when a
machine-verification dimension covers it, and an empty column 2 is the
product's target state ("no human intervention needed", no label
required). Every function here is pure: a deterministic function of the
delivery's evidence.

Issue #1660: the checklist is Orbi's own comment and follows the run's
bound message language -- both columns and the wording around them; the
machine block keeps its schema and only its item TEXT follows the
language.

Wording discipline (orbi-website#128): the checklist never claims a
guarantee of correctness or direction -- column 1 is evidence of what
the run verified, column 2 is the minimal set a person still confirms.
"""
import json

from orbi import language
from orbi.delivery_labels import HUMAN_REVIEW_LABEL

# The machine-parsable block marker (an HTML comment -- GitHub renders
# nothing of it; a migration tool greps it). Schema 1: one JSON object
# with the run id and both columns verbatim.
CHECKLIST_MARKER = "orbi:human-review"
_CHECKLIST_SCHEMA = 1

# Column-2 items (the machine-unverifiable minimum). Each fires on a
# data condition of the delivered diff, not on a repo-agnostic "humans
# must check this" template. The catalog keys point at the sentence in
# the run's message language.
_INTENT_ITEM = "human_review.item_intent"
_UI_ITEM = "human_review.item_ui"
_DEPLOY_ITEM = "human_review.item_deploy"
_EVIDENCE_ITEM = "human_review.item_evidence"
_DIFF_ITEM = "human_review.item_diff"

_UI_SUFFIXES = (
    ".html", ".htm", ".css", ".scss", ".less",
    ".jsx", ".tsx", ".vue", ".svelte",
)
_UI_DIRS = frozenset({"templates", "static", "frontend", "web", "ui",
                      "assets"})
_DEPLOY_PREFIXES = (".github/", "systemd/", "3rd/monitoring/", "deploy/",
                    "charts/")
_DEPLOY_NAMES = frozenset({"dockerfile", "docker-compose.yml",
                           "docker-compose.yaml", "install.sh"})
_DEPLOY_SUFFIXES = (".service", ".timer", ".tf")
_DOCS_SUFFIXES = (".md", ".rst", ".mdx", ".txt")
_DOCS_DIRS = frozenset({"docs", "doc"})
_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part]


def _is_test_path(path: str) -> bool:
    parts = _parts(path)
    if not parts:
        return False
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    return (
        name.startswith("test_")
        or name.startswith("conftest.")
        or "_test." in name
        or ".spec." in name
    )


def _is_docs_path(path: str) -> bool:
    parts = _parts(path)
    if not parts:
        return False
    if any(part in _DOCS_DIRS for part in parts[:-1]):
        return True
    return parts[-1].lower().endswith(_DOCS_SUFFIXES)


def _is_ui_path(path: str) -> bool:
    parts = _parts(path)
    if not parts:
        return False
    if any(part in _UI_DIRS for part in parts[:-1]):
        return True
    return parts[-1].lower().endswith(_UI_SUFFIXES)


def _is_deploy_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(_DEPLOY_PREFIXES):
        return True
    parts = _parts(path)
    if not parts:
        return False
    name = parts[-1].lower()
    return name in _DEPLOY_NAMES or name.endswith(_DEPLOY_SUFFIXES)


def build_checklist(*, test_status: str | None,
                    changed_files: list[str] | None,
                    language_code: str = language.DEFAULT_LANGUAGE) -> dict:
    """Split the delivery's evidence into the two checklist columns.

    `test_status` is the PR head's CI check status (Issue #1613:
    `passed` / `failed: <names>` / `pending` / `-`); `changed_files` are
    the paths the delivered commits changed against the frozen base
    (None when the diff cannot be read). Column 1 is what the run
    actually verified; column 2 lists the dimensions the machine
    evidence cannot close -- empty only when the delivery really carries
    none (the target state).

    Only a passing CI result is evidence: with no PR, no checks, pending
    checks or a failed check the evidence item stays in column 2 and the
    gate holds.
    """
    column1: list[str] = []
    column2: list[str] = []
    if test_status == "passed":
        column1.append(language.text(
            language_code, "human_review.item_test_result",
            status=test_status,
        ))
        # No declared test command -- the suite is the
        # repository's own test suite (the agent infers the method).
        column1.append(language.text(
            language_code, "human_review.item_coverage",
            count=len(changed_files or []),
        ))
    else:
        column2.append(language.text(language_code, _EVIDENCE_ITEM))
    if changed_files is None:
        column2.append(language.text(language_code, _DIFF_ITEM))
    changed = changed_files or []
    if any(not _is_test_path(path) and not _is_docs_path(path)
           for path in changed):
        column2.append(language.text(language_code, _INTENT_ITEM))
    if any(_is_ui_path(path) for path in changed):
        column2.append(language.text(language_code, _UI_ITEM))
    if any(_is_deploy_path(path) for path in changed):
        column2.append(language.text(language_code, _DEPLOY_ITEM))
    return {"column1": column1, "column2": column2}


def render_checklist_comment(*, run_id: str, pr_url: str,
                             checklist: dict,
                             language_code: str = language.DEFAULT_LANGUAGE,
                             ) -> str:
    """Render the Issue comment: run marker, both columns, machine block.

    Hard constraints: no line may start with
    `Orbi review round ` (`review_rounds_so_far` counts it and the
    checklist would burn the bounded review budget); an empty column 2
    says so explicitly and does not require the label; the wording never
    claims a guarantee of direction. A non-empty column 2 (a person
    must confirm) ends with the community help line (Issue #1684).
    """
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    column1 = checklist["column1"]
    column2 = checklist["column2"]
    # The review instruction names the repository's own test
    # suite -- no declared test command exists to quote.
    lines = [
        f"<!-- orbi:run={run_id} -->",
        message("human_review.title"),
        "",
        message("human_review.method", pr_url=pr_url),
        "",
        message("human_review.column1"),
    ]
    if column1:
        lines.extend(f"- {item}" for item in column1)
    else:
        lines.append(f"- {message('human_review.column1_empty')}")
    lines.append("")
    if column2:
        lines.append(message("human_review.column2"))
        lines.extend(f"- {item}" for item in column2)
        lines.append("")
        lines.append(message(
            "human_review.confirm", label=HUMAN_REVIEW_LABEL,
            run_id=run_id,
        ))
    else:
        lines.append(message(
            "human_review.column2_empty", label=HUMAN_REVIEW_LABEL,
            run_id=run_id,
        ))
    lines += [
        "",
        f"<!-- {CHECKLIST_MARKER}",
        json.dumps(
            {
                "schema": _CHECKLIST_SCHEMA,
                "run_id": run_id,
                "column1": column1,
                "column2": column2,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "-->",
    ]
    if column2:
        # A person must confirm: this problem message ends with the
        # community help line (Issue #1684). An empty column 2 needs no
        # human, so it stays exactly as before.
        lines += ["", message("help.community")]
    return "\n".join(lines)
