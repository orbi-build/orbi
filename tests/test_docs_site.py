"""Documentation site contract for the v0.1.0 open-source release (Issue #104
+ Issue #116).

The open-source docs live in `docs/` (Mintlify config `docs.json` plus
`*.mdx` pages) and are the single source of truth: Mintlify only builds,
searches and hosts them (repository default branch, documentation path
`/docs`). The English pages sit at the `docs/` root and the Chinese
translation lives in `docs/zh/` (the verified Mintlify i18n layout: the
`navigation.languages` array, default language unprefixed, translated page
paths prefixed with the language directory). These tests fail when the
docs are missing, when the Mintlify navigation drifts from the actual
pages, when a required topic disappears (problem/scenarios/MVP boundary,
install prerequisites, config fields, Issue→PR workflow, operations
commands, labels/run marker/Epic/Release task/P0, security boundary,
test+coverage commands, contributing, smoke walkthrough), when a doc
references a label or config field the implementation does not have, or
when a doc (in any language) carries a personal absolute path, an
unimplemented feature, or the stale 15-minute timer.
"""
import json
import re
from pathlib import Path

import bootstrap_runner as runner

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DOCS_CONFIG = DOCS_DIR / "docs.json"
README = REPO_ROOT / "README.md"
EXAMPLE_CONFIG = REPO_ROOT / ".muyan-pilot.example.toml"

# The pages the v0.1.0 docs must ship (one page per topic).
REQUIRED_PAGES = (
    "index",
    "getting-started",
    "workflow",
    "operations",
    "security",
    "testing",
    "contributing",
    "optional-kv-cache",
)

# Every `ai-*` label the docs may mention must exist in the runner's label
# set (same contract as tests/test_docs_labels.py for README/AGENTS).
KNOWN_LABELS = frozenset({
    "ai-ready",
    "ai-epic",
    runner.IN_PROGRESS_LABEL,
    runner.PR_OPENED_LABEL,
    runner.FIX_NEEDED_LABEL,
    runner.MERGED_LABEL,
    runner.BLOCKED_LABEL,
})

LABEL_PATTERN = re.compile(r"\bai-[a-z][a-z-]*\b")

# Config fields the implementation understands (bootstrap_runner.load_config
# plus the committed example). A docs field table may document exactly this
# set — no invented field may sneak in.
KNOWN_CONFIG_FIELDS = frozenset({
    "source_repos",
    "repo_dir",
    "workspace_root",
    "prompt",
    "prompt_review",
    "base_branch",
    "max_concurrency",
    "skills",
    "context_files",
})

CONFIG_FIELD_PATTERN = re.compile(
    r"^\s*([a-z][a-z_]+)\s*=", re.MULTILINE
)

# Personal absolute paths must never appear in the docs (acceptance: the
# commands work at any clone path).
PERSONAL_PATH_PATTERN = re.compile(r"/home/[a-z0-9_]+|/Users/[a-z0-9_]+")


def docs_files() -> list[Path]:
    """All committed Markdown/MDX pages under docs/ (sorted, stable).

    Includes the language subdirectories (Issue #116: `docs/zh/`), so
    every global content check (labels, personal paths, stale timer,
    unimplemented features) covers every language.
    """
    assert DOCS_DIR.is_dir(), f"missing docs directory: {DOCS_DIR}"
    files = sorted(
        path for path in DOCS_DIR.rglob("*")
        if path.is_file() and path.suffix in (".md", ".mdx")
    )
    assert files, "docs/ contains no Markdown/MDX pages"
    return files


def page_text(slug: str) -> str:
    path = DOCS_DIR / f"{slug}.mdx"
    assert path.is_file(), f"missing docs page: {path}"
    return path.read_text(encoding="utf-8")


def load_docs_config() -> dict:
    assert DOCS_CONFIG.is_file(), f"missing Mintlify config: {DOCS_CONFIG}"
    config = json.loads(DOCS_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(config, dict), "docs.json is not a JSON object"
    return config


def navigation_languages(config: dict) -> list[dict]:
    """The Mintlify i18n navigation (Issue #116, verified against the
    official i18n guide + docs.json schema reference): the `languages`
    array, one entry per language, each with its own groups."""
    navigation = config.get("navigation")
    assert isinstance(navigation, dict) and navigation, (
        "docs.json has no navigation section"
    )
    languages = navigation.get("languages")
    assert isinstance(languages, list) and languages, (
        "docs.json navigation must use the languages array (Mintlify i18n)"
    )
    return languages


def language_page_entries(config: dict) -> list[tuple[str, list[str]]]:
    """(language code, page paths) for every navigation language entry.

    The default language lists its pages without a prefix (files at the
    docs root); every other language prefixes its page paths with the
    language directory (e.g. `zh/index` -> `docs/zh/index.mdx`).
    """
    entries: list[tuple[str, list[str]]] = []
    for lang in navigation_languages(config):
        assert isinstance(lang, dict) and lang.get("language"), (
            f"navigation language entry without a code: {lang!r}"
        )
        code = str(lang["language"])
        pages: list[str] = []
        groups = lang.get("groups")
        assert isinstance(groups, list) and groups, (
            f"navigation language {code!r} has no groups"
        )
        for group in groups:
            assert isinstance(group, dict) and group.get("group"), (
                f"navigation group without a name: {group!r}"
            )
            group_pages = group.get("pages")
            assert isinstance(group_pages, list) and group_pages, (
                f"navigation group {group.get('group')!r} has no pages"
            )
            pages.extend(group_pages)
        entries.append((code, pages))
    return entries


def test_docs_config_is_a_mintlify_config_named_muyan_pilot():
    config = load_docs_config()
    schema = str(config.get("$schema", ""))
    assert "mintlify" in schema.lower(), (
        f"docs.json must point at the Mintlify schema, got: {schema!r}"
    )
    assert config.get("name") == "Muyan Pilot", (
        f"docs.json name must be 'Muyan Pilot', got: {config.get('name')!r}"
    )
    # The official Mintlify schema (mintlify.com/docs.json) and settings
    # reference mark `theme` as required: a config without it does not
    # validate against its own declared schema, so the site build contract
    # would break. Pin the value the build expects.
    assert config.get("theme") == "mint", (
        f"docs.json must set the required Mintlify theme, "
        f"got: {config.get('theme')!r}"
    )


def test_docs_navigation_uses_the_verified_i18n_languages_layout():
    """Issue #116: the navigation is the Mintlify i18n `languages` array
    (verified against the official i18n guide + docs.json schema
    reference): English is the default language (listed first, its pages
    unprefixed at the docs root), Chinese is `zh` (a supported code) with
    its pages prefixed `zh/` (files under `docs/zh/`), and no page path
    appears in more than one language (the official warning: duplicating
    paths across languages is undefined behavior)."""
    config = load_docs_config()
    entries = language_page_entries(config)
    codes = [code for code, _pages in entries]
    assert codes[0] == "en", (
        f"the default language must be English (first entry), got: {codes!r}"
    )
    assert "zh" in codes, (
        f"the Chinese language must be `zh` (Mintlify supported code), got: {codes!r}"
    )
    assert len(codes) == len(set(codes)), f"duplicate language codes: {codes!r}"
    en_pages = dict(entries)["en"]
    zh_pages = dict(entries)["zh"]
    assert all(not page.startswith("en/") for page in en_pages), (
        f"default-language pages must be unprefixed (docs root), got: {en_pages!r}"
    )
    assert zh_pages, "the Chinese navigation lists no pages"
    assert all(page.startswith("zh/") for page in zh_pages), (
        f"translated pages must be prefixed with the language directory, got: {zh_pages!r}"
    )
    overlap = set(en_pages) & set(zh_pages)
    assert not overlap, f"page paths duplicated across languages: {sorted(overlap)}"


def test_docs_navigation_matches_the_actual_pages_exactly():
    """KISS: one group per topic per language, every page listed once, no
    orphan page outside the navigation and no navigation entry without a
    file (Issue #116: the i18n `languages` layout)."""
    config = load_docs_config()
    entries = language_page_entries(config)
    listed: list[str] = []
    for _code, pages in entries:
        listed.extend(pages)
    assert len(listed) == len(set(listed)), (
        f"navigation lists a page twice: {sorted(set(p for p in listed if listed.count(p) > 1))}"
    )
    actual = {str(path.relative_to(DOCS_DIR)).removesuffix(".mdx").removesuffix(".md")
              for path in docs_files()}
    missing = set(listed) - actual
    assert not missing, f"navigation references missing pages: {sorted(missing)}"
    orphans = actual - set(listed)
    assert not orphans, f"pages missing from the navigation: {sorted(orphans)}"


def test_docs_ship_every_required_topic_page():
    for slug in REQUIRED_PAGES:
        assert (DOCS_DIR / f"{slug}.mdx").is_file(), f"missing docs page: {slug}.mdx"


def test_docs_describe_problem_scenarios_and_mvp_boundary():
    """The overview must say what the project solves, when to use it, and
    the explicit MVP boundary (GitHub Issues are the only state store; no
    database, queue, daemon loop or fallback)."""
    text = page_text("index")
    assert "GitHub Issue" in text, "overview must name the GitHub Issue task pool"
    assert "MVP" in text, "overview must state the MVP boundary"
    for forbidden in ("database", "queue", "daemon"):
        assert forbidden in text.lower(), (
            f"overview must name the MVP boundary: no {forbidden}"
        )


def test_docs_document_install_prerequisites():
    """Core prerequisites: Python (the production minor version, same as
    CI), Pi, Git + GitHub CLI, systemd, and a working OpenAI-compatible
    model endpoint. The KV cache proxy is NOT a core prerequisite."""
    text = page_text("getting-started")
    assert "Python" in text
    assert "3.14" in text, "prerequisites must pin the production Python minor version"
    assert "Pi" in text
    assert "git" in text.lower()
    assert "gh" in text, "prerequisites must name the GitHub CLI (gh)"
    assert "systemd" in text.lower()
    assert "OpenAI-compatible" in text, (
        "prerequisites must name the OpenAI-compatible model endpoint"
    )
    assert "local-llm-kv-cache" not in text or "optional" in text.lower() or "可选" in text, (
        "the KV cache proxy must not be presented as a core prerequisite"
    )


def test_docs_config_field_table_matches_the_implementation():
    """Every config field the docs document must exist in the committed
    example (and therefore in load_config); no invented field."""
    text = page_text("getting-started")
    documented = set(CONFIG_FIELD_PATTERN.findall(text))
    unknown = documented - KNOWN_CONFIG_FIELDS
    assert not unknown, f"docs document unknown config fields: {sorted(unknown)}"
    example = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    example_fields = set(CONFIG_FIELD_PATTERN.findall(example))
    missing = example_fields - documented
    assert not missing, (
        f"docs field table misses fields of the committed example: {sorted(missing)}"
    )


def test_docs_document_the_full_issue_to_merge_workflow():
    """The workflow page must cover the complete automatic chain (claim →
    worktree → implement → test → PR → independent review with in-session
    fix → merge), the PR body contract, and the run marker."""
    text = page_text("workflow")
    for state in (
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked",
    ):
        assert state in text, f"workflow page misses the {state} state"
    assert "REVIEW_VERDICT" in text, "workflow must document the review verdict"
    assert "Fixes #" in text, "workflow must document the PR body Fixes #N contract"
    assert "muyan-pilot:run=" in text, "workflow must document the run marker"


def test_docs_document_labels_run_marker_epic_release_task_and_p0():
    """The workflow page must give the basic explanation of the project's
    GitHub workflow vocabulary: the delivery labels, the run marker, Epic
    coordination issues (ai-epic), Release tasks, and the P0 pickup
    order (the plain `p0` label, Issue #101: `ai-ready`+`p0` before
    `ai-ready`+`bug` before plain `ai-ready`, failed P0 enters
    `ai-blocked` with no infinite retry). Epic/Release task/P0 are
    workflow concepts — the docs must not claim runner features that are
    not implemented yet (Issue #93/#98 are still open)."""
    text = page_text("workflow")
    assert "ai-epic" in text, "workflow must explain Epic issues (ai-epic)"
    assert "Release" in text, "workflow must explain Release tasks"
    assert "P0" in text, "workflow must explain the P0 priority"
    # Issue #101: the P0 explanation names the plain `p0` label and the
    # fixed pickup order (p0 before bug before plain).
    assert "`p0`" in text, "P0 explanation must name the p0 label"
    assert "`ai-ready`+`p0`" in text, "P0 explanation must show the order"
    assert "bug" in text.lower(), "P0 explanation must name the bug label"
    # The docs only reference labels that exist in the runner's label set.
    mentioned = set(LABEL_PATTERN.findall(text))
    unknown = mentioned - KNOWN_LABELS
    assert not unknown, f"workflow references unknown labels: {sorted(unknown)}"


def test_docs_document_operations_commands():
    """Operations must cover first start, the 5-minute timer, journal
    logs, the status/session/add CLI, worktrees and failure recovery —
    with the commands the implementation actually provides."""
    text = page_text("operations")
    assert "muyan-pilot.timer" in text
    assert "OnCalendar=*-*-* *:00/5" in text, (
        "operations must document the 5-minute idle polling interval"
    )
    assert "journalctl" in text, "operations must show the journal command"
    assert "muyan_pilot.py" in text, "operations must name the CLI"
    for command in ("status", "session", "add"):
        assert command in text, f"operations must document the {command} command"
    assert "worktree" in text.lower(), "operations must explain the task worktree"
    assert "ai-blocked" in text, "operations must document failure recovery (ai-blocked)"
    assert "ExecStartPre" in text, "operations must document the code-update preflight"


def test_docs_document_the_security_boundary():
    """Security: the AI never merges or pushes the protected branch (the
    Runner is the only merge actor), the GitHub token is a local secret,
    and the local KV/session files are local state that never leaves the
    machine."""
    text = page_text("security")
    assert "merge" in text.lower()
    assert "protected" in text.lower() or "保护分支" in text, (
        "security must name the protected branch boundary"
    )
    assert "token" in text.lower(), "security must cover the GitHub token"
    assert "KV" in text, "security must cover the local KV/session files"


def test_docs_document_tests_and_coverage_commands():
    """Testing must show the exact contract commands (full pytest with
    branch coverage + 100% report) and the remote CI gate."""
    text = page_text("testing")
    assert "coverage run --branch -m pytest tests/" in text, (
        "testing must show the contract coverage-run command"
    )
    assert "coverage report" in text, "testing must show the coverage report command"
    assert "100%" in text, "testing must state the 100% line/branch coverage requirement"
    assert "GitHub Actions" in text, "testing must name the remote CI gate"


def test_docs_document_contributing_paths():
    """Contributing must show how to create an Issue (with ai-ready),
    report a bug, and submit a PR (feature branch, Fixes #N, one PR)."""
    text = page_text("contributing")
    assert "ai-ready" in text, "contributing must show how to dispatch an Issue"
    assert "bug" in text.lower(), "contributing must show how to report a bug"
    assert "PR" in text, "contributing must show how to submit a PR"
    assert "Fixes #" in text, "contributing must document the PR body contract"


def test_docs_document_the_git_transport_boundary():
    """Issue #114: the docs must document the two-channel boundary —
    git data operations over SSH (including workflow file pushes),
    GitHub API operations on the gh token, the pre-start transport
    check without an HTTPS fallback, and the human-run setup migration
    of an existing HTTPS remote."""
    operations = page_text("operations")
    assert "transport_check_failed" in operations, (
        "operations must document the pre-start transport check"
    )
    assert "no HTTPS fallback" in operations, (
        "operations must state that a broken transport never falls back "
        "to HTTPS"
    )
    setup = page_text("setup")
    assert "git remote set-url origin" in setup, (
        "setup must document the HTTPS-to-SSH migration command"
    )
    assert "ssh_unreachable" in setup, (
        "setup must document the SSH connectivity failure"
    )
    workflow = page_text("workflow")
    assert "SSH" in workflow, (
        "workflow must document the SSH git transport"
    )
    security = page_text("security")
    assert "SSH key" in security, (
        "security must document the SSH key as the git-data credential"
    )


def test_docs_smoke_walkthrough_is_clone_path_independent():
    """The smoke walkthrough must use only relative paths / clone-local
    paths so it works at any clone location — no personal absolute path
    anywhere in the docs (acceptance criterion)."""
    text = page_text("getting-started")
    assert "smoke" in text.lower(), "getting-started must contain the smoke walkthrough"
    for path in docs_files():
        content = path.read_text(encoding="utf-8")
        match = PERSONAL_PATH_PATTERN.search(content)
        assert match is None, (
            f"{path.name} contains a personal absolute path: {match.group(0)}"
        )


def test_docs_do_not_document_unimplemented_runner_features():
    """Acceptance: no unimplemented feature. The runner does not skip
    ai-epic Issues yet (Issue #93 open) and has no P0 priority field
    (Issue #101 open) — the docs may name the concepts but must not
    claim that behavior from the current code."""
    text = page_text("workflow").lower()
    assert "epic_not_claimed" not in text, (
        "the runner does not log epic_not_claimed yet (Issue #93 is open)"
    )
    operations = page_text("operations").lower()
    assert "priority" not in operations or "bug" in operations, (
        "operations must not claim a priority queue that does not exist"
    )


def test_docs_document_the_five_minute_timer():
    """Issue #51: the idle polling interval is 5 minutes. The operations
    pages (EN and ZH) must document the real OnCalendar, and no stale
    15-minute OnCalendar may remain in any doc page."""
    en = page_text("operations")
    assert "OnCalendar=*-*-* *:00/5" in en, (
        "English operations must document the 5-minute timer"
    )
    for path in docs_files():
        content = path.read_text(encoding="utf-8")
        assert "00/15" not in content, (
            f"{path.name} carries the stale 15-minute OnCalendar"
        )


def test_docs_kv_cache_page_matches_the_upstream_port_contract():
    """The upstream proxy's CODE default port is 8081
    (cache_proxy.py: os.environ.get("PI_LLAMA_CACHE_PORT", "8081"));
    18082 is the value the committed systemd unit sets (verified against
    the upstream repo). The docs must keep the two apart: the agent-
    facing port 18082 stays documented, but the Configuration table must
    not claim 18082 as the env var's default."""
    text = page_text("optional-kv-cache")
    assert "18082" in text, (
        "the docs must keep the unit's agent-facing port 18082"
    )
    row = next(
        (line for line in text.splitlines()
         if line.startswith("| `PI_LLAMA_CACHE_PORT`")),
        None,
    )
    assert row is not None, "missing the PI_LLAMA_CACHE_PORT row"
    default_cell = row.strip("|").split("|")[1].strip()
    assert "18082" not in default_cell.split("(")[0], (
        f"the docs claim 18082 as the proxy code default: {row!r}"
    )


def test_readme_keeps_the_docs_site_entry():
    """The root README stays the GitHub homepage and must carry the
    documentation site entry (Mintlify default subdomain for this repo,
    with the custom-domain caveat)."""
    text = README.read_text(encoding="utf-8")
    assert "muyan-pilot.mintlify.site" in text, (
        "README must link the Mintlify documentation site"
    )
    assert "docs/" in text, "README must point at the in-repo docs/ source"


def test_docs_only_reference_existing_labels_everywhere():
    for path in docs_files():
        content = path.read_text(encoding="utf-8")
        mentioned = set(LABEL_PATTERN.findall(content))
        unknown = mentioned - KNOWN_LABELS
        assert not unknown, (
            f"{path.name} references unknown labels: {sorted(unknown)}"
        )
