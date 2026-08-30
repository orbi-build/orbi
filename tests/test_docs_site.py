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
    runner.RELEASE_LABEL,
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
    # Issue #139: the active Milestone claim scope (optional string).
    "active_milestone",
    "max_concurrency",
    "skills",
    "context_files",
    # Issue #119/#157: the optional Pi model selection keys (load_config
    # plus the committed example, commented out).
    "pi_provider",
    "pi_model",
    "pi_thinking",
    "pi_providers",
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


def test_docs_getting_started_documents_the_model_provider_configuration():
    """Issue #179: the getting-started article must take a new user
    through the model provider configuration with the real v0.2.0
    contract: the optional `pi_providers` file in Pi's `models.json`
    shape, the OpenAI-compatible boundary (local llama.cpp or any
    compatible API), the env-var API key reference (never a literal
    key in the docs), the gitignored env file the systemd unit loads,
    the fail-fast validation, and the explicit boundary that no
    per-provider integration is claimed as complete."""
    text = page_text("getting-started")
    # The selection keys are documented as real config fields.
    for field in ("pi_providers", "pi_provider", "pi_model", "pi_thinking"):
        assert field in text, f"getting-started must document {field}"
    # The provider file uses Pi's models.json shape.
    assert "models.json" in text, ("the provider file must be named in Pi's models.json shape")
    assert "baseUrl" in text and "apiKey" in text, (
        "the provider file contract must name baseUrl and apiKey"
    )
    # The key is an env-var reference, never a literal in the docs.
    assert "$GROQ_API_KEY" in text, (
        "the OpenAI-compatible example must reference the key as an env var"
    )
    assert ".muyan-pilot/env" in text, (
        "the key value must live in the gitignored env file"
    )
    assert "EnvironmentFile" in text, (
        "the systemd unit's EnvironmentFile must be named"
    )
    # The local llama.cpp boundary with the real server facts.
    assert "llama.cpp" in text.lower() or "llama.cpp" in text, (
        "the local provider path must name llama.cpp"
    )
    assert "127.0.0.1:8080/v1" in text, (
        "the local example must use the real llama.cpp OpenAI-compatible URL"
    )
    assert "--alias" in text, ("the model id must be tied to the llama.cpp --alias")
    # The honest boundary: no per-provider integration is claimed.
    assert "OpenAI-compatible" in text, (
        "the documented provider boundary must be named"
    )
    assert "guarantee" in text.lower() or "tested" in text.lower(), (
        "the article must state what is and is not tested/guaranteed"
    )
    # The fail-fast validation is documented (missing key env var).
    assert "fail" in text.lower(), (
        "the provider validation must be described as fail-fast"
    )


def test_docs_getting_started_documents_the_full_chain_and_troubleshooting():
    """Issue #179: the smoke walkthrough must verify the FULL chain —
    picked up, implemented, tested, PR opened, independently reviewed,
    merged — with concrete success criteria per stage (the real journal
    lines and the real `gh` checks), and the article must carry a
    troubleshooting section with the real failure scenes."""
    text = page_text("getting-started")
    # The chain stages with their real evidence.
    assert "ai-pr-opened" in text, "the chain must reach ai-pr-opened"
    assert "ai-merged" in text, "the chain must reach ai-merged"
    assert "result=pr_opened" in text, ("the PR stage must cite the real run_end journal line")
    assert "verdict=pass" in text, ("the review stage must cite the real review journal line")
    assert "merged pr=" in text, ("the merge stage must cite the real merged journal line")
    assert "Fixes #" in text, "the PR body contract must be part of the success criteria"
    assert "gh pr view" in text, ("the terminal state must be checked with the real gh command")
    assert "mergedAt" in text, ("the gh PR check must use the real mergedAt field")
    # The failure scene.
    assert "ai-blocked" in text, ("the failure outcome must be named")
    assert "run_failed" in text, ("the failure must cite the real journal line")
    # The troubleshooting section.
    assert "Troubleshooting" in text, ("the article must carry a troubleshooting section")


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
    `ai-blocked` with no infinite retry). The Epic skip is implemented
    behavior (Issue #93: the claim scan never claims an `ai-epic`
    Issue and logs `epic_not_claimed`)."""
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
    assert "muyan-pilot@.timer" in text, (
        "operations must document the timer template"
    )
    assert "muyan-pilot@1.timer" in text, (
        "operations must document the two timer instances (Issue #149)"
    )
    assert "OnCalendar=*-*-* *:00/5" in text, (
        "operations must document the 5-minute idle polling interval"
    )
    assert "journalctl" in text, "operations must show the journal command"
    assert "muyan-pilot" in text, "operations must name the CLI"
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


def test_docs_document_the_minimal_implementation_principle():
    """Issue #118: the contributing page documents Issue granularity and
    the KISS/LEAN minimal implementation principle — the smallest
    complete change for the acceptance criteria, the forbidden expansion
    list, 如无必要勿增实体 (every new entity maps to an acceptance
    criterion), the simpler-design rule, and the unchanged MVP
    boundary."""
    text = page_text("contributing")
    assert "issue granularity" in text.lower(), (
        "contributing must document the Issue granularity"
    )
    assert "kiss" in text.lower(), "contributing must name KISS"
    assert "lean" in text.lower(), "contributing must name LEAN"
    assert "smallest complete" in text.lower(), (
        "contributing must state the smallest complete change principle"
    )
    assert "speculative" in text.lower(), (
        "contributing must forbid speculative features"
    )
    assert "no-benefit abstraction" in text.lower(), (
        "contributing must forbid no-benefit abstractions"
    )
    assert "future-proofing" in text.lower(), (
        "contributing must forbid future-proofing"
    )
    assert "scope expansion" in text.lower(), (
        "contributing must forbid scope expansion"
    )
    assert "如无必要勿增实体" in text, (
        "contributing must state 如无必要勿增实体"
    )
    assert "acceptance criterion" in text.lower(), (
        "contributing must tie every new entity to an acceptance criterion"
    )
    assert "fewer concepts, fewer files" in text.lower(), (
        "contributing must prefer the simpler design"
    )


def test_docs_document_the_uv_prerequisite_boundary():
    """Issue #156: the docs must draw the initialization boundary — the
    USER installs git/gh/python3/systemd user session/uv (uv with the
    official installer command), while `muyan-pilot setup` only checks
    auth, repos, the CLI editable install, units and timers; setup never
    installs system packages, never installs gh, never runs
    `gh auth login`. The setup page must list uv in the command step and
    show the uv-missing failure example."""
    started = page_text("getting-started")
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in started, (
        "getting-started must give the official uv install command "
        "(verified against the uv docs)"
    )
    assert "gh auth login" in started, (
        "getting-started must state that setup never runs gh auth login "
        "(the user logs in)"
    )
    setup = page_text("setup")
    assert "required command missing: uv" in setup, (
        "setup must show the uv-missing setup_failed example"
    )
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in setup, (
        "setup's uv-missing failure example must carry the actionable "
        "install guidance"
    )


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


def test_docs_document_the_implemented_epic_skip():
    """Issue #93: the runner skips `ai-epic` Issues in the claim scan
    with the structured `epic_not_claimed` line — the workflow page
    must document that real behavior (the docs may name a concept only
    when the code carries it)."""
    text = page_text("workflow")
    assert "ai-epic" in text, (
        "workflow must name the ai-epic label the claim scan skips"
    )
    assert "epic_not_claimed" in text, (
        "workflow must document the structured epic_not_claimed log line"
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
