"""The human acceptance gate's checklist (Issue #763).

The gate's carrier is the human-only label `ai-human-review`
(`orbi.delivery_labels`); this module is its readable face: the two
columns of the acceptance checklist posted to the Issue when a delivery
completes, plus the machine-parsable block a future migration tool reads
when a column-2 item becomes machine-verifiable (the artifact shape
stays, the items migrate).

The split between the columns is DATA-DRIVEN per delivery — derived
from the delivered diff paths and the run's test evidence — never a
hardcoded "human-only items" list: an item leaves column 2 when a
machine-verification dimension covers it, and an empty column 2 is the
product's target state ("no human intervention needed", no label
required). Every function here is pure: a deterministic function of the
delivery's evidence.

Wording discipline (orbi-website#128): the checklist never claims a
guarantee of correctness or direction — column 1 is evidence of what
the run verified, column 2 is the minimal set a person still confirms.
"""
import json

from orbi.delivery_labels import HUMAN_REVIEW_LABEL

# The machine-parsable block marker (an HTML comment — GitHub renders
# nothing of it; a migration tool greps it). Schema 1: one JSON object
# with the run id and both columns verbatim.
CHECKLIST_MARKER = "orbi:human-review"
_CHECKLIST_SCHEMA = 1

# Column-2 items (Issue #763 section 4: the machine-unverifiable
# minimum). Each fires on a data condition of the delivered diff, not
# on a repo-agnostic "humans must check this" template.
_INTENT_ITEM = (
    "业务意图判断：自动化测试只能证明实现与测试一致，不能证明测试与意图一致"
    "——本次改动是否符合原始预期，需要人确认"
)
_UI_ITEM = (
    "UI 视觉与文案：测试断言不到渲染效果与措辞，需要人在真实界面确认"
)
_DEPLOY_ITEM = (
    "外部环境验证：部署 / CI / 监控类改动要在真实环境验证，本机测试覆盖不到"
)
_EVIDENCE_ITEM = (
    "测试证据缺失：没有可读的测试结果记录，本次交付的自动化验证程度未知"
)
_DIFF_ITEM = (
    "改动文件清单不可读：无法确定本次交付实际改了什么，验证范围未知"
)

_UI_SUFFIXES = (
    ".html", ".htm", ".css", ".scss", ".less",
    ".jsx", ".tsx", ".vue", ".svelte",
)
_UI_DIRS = frozenset({"templates", "static", "frontend", "web", "ui",
                      "assets"})
_DEPLOY_PREFIXES = (".github/", "systemd/", "monitoring/", "deploy/",
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


def build_checklist(*, test_result: str | None,
                    changed_files: list[str] | None,
                    test_command: str | None) -> dict:
    """Split the delivery's evidence into the two checklist columns.

    `test_result` is the worktree's `.orbi/test.log` summary (or None);
    `changed_files` are the paths the delivered commits changed against
    the frozen base (None when the diff cannot be read). Column 1 is
    what the run actually verified; column 2 lists the dimensions the
    machine evidence cannot close — empty only when the delivery really
    carries none (the target state).
    """
    column1: list[str] = []
    column2: list[str] = []
    if test_result:
        column1.append(f"测试结果：{test_result}")
        suite = test_command or "仓库测试套件"
        column1.append(
            f"覆盖层：{suite} —— 自动化断言覆盖到代码与接口层；"
            f"改动共 {len(changed_files or [])} 个文件"
        )
    if test_result is None:
        column2.append(_EVIDENCE_ITEM)
    if changed_files is None:
        column2.append(_DIFF_ITEM)
    changed = changed_files or []
    if any(not _is_test_path(path) and not _is_docs_path(path)
           for path in changed):
        column2.append(_INTENT_ITEM)
    if any(_is_ui_path(path) for path in changed):
        column2.append(_UI_ITEM)
    if any(_is_deploy_path(path) for path in changed):
        column2.append(_DEPLOY_ITEM)
    return {"column1": column1, "column2": column2}


def render_checklist_comment(*, run_id: str, pr_url: str,
                             test_command: str | None,
                             checklist: dict) -> str:
    """Render the Issue comment: run marker, both columns, machine block.

    Hard constraints (Issue #763): no line may start with
    `Orbi review round ` (`review_rounds_so_far` counts it and the
    checklist would burn the bounded review budget); an empty column 2
    says 无需人工介入 explicitly and does not require the label; the
    wording never claims a guarantee of direction.
    """
    column1 = checklist["column1"]
    column2 = checklist["column2"]
    suite = test_command or "仓库测试套件"
    lines = [
        f"<!-- orbi:run={run_id} -->",
        "**Orbi human review checklist**",
        "",
        "复核方式：阅读 PR 的完整 diff（"
        f"{pr_url}），在本地跑仓库测试（`{suite}`），再确认下面两栏。",
        "",
        "栏一 · AI 已验证（证据，不是待办）：",
    ]
    if column1:
        lines.extend(f"- {item}" for item in column1)
    else:
        lines.append("- （无可读的自动化验证证据）")
    lines.append("")
    if column2:
        lines.append("栏二 · 需要人确认（机器验不了的最小集合）：")
        lines.extend(f"- {item}" for item in column2)
        lines.append("")
        lines.append(
            f"确认无误后给本 Issue 打 `{HUMAN_REVIEW_LABEL}` 标签，"
            "Orbi 会在下一个 tick 继续评审与合并；在此之前交付在此等待"
            f"（run_id={run_id}）。"
        )
    else:
        lines.append(
            "栏二 · 需要人确认：**无需人工介入** —— 本次交付没有识别出"
            f"机器验不了的条目，不要求 `{HUMAN_REVIEW_LABEL}` 标签"
            f"（run_id={run_id}）。"
        )
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
    return "\n".join(lines)
