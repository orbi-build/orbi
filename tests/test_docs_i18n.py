"""Chinese documentation contract (Issue #116).

The Chinese docs live in `docs/zh/` (the verified Mintlify i18n layout:
same structure as the default English pages at the `docs/` root) and keep
the SAME single source of truth as the English pages — the implementation
facts (labels, config fields, commands, ports, the 5-minute timer, the
P0 pickup order, the PR body contract, the git transport boundary, the
optional KV cache proxy) are asserted in the Chinese pages exactly as the
English pages carry them. These tests fail when the Chinese entry or a
required topic page is missing, when the EN/ZH page sets drift (one
language documents a topic the other does not), when a Chinese page
references a label or config field the implementation does not have, or
when the README stops pointing at the Chinese docs entry.
"""
import re
from pathlib import Path

import muyan_pilot.runner as runner

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
ZH_DIR = DOCS_DIR / "zh"
README = REPO_ROOT / "README.md"
EXAMPLE_CONFIG = REPO_ROOT / ".muyan-pilot.example.toml"

# One page per English topic page (same structure, same slugs).
REQUIRED_ZH_PAGES = (
    "index",
    "getting-started",
    "setup",
    "workflow",
    "operations",
    "security",
    "testing",
    "contributing",
    "optional-kv-cache",
)

# Every `ai-*` label the Chinese docs may mention must exist in the
# runner's label set (same contract as the English pages).
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
# plus the committed example) — the Chinese field table may document
# exactly this set, no invented field.
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
    # Issue #228: the configurable model_wait dead-request threshold
    # (finite positive number, default 1800 s).
    "model_wait_dead_seconds",
    # Issue #233: the /slots swallowed-model-request probe (optional
    # http(s) URL + sustained-idle grace, default 60 s).
    "model_wait_probe_url",
    "model_wait_probe_seconds",
    "skills",
    "context_files",
    # Issue #119/#157: the optional Pi model selection keys (load_config
    # plus the committed example, commented out).
    "pi_provider",
    "pi_model",
    "pi_thinking",
    "pi_providers",
})

CONFIG_FIELD_PATTERN = re.compile(r"^\s*([a-z][a-z_]+)\s*=", re.MULTILINE)


def zh_page_text(slug: str) -> str:
    path = ZH_DIR / f"{slug}.mdx"
    assert path.is_file(), f"missing Chinese docs page: {path}"
    return path.read_text(encoding="utf-8")


def zh_page_stems() -> set[str]:
    assert ZH_DIR.is_dir(), f"missing Chinese docs directory: {ZH_DIR}"
    return {path.stem for path in ZH_DIR.glob("*.mdx")}


def en_page_stems() -> set[str]:
    return {
        path.stem
        for path in DOCS_DIR.iterdir()
        if path.is_file() and path.suffix in (".md", ".mdx")
    }


def test_chinese_docs_entry_and_every_required_topic_page_exist():
    """A Chinese user starting from the docs home must find the entry
    (index) and every core topic (Issue #116 requirement list)."""
    for slug in REQUIRED_ZH_PAGES:
        assert (ZH_DIR / f"{slug}.mdx").is_file(), (
            f"missing Chinese docs page: zh/{slug}.mdx"
        )


def test_chinese_and_english_page_sets_stay_the_same_source_of_truth():
    """No topic may exist in only one language: the page sets must be
    identical, so a reader in either language sees the same coverage and
    the two versions cannot drift apart silently."""
    assert zh_page_stems() == en_page_stems(), (
        f"EN/ZH page sets differ: only-en={sorted(en_page_stems() - zh_page_stems())} "
        f"only-zh={sorted(zh_page_stems() - en_page_stems())}"
    )


def test_chinese_homepage_carries_a_real_frontmatter_title():
    """Issue #128: the zh language index page has no filename-derived
    title the renderer can use, so without a frontmatter `title` the
    renderer falls back to the capitalized language code — the live
    site showed "Zh" in the sidebar, breadcrumb and page header (and
    `/zh.md` injected `# Zh` before the content). The frontmatter
    `title` is the documented page-metadata mechanism (official
    Mintlify i18n guide). The title must be a real, natural title that
    names the project — never the internal language code."""
    text = zh_page_text("index")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None, (
        "zh/index.mdx must start with YAML frontmatter (page metadata)"
    )
    title_match = re.search(
        r'^title:\s*["\']?([^"\'\n]+?)["\']?\s*$',
        match.group(1),
        re.MULTILINE,
    )
    assert title_match is not None, "frontmatter must set a title"
    title = title_match.group(1).strip()
    assert title, "the frontmatter title must not be empty"
    assert title.lower() != "zh", (
        f"the title must not be the internal language code, got: {title!r}"
    )
    assert "Orbi" in title, (
        "the Chinese home title must name the project (Orbi, the "
        f"v0.3.0 rebrand name), got: {title!r}"
    )


def test_english_homepage_keeps_its_existing_title_behavior():
    """Issue #128 scope: the English page behavior is unaffected — the
    English index keeps no frontmatter title (the renderer keeps its
    pre-Issue-128 filename fallback "Index")."""
    text = (DOCS_DIR / "index.mdx").read_text(encoding="utf-8")
    assert not text.startswith("---"), (
        "the English index must not gain a frontmatter title "
        "(English behavior stays unchanged)"
    )


def test_chinese_overview_names_the_task_pool_and_mvp_boundary():
    """The Chinese overview (index) must say what the project is (GitHub
    Issue task pool) and the explicit MVP boundary (no database/queue/
    daemon) — same facts as the English overview."""
    text = zh_page_text("index")
    assert "GitHub Issue" in text, "overview must name the GitHub Issue task pool"
    assert "MVP" in text, "overview must state the MVP boundary"
    for forbidden in ("数据库", "队列", "daemon"):
        assert forbidden in text, (
            f"overview must name the MVP boundary (no {forbidden})"
        )


def test_chinese_getting_started_documents_prerequisites_and_smoke():
    """A Chinese user must be able to complete the install-prerequisite
    check, configuration, first start and the minimal smoke from the
    docs home (acceptance): the same facts as the English page —
    production Python minor version, Pi, git + gh, systemd, the
    OpenAI-compatible endpoint, the optional (not core) KV proxy, and the
    smoke walkthrough with the real commands."""
    text = zh_page_text("getting-started")
    assert "Python" in text
    assert "3.14" in text, "prerequisites must pin the production Python minor version"
    assert "Pi" in text
    assert "git" in text.lower()
    assert "gh" in text, "prerequisites must name the GitHub CLI (gh)"
    assert "systemd" in text.lower()
    assert "OpenAI-compatible" in text, (
        "prerequisites must name the OpenAI-compatible model endpoint"
    )
    assert "local-llm-kv-cache" not in text or "可选" in text, (
        "the KV cache proxy must not be presented as a core prerequisite"
    )
    assert "smoke" in text.lower(), "getting-started must contain the smoke walkthrough"
    assert "muyan-pilot add" in text, "smoke must use the real add command"
    assert (
        "PYTHONPATH=src python3 -m muyan_pilot.runner "
        "--config muyan-pilot.toml"
    ) in text, "smoke must run the real one-tick command (Issue #168 src layout)"
    assert (
        "journalctl --user -u muyan-pilot@1.service -u "
        "muyan-pilot@2.service" in text
    ), "smoke must show the real journal command (both service instances)"


def test_chinese_getting_started_documents_the_model_provider_configuration():
    """Issue #179: the Chinese getting-started article carries the same
    model provider facts as the English page — the optional
    `pi_providers` file in Pi's `models.json` shape, the
    OpenAI-compatible boundary (local llama.cpp or any compatible
    API), the env-var API key reference, the gitignored env file the
    systemd unit loads, and the explicit boundary that no
    per-provider integration is claimed as complete."""
    text = zh_page_text("getting-started")
    for field in ("pi_providers", "pi_provider", "pi_model", "pi_thinking"):
        assert field in text, f"Chinese getting-started must document {field}"
    assert "models.json" in text, (
        "the provider file must be named in Pi's models.json shape"
    )
    assert "baseUrl" in text and "apiKey" in text, (
        "the provider file contract must name baseUrl and apiKey"
    )
    assert "$GROQ_API_KEY" in text, (
        "the OpenAI-compatible example must reference the key as an env var"
    )
    assert ".muyan-pilot/env" in text, (
        "the key value must live in the gitignored env file"
    )
    assert "EnvironmentFile" in text, (
        "the systemd unit's EnvironmentFile must be named"
    )
    assert "127.0.0.1:8080/v1" in text, (
        "the local example must use the real llama.cpp OpenAI-compatible URL"
    )
    assert "--alias" in text, ("the model id must be tied to the llama.cpp --alias")
    assert "OpenAI-compatible" in text, (
        "the documented provider boundary must be named"
    )
    assert "guarantee" in text.lower() or "tested" in text.lower() or "保证" in text, (
        "the article must state what is and is not tested/guaranteed"
    )
    assert "fail" in text.lower() or "失败" in text, (
        "the provider validation must be described as fail-fast"
    )


def test_chinese_getting_started_documents_the_full_chain_and_troubleshooting():
    """Issue #179: the Chinese smoke walkthrough verifies the FULL chain
    — picked up, implemented, tested, PR opened, independently
    reviewed, merged — with the same concrete success criteria as the
    English page (the real journal lines and the real `gh` checks), and
    the article carries a troubleshooting section."""
    text = zh_page_text("getting-started")
    assert "ai-pr-opened" in text, "the chain must reach ai-pr-opened"
    assert "ai-merged" in text, "the chain must reach ai-merged"
    assert "result=pr_opened" in text, ("the PR stage must cite the real run_end journal line")
    assert "verdict=pass" in text, ("the review stage must cite the real review journal line")
    assert "merged pr=" in text, ("the merge stage must cite the real merged journal line")
    assert "Fixes #" in text, "the PR body contract must be part of the success criteria"
    assert "gh pr view" in text, ("the terminal state must be checked with the real gh command")
    assert "mergedAt" in text, ("the gh PR check must use the real mergedAt field")
    assert "ai-blocked" in text, ("the failure outcome must be named")
    assert "run_failed" in text, ("the failure must cite the real journal line")
    assert "故障排查" in text, ("the article must carry a troubleshooting section")


def test_chinese_config_field_table_matches_the_implementation():
    """Every config field the Chinese docs document must exist in the
    committed example (and therefore in load_config); no invented field,
    and every example field must be documented."""
    text = zh_page_text("getting-started")
    documented = set(CONFIG_FIELD_PATTERN.findall(text))
    unknown = documented - KNOWN_CONFIG_FIELDS
    assert not unknown, f"Chinese docs document unknown config fields: {sorted(unknown)}"
    example = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    example_fields = set(CONFIG_FIELD_PATTERN.findall(example))
    missing = example_fields - documented
    assert not missing, (
        f"Chinese docs field table misses fields of the committed example: {sorted(missing)}"
    )


def test_chinese_workflow_documents_the_full_issue_to_merge_chain():
    """The Chinese workflow page must cover the complete automatic chain
    and the project's GitHub workflow vocabulary: all six delivery
    states, the review verdict, the PR body contract, the run marker,
    Epic (ai-epic), Release tasks, the P0 pickup order, and the SSH git
    transport — same facts as the English page."""
    text = zh_page_text("workflow")
    for state in (
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked",
    ):
        assert state in text, f"Chinese workflow page misses the {state} state"
    assert "REVIEW_VERDICT" in text, "workflow must document the review verdict"
    assert "Fixes #" in text, "workflow must document the PR body Fixes #N contract"
    assert "muyan-pilot:run=" in text, "workflow must document the run marker"
    assert "ai-epic" in text, "workflow must explain Epic issues (ai-epic)"
    assert "Release" in text, "workflow must explain Release tasks"
    assert "P0" in text, "workflow must explain the P0 priority"
    assert "`p0`" in text, "P0 explanation must name the p0 label"
    assert "`ai-ready`+`p0`" in text, "P0 explanation must show the order"
    assert "bug" in text.lower(), "P0 explanation must name the bug label"
    assert "SSH" in text, "workflow must document the SSH git transport"
    # The Chinese docs only reference labels that exist in the runner's
    # label set.
    mentioned = set(LABEL_PATTERN.findall(text))
    unknown = mentioned - KNOWN_LABELS
    assert not unknown, f"Chinese workflow references unknown labels: {sorted(unknown)}"


def test_chinese_operations_documents_the_real_commands_and_recovery():
    """The Chinese operations page must carry the real operations facts:
    the 5-minute timer, the journal, the CLI commands, the worktree and
    base freshness, the failure recovery (ai-blocked), the ExecStartPre
    code-update preflight, and the transport check without an HTTPS
    fallback — same facts as the English page."""
    text = zh_page_text("operations")
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
    assert "transport_check_failed" in text, (
        "operations must document the pre-start transport check"
    )
    assert "HTTPS" in text, (
        "operations must state that a broken transport never falls back to HTTPS"
    )


def test_chinese_security_documents_the_boundaries():
    """The Chinese security page must carry the same boundaries: the AI
    never merges or pushes the protected branch, the GitHub token is a
    local secret, the SSH key is the git-data credential, and the local
    KV/session files never leave the machine."""
    text = zh_page_text("security")
    assert "merge" in text.lower()
    assert "保护分支" in text, "security must name the protected branch boundary"
    assert "token" in text.lower(), "security must cover the GitHub token"
    assert "SSH key" in text, "security must document the SSH key as the git-data credential"
    assert "KV" in text, "security must cover the local KV/session files"


def test_chinese_testing_documents_the_contract_commands():
    """The Chinese testing page must show the tiered coverage policy
    (Issue #234: whole repository line >= 95% and branch >= 95% checked
    separately, changed Python code at 100%, core paths at 100%), the
    contract commands and the remote CI gate — same facts and numbers as
    the English page."""
    text = zh_page_text("testing")
    assert "coverage run --branch -m pytest tests/" in text, (
        "testing must show the contract coverage-run command"
    )
    assert "coverage report" in text, "testing must show the coverage report command"
    assert "coverage_gate.py" in text, (
        "testing must show the tiered global gate command"
    )
    assert "diff_coverage_gate.py" in text, (
        "testing must show the changed-code gate command"
    )
    assert "95%" in text, "testing must state the 95% line/branch tiers"
    assert "100%" in text, (
        "testing must state the 100% changed-code and core-path tiers"
    )
    assert "GitHub Actions" in text, "testing must name the remote CI gate"


def test_chinese_contributing_documents_issue_bug_and_pr_paths():
    """The Chinese contributing page must show how to create an Issue
    (with ai-ready), report a bug, and submit a PR (feature branch,
    Fixes #N, one PR) — same facts as the English page."""
    text = zh_page_text("contributing")
    assert "ai-ready" in text, "contributing must show how to dispatch an Issue"
    assert "bug" in text.lower(), "contributing must show how to report a bug"
    assert "PR" in text, "contributing must show how to submit a PR"
    assert "Fixes #" in text, "contributing must document the PR body contract"


def test_chinese_contributing_documents_the_minimal_implementation_principle():
    """Issue #118: the Chinese contributing page carries the same
    KISS/LEAN minimal implementation principle as the English page —
    smallest complete change, forbidden expansion list, 如无必要勿增实体,
    the simpler-design rule and the unchanged MVP boundary."""
    text = zh_page_text("contributing")
    assert "KISS" in text, "contributing must name KISS"
    assert "LEAN" in text, "contributing must name LEAN"
    assert "最小完整变更" in text, (
        "contributing must state the smallest complete change principle"
    )
    assert "如无必要勿增实体" in text, (
        "contributing must state 如无必要勿增实体"
    )
    assert "验收条件" in text, (
        "contributing must tie every new entity to an acceptance criterion"
    )
    assert "概念更少、文件更少" in text, (
        "contributing must prefer the simpler design"
    )
    assert "未来功能" in text or "future-proofing" in text, (
        "contributing must forbid future features / future-proofing"
    )


def test_chinese_kv_cache_page_matches_the_upstream_port_contract():
    """Same port contract as the English page: the agent-facing port
    18082 (the committed unit's value) stays documented, but the
    Configuration table must not claim 18082 as the env var's code
    default (8081)."""
    text = zh_page_text("optional-kv-cache")
    assert "18082" in text, (
        "the Chinese docs must keep the unit's agent-facing port 18082"
    )
    row = next(
        (line for line in text.splitlines()
         if line.startswith("| `PI_LLAMA_CACHE_PORT`")),
        None,
    )
    assert row is not None, "missing the PI_LLAMA_CACHE_PORT row"
    default_cell = row.strip("|").split("|")[1].strip()
    assert "18082" not in default_cell.split("(")[0], (
        f"the Chinese docs claim 18082 as the proxy code default: {row!r}"
    )


def test_chinese_docs_document_the_uv_prerequisite_boundary():
    """Issue #156: the Chinese docs carry the same initialization
    boundary as the English page — the user installs uv (official
    installer command), setup only checks/verifies, and setup never
    installs system packages, never installs gh, never runs
    `gh auth login`. The Chinese setup page lists uv in the command
    step and shows the uv-missing failure example."""
    started = zh_page_text("getting-started")
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in started, (
        "Chinese getting-started must give the official uv install command"
    )
    assert "gh auth login" in started, (
        "Chinese getting-started must state that setup never runs "
        "gh auth login"
    )
    setup = zh_page_text("setup")
    assert "required command missing: uv" in setup, (
        "Chinese setup must show the uv-missing setup_failed example"
    )
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in setup, (
        "Chinese setup's uv-missing failure example must carry the "
        "actionable install guidance"
    )


def test_chinese_setup_documents_the_migration_and_ssh_failure():
    """Same setup facts as the English page: the HTTPS-to-SSH migration
    command and the SSH connectivity failure."""
    text = zh_page_text("setup")
    assert "git remote set-url origin" in text, (
        "setup must document the HTTPS-to-SSH migration command"
    )
    assert "ssh_unreachable" in text, (
        "setup must document the SSH connectivity failure"
    )


def zh_docs_files() -> list[Path]:
    """All committed MDX pages under docs/zh/ (sorted, stable). The
    Chinese pages are flat (same structure as the English pages at the
    docs root), so a flat glob is the whole set."""
    files = sorted(ZH_DIR.glob("*.mdx"))
    assert files, "docs/zh/ contains no MDX pages"
    return files


def test_chinese_docs_only_reference_existing_labels_everywhere():
    for path in zh_docs_files():
        content = path.read_text(encoding="utf-8")
        mentioned = set(LABEL_PATTERN.findall(content))
        unknown = mentioned - KNOWN_LABELS
        assert not unknown, (
            f"{path.name} references unknown labels: {sorted(unknown)}"
        )


def test_readme_points_at_the_chinese_docs_entry():
    """The README keeps the project home and the documentation site
    entry; since Issue #116 it must also point at the Chinese docs
    (docs/zh/) so Chinese users find the entry from GitHub too.
    Issue #183: the site entry is the bound custom domain
    docs.orbi.build."""
    text = README.read_text(encoding="utf-8")
    assert "docs/zh/" in text, (
        "README must point at the Chinese docs entry (docs/zh/)"
    )
    assert "docs.orbi.build" in text, (
        "README must keep the documentation site entry (docs.orbi.build)"
    )


def test_chinese_getting_started_clones_the_orbi_build_org_repo():
    """Issue #183: same clone-URL contract as the English page — the
    repository moved to the orbi-build org, and the Chinese smoke
    walkthrough must clone the new address (the old one only survives
    as a GitHub redirect)."""
    text = zh_page_text("getting-started")
    assert "git clone https://github.com/orbi-build/orbi.git" in text, (
        "Chinese getting-started must clone the orbi-build/orbi repository"
    )
    assert "xqliu/muyan-pilot" not in text, (
        "Chinese getting-started keeps the stale xqliu/muyan-pilot reference"
    )
